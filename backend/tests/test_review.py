"""Human review — the only place ground truth can come from.

These corpora have none. Gallica ships a `text.txt` that is the *same OCR
layer* as its ALTO, so nothing on disk can say whether a correction was right
or a refusal justified. A reader looking at the scan is the only source, and
this is where that reading is kept so it accumulates instead of evaporating.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.review import LineReview, ReviewVerdict
from app.jobs.store import JobStore
from app.schemas import Provider


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient on an isolated storage dir — the demo's usual shape."""
    from app import storage as storage_module
    from app.main import create_app

    monkeypatch.setattr(storage_module, "_BASE_DIR", tmp_path / "jobs")
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def job(client: TestClient) -> str:
    """A store-created job: no token hash, so the capability gate stays open.

    Jobs created outside the HTTP layer are ungated by design (`P1-7`), which
    is what lets a test exercise the route without minting a token.
    """
    store: JobStore = client.app.state.job_store
    return store.create_job(provider=Provider.MISTRAL, model="m")


def _put(client: TestClient, job_id: str, reviews: list[dict]):
    return client.put(f"/api/jobs/{job_id}/reviews", json={"reviews": reviews})


def test_a_review_survives_and_comes_back(client: TestClient, job: str) -> None:
    response = _put(
        client,
        job,
        [
            {
                "page_id": "PAG_1",
                "line_id": "TL000323",
                "verdict": "refused",
                "note": "l'OCR lit ETANTS, le scan dit ENFANTS",
            }
        ],
    )
    assert response.status_code == 200, response.text

    fetched = client.get(f"/api/jobs/{job}/reviews").json()["reviews"]
    assert len(fetched) == 1
    assert fetched[0]["verdict"] == "refused"
    assert "ENFANTS" in fetched[0]["note"]
    assert fetched[0]["reviewed_at"], "the server stamps the date, not the client"


def test_reviewing_a_line_twice_replaces_rather_than_appends(client: TestClient, job: str) -> None:
    """A reader who changes their mind must not be fighting an append log."""
    line = {"page_id": "PAG_1", "line_id": "TL1", "verdict": "accepted"}
    _put(client, job, [line])
    _put(client, job, [{**line, "verdict": "refused", "note": "en y regardant mieux"}])

    reviews = client.get(f"/api/jobs/{job}/reviews").json()["reviews"]
    assert len(reviews) == 1, reviews
    assert reviews[0]["verdict"] == "refused"


def test_two_files_sharing_a_line_id_stay_separate(client: TestClient, job: str) -> None:
    """`ADR-001` — a line id repeats across files; only the PAIR is unique.

    Keyed on the bare id, these two judgements would merge the first time a
    job carries more than one ALTO, and the second reader would silently
    overwrite the first.
    """
    _put(
        client,
        job,
        [
            {"page_id": "PAG_1", "line_id": "TL1", "verdict": "accepted"},
            {"page_id": "PAG_2", "line_id": "TL1", "verdict": "refused"},
        ],
    )
    reviews = client.get(f"/api/jobs/{job}/reviews").json()["reviews"]
    assert len(reviews) == 2, reviews
    assert {r["page_id"] for r in reviews} == {"PAG_1", "PAG_2"}


def test_a_transcription_without_text_is_refused(client: TestClient, job: str) -> None:
    """The text IS the review, so a `transcribed` verdict cannot be empty.

    Accepting it would fill the ground-truth set with rows that assert
    nothing, and they would be indistinguishable later from real ones.
    """
    response = _put(
        client,
        job,
        [{"page_id": "PAG_1", "line_id": "TL1", "verdict": "transcribed"}],
    )
    assert response.status_code == 422
    assert "text the reader read" in response.text


def test_a_transcription_is_what_the_reader_read(client: TestClient, job: str) -> None:
    """The verdict that produces ground truth rather than grading the engine."""
    _put(
        client,
        job,
        [
            {
                "page_id": "PAG_1",
                "line_id": "TL000323",
                "verdict": "transcribed",
                "transcription": "1,500 ENFANTS : LE NOMBRE DES MORTS S'ÉLÈVE",
            }
        ],
    )
    review = client.get(f"/api/jobs/{job}/reviews").json()["reviews"][0]
    assert review["transcription"].startswith("1,500 ENFANTS")


def test_an_unknown_job_is_not_reviewable(client: TestClient) -> None:
    response = _put(client, "nope", [{"page_id": "P", "line_id": "L", "verdict": "accepted"}])
    assert response.status_code == 404


def test_the_key_separator_cannot_occur_in_an_id() -> None:
    """Why the composite key joins on NUL rather than a space.

    An XML id may legally contain neither, but a producer emitting an id with
    a space is a real possibility and would merge two lines; a NUL is not
    representable in XML at all, so the pair round-trips unambiguously.
    """
    from app.api.review import _key

    left = LineReview(page_id="A B", line_id="C", verdict=ReviewVerdict.ACCEPTED)
    right = LineReview(page_id="A", line_id="B C", verdict=ReviewVerdict.ACCEPTED)
    assert _key(left) != _key(right), (
        "two different (page, line) pairs collapsed to one key — a space "
        "separator would do exactly this"
    )


@pytest.mark.parametrize("activity", ["save", "activity", "export"])
def test_review_activity_renews_retention_but_an_idle_job_still_expires(
    client: TestClient, job: str, monkeypatch, activity: str
) -> None:
    from app.jobs import store as store_module
    from app.schemas import JobStatus

    now = [1000.0]
    monkeypatch.setattr(store_module.time, "monotonic", lambda: now[0])
    store = client.app.state.job_store
    store.update_job(job, status=JobStatus.COMPLETED_WITH_REVIEW_REQUIRED)
    now[0] += 3500
    if activity == "save":
        response = _put(client, job, [{"page_id": "P", "line_id": "L", "verdict": "accepted"}])
    elif activity == "activity":
        response = client.post(f"/api/jobs/{job}/reviews/activity")
    else:
        suffix = "/export" if activity == "export" else ""
        response = client.get(f"/api/jobs/{job}/reviews{suffix}")
    assert response.status_code in (200, 204)
    now[0] += 200
    assert store.sweep() == 0
    assert store.get_job(job) is not None
    now[0] += 3399
    assert store.sweep() == 0
    now[0] += 1
    assert store.sweep() == 1
    assert store.get_job(job) is None


def test_export_keeps_judgements_separate_from_approved_xml(client: TestClient, job: str) -> None:
    from app.schemas import JobStatus

    client.app.state.job_store.update_job(job, status=JobStatus.COMPLETED_WITH_REVIEW_REQUIRED)
    _put(client, job, [{"page_id": "P", "line_id": "L", "verdict": "refused"}])
    response = client.get(f"/api/jobs/{job}/reviews/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "reviews.json" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["export_version"] == 1
    assert payload["job_id"] == job
    assert payload["annotations_only"] is True
    assert payload["reviews"][0]["verdict"] == "refused"
    assert payload["reviews"][0]["reviewed_at"]
    assert payload["reviews"][0]["source_sha256"] is None
    assert payload["reviews"][0]["candidate_text"] is None
    assert payload["engine_report"] is None


def test_passive_review_reads_do_not_keep_an_idle_job_alive(client, job, monkeypatch):
    from app.jobs import store as store_module
    from app.schemas import JobStatus

    now = [1000.0]
    monkeypatch.setattr(store_module.time, "monotonic", lambda: now[0])
    store = client.app.state.job_store
    store.update_job(job, status=JobStatus.COMPLETED_WITH_REVIEW_REQUIRED)
    now[0] += 3500
    assert client.get(f"/api/jobs/{job}/reviews").status_code == 200
    assert client.get(f"/api/jobs/{job}").status_code == 200
    now[0] += 100
    assert store.sweep() == 1


def test_new_review_routes_require_the_job_token(client, job):
    import hashlib

    from app.schemas import JobStatus

    token = "review-owner-secret"
    client.app.state.job_store.update_job(
        job, status=JobStatus.COMPLETED, token_hash=hashlib.sha256(token.encode()).hexdigest()
    )
    for method, suffix in (("get", "export"), ("post", "activity")):
        url = f"/api/jobs/{job}/reviews/{suffix}"
        assert getattr(client, method)(url).status_code == 404
        response = getattr(client, method)(url, headers={"X-Job-Token": token})
        assert response.status_code in (200, 204)
        assert token not in response.text
        assert "token_hash" not in response.text


def test_export_and_activity_reject_unknown_jobs_and_export_waits_for_completion(client, job):
    assert client.post("/api/jobs/unknown/reviews/activity").status_code == 404
    assert client.get("/api/jobs/unknown/reviews/export").status_code == 404
    assert client.get(f"/api/jobs/{job}/reviews/export").status_code == 409
