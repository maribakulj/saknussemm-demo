"""JobRunner — bridges the pure ``CorrectionPipeline`` with infrastructure.

Owns the job lifecycle (STARTED → RUNNING → COMPLETED/FAILED), the
timeout budget, the observer adapter, and error sanitisation. The
JobStore is injected at construction time so it can be swapped (in
tests, or for a future out-of-process store) without touching the
pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import warnings
from collections.abc import Callable
from pathlib import Path

from saknussemm import (
    CorrectionAborted,
    CorrectionPipeline,
    CorrectionResult,
    LineRef,
    sanitize_error,
)
from saknussemm.core.events import ReconcileStats
from saknussemm.core.protocols import ProviderPermanentError
from saknussemm.core.schemas import (
    GuardConfig,
    ImageAsset,
    ImageTransform,
    ModelCapabilities,
    PageImage,
    PageManifest,
    PairingPolicy,
)
from saknussemm.errors import ConfigurationError
from saknussemm.producers.vision import build_image_asset

from app.jobs.engine_contract import require_engine_result_api
from app.jobs.events import JobEventType
from app.jobs.observers import CompositeObserver, JobStoreObserver, LoggingObserver
from app.protocols import BaseProvider, JobStore, OutputWriter
from app.schemas import DocumentManifest, JobStatus

# Checked at import — i.e. at server startup — and not at the end of the
# first run that reads one of those attributes. See engine_contract.
require_engine_result_api()

logger = logging.getLogger(__name__)


def page_image_assets(
    document_manifest: DocumentManifest, images_by_source: dict[str, Path]
) -> dict[str, ImageAsset]:
    """``{page_id: ImageAsset}`` from the demo's per-SOURCE-FILE image map.

    The two models disagree and the disagreement matters. This backend stores
    one image per uploaded source file, keyed by its stem; the library wants one
    per *physical page*, and ``require_page_images`` says why in as many words:
    "never one per source file: a multipage XML has as many scans as pages, and
    flattening them to a single per-file ref sent the producer the wrong image
    for every page but the first".

    So a source file carrying more than one page is **refused**, not flattened.
    A wrong scan produces a confident correction of a line that is not on it,
    and nothing downstream can see that — the projection invariant compares the
    artefact to the decisions the artefact was built from.
    """
    assets: dict[str, ImageAsset] = {}
    pages_per_source: dict[str, int] = {}
    for page in document_manifest.pages:
        pages_per_source[page.source_file] = pages_per_source.get(page.source_file, 0) + 1

    for page in document_manifest.pages:
        stem = Path(page.source_file).stem.lower()
        image_path = images_by_source.get(stem)
        if image_path is None:
            continue
        if pages_per_source[page.source_file] > 1:
            raise ValueError(
                f"{page.source_file!r} carries {pages_per_source[page.source_file]} "
                "pages but only one image was uploaded for it. A vision run needs "
                "one scan per physical page; sending the same image for every page "
                "would correct each line against the wrong scan. Upload one file "
                "per page, or run this document without vision."
            )
        asset = build_image_asset(page_id=page.page_id, path=image_path)
        assets[page.page_id] = asset.model_copy(update={"transform": _transform_for(page, asset)})
    return assets


def _transform_for(page: PageManifest, asset: ImageAsset) -> ImageTransform | None:
    """Map the ALTO/PAGE coordinate space onto the scan's pixels.

    **Not optional, and the reason is a measured near-miss.** A digital
    library serves a downscaled derivative — Gallica's IIIF ``!1600,1600``
    gives a 1193x1600 JPEG for a page whose ALTO measures 6802x9121, a factor
    of **0.1754**. With no transform the crop is taken at scale 1.0, so a line
    at ``hpos=4798`` is cropped from an image 1193 pixels wide: off the
    canvas, and every crop comes back blank or from the wrong place.

    Nothing downstream notices. The VLM is handed a picture of nothing, says
    something plausible about the OCR text it was also given, and the guards
    judge a proposal made without the evidence they think it was made with.

    Derived rather than configured: the page declares its own dimensions and
    the decoded scan declares its pixels, so the ratio is available without
    asking a caller for a DPI it may not know. Returns ``None`` when either
    side is missing — a wrong scale is worse than a declared unknown.
    """
    if not (asset.pixel_width and asset.pixel_height):
        return None
    if not (page.page_width and page.page_height):
        return None
    return ImageTransform(
        scale_x=asset.pixel_width / page.page_width,
        scale_y=asset.pixel_height / page.page_height,
    )


def _media_type_for(path: Path) -> str:
    """Decoded from the bytes, not guessed from the extension.

    A ``.jpg`` that is really a PNG makes the vendor reject the data URI, and
    the schema field documents itself as "determined from the bytes rather than
    guessed from the extension".
    """
    header = path.read_bytes()[:12]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:2] == b"\xff\xd8":
        return "image/jpeg"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return "application/octet-stream"


def _default_timeout_from_env() -> int:
    """Resolve JOB_TIMEOUT_SECONDS once at import. 0 disables the timeout.

    Kept at module scope (rather than inside ``JobRunner.__init__``) so
    tests can ``monkeypatch.setattr("app.jobs.runner.DEFAULT_JOB_TIMEOUT_SECONDS", N)``
    when they need a tighter budget than the production default.
    """
    try:
        return int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
    except ValueError:
        warnings.warn(
            "JOB_TIMEOUT_SECONDS env var is not a valid integer; using default 1800s",
            stacklevel=1,
        )
        return 1800


DEFAULT_JOB_TIMEOUT_SECONDS: int = _default_timeout_from_env()


class JobRunner:
    """Drives a `CorrectionPipeline` and persists its outcome to a JobStore."""

    def __init__(self, job_store: JobStore) -> None:
        self.job_store = job_store

    @staticmethod
    def _commit_outputs(output_writer: OutputWriter) -> None:
        """P0-4 — promote staged outputs atomically after a successful
        run. Duck-typed: writers without a staging concept (in-memory
        test doubles, custom sinks) simply skip it."""
        commit = getattr(output_writer, "commit", None)
        if callable(commit):
            commit()

    @staticmethod
    def _discard_outputs(output_writer: OutputWriter) -> None:
        """P0-4 — drop staged outputs on failure/timeout/cancellation so
        nothing partial ever becomes downloadable."""
        discard = getattr(output_writer, "discard", None)
        if callable(discard):
            discard()

    async def run(
        self,
        *,
        job_id: str,
        document_manifest: DocumentManifest,
        provider_name: str,
        api_key: str,
        model: str,
        output_writer: OutputWriter,
        source_files: dict[str, Path],
        provider: BaseProvider | None = None,
        pairing_policy: PairingPolicy | None = None,
        page_images: dict[str, PageImage] | None = None,
        timeout_seconds: int = 1800,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        """Run a job end-to-end. Updates the JobStore as side effect.

        `output_writer`: injected sink for the corrected ALTO + trace.
        The caller chooses the implementation (filesystem, S3, in-memory
        for tests, ...) — the runner stays oblivious of where outputs land.
        `source_files`: mapping of source_name → xml_path on disk.
        `provider`: injected provider (for testing); if None, resolved
        from the global registry via `app.providers.get_provider`.
        `timeout_seconds`: 0 disables the timeout.
        `pairing_policy`: the SAME policy the document was parsed with
        (`geometric_pairing` opt-out). Hyphen pairing happens at parse time,
        so this is provenance-only: it feeds `config_fingerprint()` so the
        stamped fingerprint names the policy actually used, not the default.
        `should_abort`: Plan V2.2 — cooperative cancellation probe,
        forwarded to the pipeline (polled between pages and chunks).
        When it trips, the job lands in CANCELLED with no output promoted.
        `page_images`: page_id → scan, from ``page_image_assets()``. Present
        means a VISION run: the producer becomes ``VisionEditProducer`` and the
        guards become ``GuardConfig.vision()``. Absent (the default) keeps the
        historical text path byte for byte.
        """
        if provider is None:
            from app.providers import get_provider
            from app.schemas import Provider

            provider = get_provider(Provider(provider_name))

        start_time = time.monotonic()

        try:
            timeout = timeout_seconds if timeout_seconds > 0 else None
            result = await asyncio.wait_for(
                self._run_pipeline(
                    job_id=job_id,
                    document_manifest=document_manifest,
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    provider_name=provider_name,
                    output_writer=output_writer,
                    source_files=source_files,
                    pairing_policy=pairing_policy,
                    page_images=page_images,
                    should_abort=should_abort,
                ),
                timeout=timeout,
            )
            total_chunks = result.total_chunks
            total_reconciled = result.total_reconciled

            lines_modified = sum(
                1
                for page in document_manifest.pages
                for lm in page.lines
                if lm.corrected_text is not None and lm.corrected_text != lm.ocr_text
            )
            elapsed = round(time.monotonic() - start_time, 2)

            # P0-4 — promote the staged outputs BEFORE the job turns
            # terminal-success: a client that sees the status can always
            # download the complete, committed set.
            self._commit_outputs(output_writer)

            # COMPLETED strictly means "zero fallback LINES". The count is
            # per line (manifest statuses), not per chunk: a rejected
            # 20-line chunk is 20 uncorrected lines, and a guard-rejected
            # line counts even when no chunk ever failed — the UI renders
            # this number as "N line(s) fell back".
            #
            # A WITHHELD FILE outranks a fallen line. Both are degradations,
            # but a fallen line still ships its page; a withheld file is a
            # page absent from the download, and reporting that as plain
            # `completed` would tell the user a volume is complete when it is
            # not. The engine refuses that for callers using its `write()`;
            # this backend stages through its own writer, so the refusal is
            # made here instead.
            if result.undeliverable_files:
                terminal = JobStatus.COMPLETED_WITH_WITHHELD_FILES
            elif result.fallback_lines > 0:
                terminal = JobStatus.COMPLETED_WITH_FALLBACKS
            else:
                terminal = JobStatus.COMPLETED
            self.job_store.update_job(
                job_id,
                status=terminal,
                chunks_total=total_chunks,
                lines_modified=lines_modified,
                duration_seconds=elapsed,
                withheld_files=result.undeliverable_files,
            )

            # Job-end reconcile_stats observability event — emitted just
            # BEFORE the terminal `completed` so subscribers that exit
            # on `completed` still receive it.
            reconcile_event = ReconcileStats(
                coherent=result.reconcile_metrics.coherent,
                fallback=result.reconcile_metrics.fallback,
                neutralised=result.reconcile_metrics.neutralised,
                total=result.reconcile_metrics.total,
            )
            self.job_store.emit(job_id, reconcile_event.type, reconcile_event.payload())

            self.job_store.emit(
                job_id,
                JobEventType.COMPLETED,
                {
                    "job_id": job_id,
                    "total_lines": document_manifest.total_lines,
                    "lines_modified": lines_modified,
                    "hyphen_pairs_total": total_reconciled,
                    "chunks_total": total_chunks,
                    "duration_seconds": elapsed,
                    # P0-1 — degraded-success visibility: the terminal
                    # status and the fallback count ride the event so the
                    # client can render "success" vs "success with N
                    # uncorrected lines" without an extra round-trip.
                    "status": terminal.value,
                    "fallbacks": result.fallback_lines,
                },
            )

        except TimeoutError:
            self._discard_outputs(output_writer)
            logger.error("Job %s timed out after %ss", job_id, timeout_seconds)
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = f"Job timed out after {timeout_seconds}s"
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id, JobEventType.FAILED, {"job_id": job_id, "error": safe_error}
            )

        except CorrectionAborted:
            # Plan V2.2 — the user's cancel request tripped the pipeline's
            # should_abort probe. This is a REQUESTED outcome, not a
            # failure: distinct terminal state, no output promoted.
            self._discard_outputs(output_writer)
            logger.info("Job %s cancelled on user request", job_id)
            elapsed = round(time.monotonic() - start_time, 2)
            self.job_store.update_job(
                job_id,
                status=JobStatus.CANCELLED,
                duration_seconds=elapsed,
            )
            self.job_store.emit(job_id, JobEventType.CANCELLED, {"job_id": job_id})

        except asyncio.CancelledError:
            self._discard_outputs(output_writer)
            # L10/B8 — SIGTERM during shutdown cancels the runner task
            # via `BackgroundTaskRegistry.shutdown()` past the 30 s grace
            # deadline. `CancelledError` extends `BaseException` (not
            # `Exception`) in Python 3.8+, so without this handler it
            # slipped past both `except TimeoutError` and `except
            # Exception` — leaving the job in RUNNING forever. The job
            # would never enter `_completed_at`, never be evicted, and
            # leak across redeploys.
            logger.warning("Job %s cancelled (likely server shutdown)", job_id)
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = "Job cancelled (server shutdown or task cancellation)"
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id, JobEventType.FAILED, {"job_id": job_id, "error": safe_error}
            )
            # Re-raise so the task scheduler sees the cancellation and
            # propagates it correctly (this is the documented asyncio
            # pattern for handling CancelledError — never silently swallow).
            raise

        except ProviderPermanentError as exc:
            self._discard_outputs(output_writer)
            # P0-1 — the provider definitively rejected the request
            # (invalid key, unknown model, 4xx family). The message is
            # already built provider-side without credentials; sanitise
            # anyway (defence in depth) and fail with a clear, actionable
            # error instead of ever reaching COMPLETED.
            logger.error(
                "Job %s failed on a permanent provider error (HTTP %s)",
                job_id,
                exc.status_code,
            )
            elapsed = round(time.monotonic() - start_time, 2)
            safe_error = sanitize_error(str(exc), api_key)[:500]
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                duration_seconds=elapsed,
            )
            self.job_store.emit(
                job_id,
                JobEventType.FAILED,
                {"job_id": job_id, "error": safe_error},
            )

        except Exception as exc:
            self._discard_outputs(output_writer)
            logger.exception("Job %s failed", job_id)
            # Sanitise BEFORE truncating: if the api_key straddles the 500-char
            # boundary, slicing first would leave half the key visible and the
            # regex would fail to mask it.
            safe_error = sanitize_error(str(exc), api_key)[:500]
            self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=safe_error,
                # Audit P3 — round like the three other handlers do (was
                # the only one recording an unrounded float).
                duration_seconds=round(time.monotonic() - start_time, 2),
            )
            self.job_store.emit(
                job_id,
                JobEventType.FAILED,
                {
                    "job_id": job_id,
                    "error": safe_error,
                },
            )

    async def _run_pipeline(
        self,
        *,
        job_id: str,
        document_manifest: DocumentManifest,
        provider: BaseProvider,
        api_key: str,
        model: str,
        provider_name: str,
        output_writer: OutputWriter,
        source_files: dict[str, Path],
        pairing_policy: PairingPolicy | None = None,
        page_images: dict[str, PageImage] | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> CorrectionResult:
        """Drive the pure pipeline and persist its counters back.

        `page_images`: page_id → scan. Present means a VISION run — the
        producer changes, and so does the guard config.
        """
        self.job_store.update_job(job_id, status=JobStatus.STARTED)
        self.job_store.emit(job_id, JobEventType.STARTED, {"job_id": job_id})

        self.job_store.update_job(
            job_id,
            status=JobStatus.RUNNING,
            document_manifest=document_manifest,
            total_lines=document_manifest.total_lines,
        )

        # Fan events out to the job store (for SSE clients) and to the
        # standard logger (for operators). ADR-006: saknussemm never
        # logs by itself — adapters here own the routing.
        #
        # §5.1 resorption — credentials go into the producer (via the
        # for_provider convenience), never into run(): the pipeline surface
        # carries no api_key anywhere.
        observer = CompositeObserver([JobStoreObserver(self.job_store, job_id), LoggingObserver()])
        if page_images:
            # Vision run. `for_provider` cannot serve it: it always builds the
            # TEXT producer, around a client whose seam carries no images on
            # purpose. So the producer is assembled here, with the three things
            # a VLM run needs and a text run must not have.
            from saknussemm.producers.vision import (
                MultimodalStructuredClient,
                VisionEditProducer,
            )

            # Checked, not assumed. A text provider here would fail at the
            # first call, deep inside a run, with a message about a missing
            # method rather than about the job being misconfigured — and the
            # run would already have spent its first chunk getting there.
            if not isinstance(provider, MultimodalStructuredClient):
                raise ConfigurationError(
                    f"a vision run needs a multimodal provider, and "
                    f"{type(provider).__name__} implements the text seam only. "
                    "Pass MistralMultimodalProvider, or run this job without "
                    "page images."
                )
            max_images = getattr(provider, "MAX_IMAGES_PER_CALL", None)
            producer = VisionEditProducer(
                provider=provider,
                api_key=api_key,
                model=model,
                # `max_images` is what makes core/batching.py split a chunk.
                # Without it the engine sends every line's crop in one call and
                # the vendor refuses the request outright.
                capabilities=ModelCapabilities(
                    text=True,
                    vision=True,
                    structured_output=True,
                    max_images=max_images,
                ),
            )
            pipeline = CorrectionPipeline(
                producer=producer,
                observer=observer,
                pairing_policy=pairing_policy,
                # A VLM reads the image, so a CORRECT reading of a badly
                # garbled line diverges from the OCR text further than the
                # text-tuned guard tolerates (0.35 → 0.15). Running vision
                # under the text config refuses the model's best work: measured
                # 18 refusals out of 20 lines, every one
                # `too_different_from_source`.
                guard_config=GuardConfig.vision(),
            )
        else:
            # §5.1 resorption — credentials go into the producer (via the
            # for_provider convenience), never into run(): the pipeline surface
            # carries no api_key anywhere.
            pipeline = CorrectionPipeline.for_provider(
                provider,
                api_key=api_key,
                model=model,
                provider_name=provider_name,
                observer=observer,
                # Provenance parity — the pipeline hashes this into the §11
                # config fingerprint. Passing the job's actual policy (default
                # or geometric_pairing opt-out) keeps the stamped fingerprint
                # honest; omitting it silently reverted to
                # DEFAULT_PAIRING_POLICY.
                pairing_policy=pairing_policy,
            )
        # `run_id` is saknussemm's generic identifier; we feed it the
        # server-side `job_id` so trace.json correlates with the API.
        result = await pipeline.run(
            document_manifest=document_manifest,
            source_files=source_files,
            page_images=page_images,
            run_id=job_id,
            # Plan V2.2 — the cancel endpoint's event, polled by the
            # pipeline between pages and chunks.
            should_abort=should_abort,
        )

        # ADR-011 slice E — the engine never mutates its input: the run's
        # outcome lives on result.decisions. The SERVER owns its read
        # models (/diff, /layout, lines_modified), so it projects the
        # decided text/status onto ITS stored manifest here — the same
        # object update_job() registered at run start.
        for page in document_manifest.pages:
            for lm in page.lines:
                decision = result.decisions.by_ref[LineRef(page_id=lm.page_id, line_id=lm.line_id)]
                lm.corrected_text = decision.final_text
                lm.status = decision.status

        # ADR-011 slice D — the engine never persists: the result carries
        # the corrected XML and the §9 report, and the backend stages them
        # here through ITS writer (the P0-4 commit/discard transaction in
        # run() stays unchanged). Disk IO runs off the event loop so a
        # large artefact set never blocks SSE keepalives or /health.
        for source_name, xml_bytes in result.corrected_files.items():
            await asyncio.to_thread(
                output_writer.write_corrected,
                source_stem=source_files[source_name].stem,
                xml_bytes=xml_bytes,
            )
        await asyncio.to_thread(
            output_writer.write_trace,
            traces_payload=result.report.model_dump_json(indent=2),
        )

        self.job_store.update_job(
            job_id,
            retries=result.retry_count,
            fallbacks=result.fallback_lines,
            # §9 unification — the run's CorrectionReport is the job's trace
            # artefact (served by /trace, dumped as trace.json). run_id ==
            # job_id (fed above), so the report self-correlates with the API.
            # It carries the per-line LineTrace list; no separate copy is kept.
            report=result.report,
        )

        return result
