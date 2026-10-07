"""Server-side job enums and record (moved out of saknussemm by spec F12).

``Provider``, ``JobStatus`` and ``JobManifest`` (with its ``images`` map)
are backend concerns — the pure correction core does not enumerate LLM
vendors or track a server job's lifecycle. They live here now; saknussemm
keeps only the domain enums (``LineStatus``, ``ChunkGranularity``,
``HyphenRole``, ``PipelineEventType``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from saknussemm.core.schemas import CorrectionReport, DocumentManifest


class JobStatus(str, Enum):
    """Lifecycle state of a correction job, surfaced to API clients."""

    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    #: P0-1 — terminal success where one or more lines fell back to their
    #: OCR source text (rejected LLM output, repeated per-chunk failures).
    #: The corrected files are valid and downloadable, but the run is
    #: explicitly DEGRADED: consumers must be able to distinguish "every
    #: line went through the provider" from "some lines silently kept
    #: their OCR text". COMPLETED excludes both fallbacks and review referrals.
    COMPLETED_WITH_FALLBACKS = "completed_with_fallbacks"
    #: The engine delivered proposed corrections requiring human review.
    #: Downloading is useful for that review; it is not an approval of the text.
    COMPLETED_WITH_REVIEW_REQUIRED = "completed_with_review_required"
    #: Terminal success where one or more SOURCE FILES are missing from the
    #: output: the engine rewrote them, re-read them, found the artefact did
    #: not carry what the run decided, and withheld them rather than hand
    #: back bytes nobody vouched for.
    #:
    #: Its own state for the same reason `COMPLETED_WITH_FALLBACKS` has one,
    #: and a stronger one. A fallen line kept its OCR text — the page still
    #: ships. A withheld file is a page **absent from the download**, and a
    #: job reporting plain `completed` would tell the user a volume is
    #: complete when it is not. The engine refuses that mistake for callers
    #: using its `write()`; this backend owns its writer, so it answers the
    #: same question itself.
    COMPLETED_WITH_WITHHELD_FILES = "completed_with_withheld_files"
    FAILED = "failed"
    #: Plan V2.2 — cooperative cancellation. CANCEL_REQUESTED is set by
    #: the cancel endpoint; the pipeline's `should_abort` probe trips
    #: between chunks/pages and the runner lands the job in CANCELLED
    #: (terminal — no output is ever promoted).
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


#: The terminal states whose outputs are valid and downloadable.
#:
#: `COMPLETED_WITH_WITHHELD_FILES` belongs here: what it produced IS valid —
#: every file present carries the run's decisions — it is simply not the
#: whole set. Excluding it would make the missing page cost the good ones
#: too, which is exactly the trade the engine stopped making.
TERMINAL_SUCCESS_STATES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_FALLBACKS,
        JobStatus.COMPLETED_WITH_REVIEW_REQUIRED,
        JobStatus.COMPLETED_WITH_WITHHELD_FILES,
    }
)


class Provider(str, Enum):
    """Identifier for an LLM vendor. Each value maps to one ``BaseProvider``."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    MISTRAL = "mistral"
    GOOGLE = "google"


class JobManifest(BaseModel):
    """Server-side record of a correction job — status, counters, trace data."""

    # L10/F6 — `JobStore.update_job` mutates fields via `setattr(job, k, v)`
    # in a loop. Pydantic v2's default `validate_assignment=False` would
    # silently accept any type at assignment time, so a typo like
    # `update_job(jid, status="garbage")` lands a string into the enum
    # field; downstream `job.status.value` then crashes far from the
    # original mistake. Turning validation on at assignment surfaces
    # the bug at the offending call-site immediately.
    model_config = ConfigDict(validate_assignment=True)

    job_id: str
    provider: Provider
    model: str
    # P1-7 — SHA-256 hex of the capability token required to access this
    # job's endpoints. None = legacy/direct-store job (no enforcement) —
    # every job created through the public API carries one.
    token_hash: str | None = None
    status: JobStatus = JobStatus.QUEUED
    document_manifest: DocumentManifest | None = None
    total_lines: int = 0
    lines_modified: int = 0
    chunks_total: int = 0
    retries: int = 0
    #: Number of LINES that kept their OCR source text (chunk fallback,
    #: guard rejection or duplicate revert) — the UI renders this as
    #: "N line(s) fell back", so it must never be a chunk count.
    fallbacks: int = 0
    #: Distinct from fallbacks: these lines retain their proposed correction.
    review_lines: int = 0
    review_reasons: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float | None = None
    error: str | None = None
    images: dict[str, str] = Field(default_factory=dict)
    # §9 — the run's public CorrectionReport: the per-line LineTrace list,
    # promoted to the versioned artefact. The /trace endpoint serves this;
    # trace.json on disk is its JSON dump. This is the ONLY trace copy the
    # job keeps (the former parallel ``line_traces`` dict was redundant —
    # nothing read it — and is gone).
    report: CorrectionReport | None = None
    #: Human review, keyed ``"<page_id> <line_id>"`` — see `app.api.review`.
    #: Keyed on the PAIR because a line id repeats across files (`ADR-001`);
    #: the bare id would merge two documents' judgements the first time a job
    #: carries more than one ALTO. Untyped here so the schema layer stays
    #: free of the API layer's models.
    reviews: dict[str, dict] = Field(default_factory=dict)
    #: Source files the engine withheld, mapped to why. Empty on a job whose
    #: every file was delivered.
    #:
    #: Mirrored off `report.undeliverable_files` rather than read from it, so
    #: a client can tell that a download is incomplete without parsing the §9
    #: report — the same reason `fallbacks` is a field beside the report that
    #: also contains it.
    withheld_files: dict[str, str] = Field(default_factory=dict)


__all__ = ["TERMINAL_SUCCESS_STATES", "JobManifest", "JobStatus", "Provider"]
