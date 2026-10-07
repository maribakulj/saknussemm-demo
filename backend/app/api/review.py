"""Human review — where a reader's judgement on a line is recorded.

The corpora this demo runs on have **no ground truth**. Gallica's `text.txt`
is the same OCR layer as its ALTO, so nothing on disk can say whether a
correction was right or a refusal was justified; the only source of truth is
a person reading the scan. This module is where that reading is kept.

**Why it is worth keeping rather than just displaying.** A reviewer who can
only look produces an impression. A reviewer who can adjudicate produces the
dataset that is missing: `(line, what the OCR read, what the model proposed,
what a human says the scan actually shows)`. That is exactly the input a
bench needs to answer "was the correction right", which no amount of
measurement inside the engine can answer on its own.

**Three verdicts, and the third is the useful one.** `accepted` and
`refused` grade what the engine did. `transcribed` carries what the reviewer
read on the image, which stands on its own whatever the engine decided — and
accumulates into ground truth line by line.

Reviews are keyed by ``(page_id, line_id)`` because a line id repeats across
files (`ADR-001`); keying on the bare id would silently merge two documents'
judgements the first time a job carries more than one ALTO.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import get_job_store
from app.api.jobs import require_job_access
from app.protocols import JobStore
from app.schemas import JobManifest
from app.schemas.job import TERMINAL_SUCCESS_STATES

router = APIRouter(prefix="/api/jobs", tags=["review"])


class ReviewVerdict(StrEnum):
    """What the reader concluded about the engine's decision on this line."""

    #: The engine's outcome is right — whether it corrected or refused.
    ACCEPTED = "accepted"
    #: The engine's outcome is wrong. `note` should say how.
    REFUSED = "refused"
    #: Neither: the reader is recording what the scan actually shows.
    TRANSCRIBED = "transcribed"


class LineReview(BaseModel):
    """One reader's judgement on one line."""

    page_id: str = Field(min_length=1, max_length=256)
    line_id: str = Field(min_length=1, max_length=256)
    verdict: ReviewVerdict
    #: What the reader read on the image. Required for ``transcribed``; free
    #: to accompany the other two when the reader wants to be precise about
    #: what the engine got wrong.
    transcription: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=2000)
    reviewed_at: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.page_id, self.line_id)


class ReviewBatch(BaseModel):
    """Reviews arrive in batches: a reader works through a page, not a line."""

    reviews: list[LineReview] = Field(max_length=2000)


class ReviewsResponse(BaseModel):
    job_id: str
    reviews: list[LineReview]


def _key(review: LineReview) -> str:
    """``(page_id, line_id)`` flattened for storage, NUL-separated.

    A NUL cannot occur in an XML id, so the pair round-trips unambiguously; a
    space could, and would merge two lines the day a producer emits one.
    """
    return f"{review.page_id}\x00{review.line_id}"


def _existing(job: JobManifest) -> dict[str, LineReview]:
    """Reviews already on the job, back as models.

    The manifest stores plain dicts so the schema layer never imports the API
    layer's models; re-validating here is what keeps that separation from
    costing type safety at the edge.
    """
    return {key: LineReview.model_validate(raw) for key, raw in (job.reviews or {}).items()}


@router.put("/{job_id}/reviews", response_model=ReviewsResponse)
async def put_reviews(
    job_id: str,
    batch: ReviewBatch,
    job: JobManifest = Depends(require_job_access),
    store: JobStore = Depends(get_job_store),
) -> ReviewsResponse:
    """Record or replace judgements on lines of this job.

    Idempotent per line: sending the same line twice replaces its review
    rather than appending, so a reader who changes their mind is not fighting
    an append-only log. The timestamp is stamped here rather than trusted
    from the client — a review's date is a fact about the server.
    """
    for review in batch.reviews:
        if review.verdict is ReviewVerdict.TRANSCRIBED and not review.transcription:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"line {review.line_id!r}: a 'transcribed' review must carry the "
                "text the reader read on the image — that text IS the review.",
            )
    if job.document_manifest is not None:
        known = {
            (page.page_id, line.line_id)
            for page in job.document_manifest.pages
            for line in page.lines
        }
        if any(review.key not in known for review in batch.reviews):
            raise HTTPException(422, "A review refers to a page or line outside this job.")

    merged = _existing(job)
    stamped = datetime.now(UTC).isoformat(timespec="seconds")
    for review in batch.reviews:
        merged[f"{_key(review)}"] = review.model_copy(update={"reviewed_at": stamped})
    store.update_job(job_id, reviews={k: v.model_dump() for k, v in merged.items()})
    return ReviewsResponse(
        job_id=job_id, reviews=sorted(merged.values(), key=lambda r: (r.page_id, r.line_id))
    )


@router.get("/{job_id}/reviews", response_model=ReviewsResponse)
async def get_reviews(
    job_id: str,
    job: JobManifest = Depends(require_job_access),
) -> ReviewsResponse:
    return ReviewsResponse(
        job_id=job_id,
        reviews=sorted(_existing(job).values(), key=lambda r: (r.page_id, r.line_id)),
    )


@router.post("/{job_id}/reviews/activity", status_code=204)
async def review_activity(
    job: JobManifest = Depends(require_job_access),
    store: JobStore = Depends(get_job_store),
) -> Response:
    """A reader interacted with the review UI; renew its idle timeout."""
    store.touch_review(job.job_id)
    return Response(status_code=204)


def _export_response(job: JobManifest) -> JSONResponse:
    """Build a portable annotation snapshot without changing candidate XML."""
    report = job.report
    provenance = report.provenance if report is not None else None
    digests = provenance.source_digests if provenance is not None else {}
    pages = (
        {page.page_id: page for page in job.document_manifest.pages}
        if job.document_manifest is not None
        else {}
    )
    outcomes = {(line.page_id, line.line_id): line for line in report.lines} if report else {}
    rows = []
    for review in sorted(_existing(job).values(), key=lambda r: r.key):
        page = pages.get(review.page_id)
        outcome = outcomes.get(review.key)
        source_file = page.source_file if page is not None else None
        rows.append(
            {
                **review.model_dump(mode="json"),
                "source_file": source_file,
                "source_sha256": digests.get(source_file) if source_file is not None else None,
                "source_text": outcome.source_text if outcome is not None else None,
                "candidate_text": outcome.decision.final_text if outcome is not None else None,
            }
        )
    return JSONResponse(
        {
            "export_version": 1,
            "job_id": job.job_id,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "annotations_only": True,
            "reviews": rows,
            # Includes input digests, engine/policy versions and exact candidate
            # outcomes. Never serialize JobManifest (it carries a token hash).
            "engine_report": report.model_dump(mode="json", exclude_none=True) if report else None,
        },
        headers={"Content-Disposition": f'attachment; filename="job_{job.job_id}_reviews.json"'},
    )


@router.get("/{job_id}/reviews/export")
async def export_reviews(
    job: JobManifest = Depends(require_job_access),
    store: JobStore = Depends(get_job_store),
) -> Response:
    """Export saved annotations and their source/candidate evidence."""
    if job.status not in TERMINAL_SUCCESS_STATES:
        raise HTTPException(409, "Wait for the job to complete before exporting reviews.")
    store.touch_review(job.job_id)
    return await asyncio.to_thread(_export_response, job)


__all__ = ["LineReview", "ReviewBatch", "ReviewVerdict", "router"]
