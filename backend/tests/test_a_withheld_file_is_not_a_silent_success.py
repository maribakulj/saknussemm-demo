"""A page missing from the download must not be reported as a complete run.

The engine changed contract on 2026-08-19. A source file whose rewritten
artefact does not carry the run's decisions is no longer fatal to the whole
run — it is **withheld**: absent from ``corrected_files``, named on
``undeliverable_files``. Losing 300 pages for one line was never worth it.

That trade creates one precise risk, and this backend is where it lands. The
engine defends its own callers with ``CorrectionResult.write``, which refuses
an incomplete set. This backend does not use it: it stages through its own
transactional writer, by design. So it loops over ``corrected_files``, writes
299 of 300 pages, and — before this module — reported ``completed``.

The fix is the same shape the demo already chose for fallen lines: a terminal
state of its own. And a withheld file outranks a fallen line, because a fallen
line still ships its page while a withheld file is a page that is not there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from saknussemm.formats.alto.parser import build_document_manifest

from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.schemas import JobStatus, Provider
from app.schemas.job import TERMINAL_SUCCESS_STATES
from app.storage.output_writer import FilesystemOutputWriter
from tests.test_orchestrator import SAMPLE_XML, MockProvider


class _WritesSomethingUnrepresentable(MockProvider):
    """Returns a line carrying a character XML cannot hold.

    U+0000 has no XML representation at all, so whatever the writer emits,
    the re-extracted text cannot equal the decision — the definition of a
    projection divergence. Using the format's own limit rather than a library
    defect keeps this test about the STATE, not about any one bug.

    It is APPENDED to the OCR text rather than replacing it, so the line
    still passes the acceptance guards. A wholesale replacement is refused as
    too dissimilar from the source, the line falls back, the artefact then
    agrees with the decision, and nothing is withheld — which would make this
    module pass for the wrong reason.
    """

    async def complete_structured(self, *args, **kwargs):  # type: ignore[override]
        payload, usage = await super().complete_structured(*args, **kwargs)
        for line in payload.get("lines", []):
            line["corrected_text"] = line["corrected_text"] + "\x00"
        return payload, usage


async def _run(tmp_path: Path, provider) -> tuple[JobStore, str]:
    store = JobStore()
    job_id = store.create_job(provider=Provider.OPENAI, model="mock")
    doc = build_document_manifest([(SAMPLE_XML, SAMPLE_XML.name)])
    await JobRunner(job_store=store).run(
        job_id=job_id,
        document_manifest=doc,
        provider_name="openai",
        api_key="fake-key",
        model="mock",
        provider=provider,
        output_writer=FilesystemOutputWriter(tmp_path),
        source_files={SAMPLE_XML.name: SAMPLE_XML},
    )
    return store, job_id


@pytest.mark.asyncio
async def test_a_withheld_file_gets_its_own_terminal_state(tmp_path) -> None:
    """`completed` must keep meaning "the whole set is there"."""
    store, job_id = await _run(tmp_path, _WritesSomethingUnrepresentable())
    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED_WITH_WITHHELD_FILES, job.status


@pytest.mark.asyncio
async def test_the_job_says_which_file_and_why(tmp_path) -> None:
    """A state alone would make the user hunt for the missing page.

    Mirrored onto the job so a client can tell a download is incomplete
    without parsing the §9 report — the same reason `fallbacks` sits beside
    the report that also contains it.
    """
    store, job_id = await _run(tmp_path, _WritesSomethingUnrepresentable())
    job = store.get_job(job_id)
    assert job is not None
    assert SAMPLE_XML.name in job.withheld_files
    assert job.withheld_files == (job.report.undeliverable_files if job.report else {})

    # Polling and an idempotent cancel return the same incomplete-set signal.
    from app.main import create_app

    with TestClient(create_app()) as client:
        client.app.state.job_store = store
        for response in (
            client.get(f"/api/jobs/{job_id}"),
            client.post(f"/api/jobs/{job_id}/cancel"),
        ):
            assert response.json()["status"] == "completed_with_withheld_files"
            assert response.json()["withheld_files"] == job.withheld_files


@pytest.mark.asyncio
async def test_the_state_is_still_a_success_so_the_good_files_ship(tmp_path) -> None:
    """Excluding it would make the missing page cost the good ones too.

    Which is exactly the trade the engine stopped making — reproducing it
    one layer up would undo the change rather than surface it.
    """
    assert JobStatus.COMPLETED_WITH_WITHHELD_FILES in TERMINAL_SUCCESS_STATES


@pytest.mark.asyncio
async def test_a_clean_run_is_untouched(tmp_path) -> None:
    """The control: nothing withheld, nothing said, plain `completed`."""
    store, job_id = await _run(tmp_path, MockProvider())
    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED, job.status
    assert job.withheld_files == {}
