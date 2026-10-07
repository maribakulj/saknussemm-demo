"""A delivered correction can still require the reader's judgement."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.read_models import build_layout
from app.jobs.store import JobStore
from tests.test_a_withheld_file_is_not_a_silent_success import _run
from tests.test_orchestrator import MockProvider


class ReferringProvider(MockProvider):
    async def complete_structured(self, *args, **kwargs):
        payload, usage = await super().complete_structured(*args, **kwargs)
        for line in payload["lines"]:
            if line["line_id"] == "TL2":
                line["corrected_text"] += " en 1789"
        return payload, usage


@pytest.fixture
def referral_job(tmp_path, monkeypatch):
    terminal_events = []
    emit = JobStore.emit

    def record_terminal(store, job_id, event, data):
        if event == "completed":
            terminal_events.append(data)
        emit(store, job_id, event, data)

    monkeypatch.setattr(JobStore, "emit", record_terminal)
    store, job_id = asyncio.run(_run(tmp_path, ReferringProvider()))
    job = store.get_job(job_id)
    assert job is not None and job.report is not None
    assert any(line.decision.status == "review_required" for line in job.report.lines)
    return store, job, tmp_path, terminal_events


@pytest.fixture
def client(referral_job, monkeypatch):
    from app.api import jobs
    from app.main import create_app

    store, job, output, _ = referral_job
    monkeypatch.setattr(jobs, "get_output_files", lambda _job_id: sorted(output.glob("*.xml")))
    with TestClient(create_app()) as test_client:
        test_client.app.state.job_store = store
        yield test_client


def test_real_engine_referral_is_a_distinct_terminal_state(referral_job):
    _, job, _, _ = referral_job
    assert job.status.value == "completed_with_review_required"
    assert job.fallbacks == 0
    assert job.review_lines == 1
    assert job.review_reasons == {"digits_changed": 1}


def test_poll_and_reconnected_stream_keep_the_review_signal(client, referral_job):
    store, job, _, live_events = referral_job
    snapshot = client.get(f"/api/jobs/{job.job_id}").json()
    terminal = store._synthetic_terminal(job.job_id, job)
    assert len(live_events) == 1
    for payload in (snapshot, terminal.data, live_events[0]):
        assert payload["status"] == "completed_with_review_required"
        assert payload["review_lines"] == 1
        assert payload["review_reasons"] == {"digits_changed": 1}
    assert terminal.event == "completed"

    # A late subscriber receives a terminal event rather than a keepalive loop.
    async def first_event():
        stream = store.stream_events(job.job_id)
        try:
            return await asyncio.wait_for(anext(stream), timeout=1)
        finally:
            await stream.aclose()

    assert asyncio.run(first_event()).event == "completed"


def test_layout_exposes_all_referral_reasons(referral_job):
    _, job, _, _ = referral_job
    layout = build_layout(job.job_id, job.document_manifest, {}, job.report)
    line = next(
        line
        for page in layout["pages"]
        for block in page["blocks"]
        for line in block["lines"]
        if line["line_id"] == "TL2"
    )
    assert line["verdict"] == "review_required"
    assert line["review_reasons"] == [{"code": "digits_changed", "detail": "∅ → 1789"}]


def test_candidate_download_and_judgements_never_claim_xml_was_applied(client, referral_job):
    _, job, _, _ = referral_job
    url = f"/api/jobs/{job.job_id}/download"
    response = client.get(url)
    assert response.status_code == 200
    assert "candidate" in response.headers["content-disposition"]
    assert response.headers["x-saknussemm-review-lines"] == "1"
    original_bytes = response.content
    flagged = next(line for line in job.report.lines if line.decision.status == "review_required")
    for verdict in ("accepted", "refused", "transcribed"):
        saved = client.put(
            f"/api/jobs/{job.job_id}/reviews",
            json={
                "reviews": [
                    {
                        "page_id": flagged.page_id,
                        "line_id": flagged.line_id,
                        "verdict": verdict,
                        "transcription": "human reading" if verdict == "transcribed" else None,
                    }
                ]
            },
        )
        assert saved.status_code == 200
        downloaded = client.get(url)
        assert downloaded.content == original_bytes
        assert "candidate" in downloaded.headers["content-disposition"]


def test_a_referral_is_finished_for_cancellation_and_eviction(client, referral_job):
    store, job, _, _ = referral_job
    response = client.post(f"/api/jobs/{job.job_id}/cancel")
    assert response.status_code == 202
    assert response.json()["status"] == "completed_with_review_required"
    assert response.json()["review_lines"] == 1
    assert response.json()["review_reasons"] == {"digits_changed": 1}
    assert job.job_id in store._completed_at


def test_candidate_zip_identifies_each_unreviewed_file(client, referral_job):
    _, job, output, _ = referral_job
    original = next(output.glob("*.xml"))
    (output / "second_corrected.xml").write_bytes(original.read_bytes())
    downloaded = client.get(f"/api/jobs/{job.job_id}/download")
    assert downloaded.status_code == 200
    assert "candidate.zip" in downloaded.headers["content-disposition"]
    assert downloaded.headers["x-saknussemm-review-lines"] == "1"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.namelist() == [
            "sample_corrected_candidate.xml",
            "second_corrected_candidate.xml",
        ]
