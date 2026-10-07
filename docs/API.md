# Backend HTTP API — the demo's, not the library's

> **Scope.** This describes the FastAPI app that demonstrates `saknussemm`
> in a browser. It is not part of the published library and retires with
> the demo: a library has no endpoints, no job store and no tokens. If you
> are integrating `saknussemm` into your own service, none of this is your
> contract — [`docs/quickstart.md`](https://github.com/maribakulj/saknussemm/blob/main/docs/quickstart.md)
> is. Read on if you are running or modifying the demo.

**Source of truth: the OpenAPI schema**, served live at `/docs` and
`/openapi.json`, and committed as `frontend/openapi.snapshot.json`
(CI regenerates it and fails on drift — see
`scripts/generate-frontend-api-types.sh`). This page is a map, not a
second contract; when in doubt, the schema wins.

## Authentication model

`POST /api/jobs` returns a **capability token** once (`job_token`);
only its SHA-256 hash is stored. Every job endpoint requires it via the
`X-Job-Token` header. Missing/wrong token → **404** (job existence
never leaks). The token is **never accepted in a URL**.

Header-less surfaces use short-lived signed credentials (`?sig=`),
scoped to one job and one purpose:

- **events** — `POST /api/jobs` also returns `events_url`
  (an events-scoped `?sig=` valid for the run's timeout budget), for
  `EventSource`.
- **images** — `GET …/layout` appends a 15-minute images-scoped
  `?sig=` to each `image_url`, for `<img>`.

## Routes

| Route | Purpose |
|---|---|
| `POST /api/providers/models` | List models for a provider + API key |
| `POST /api/jobs` | Multipart upload (`files`, `provider`, `api_key`, `model`, optional `geometric_pairing`) → `{job_id, job_token, events_url}` |
| `GET /api/jobs/{id}` | Authoritative status snapshot (`JobStatusResponse`) |
| `POST /api/jobs/{id}/cancel` | Cooperative cancellation — idempotent, 202, body = current status |
| `GET /api/jobs/{id}/events` | SSE stream (auth via `?sig=` or header) |
| `GET /api/jobs/{id}/download` | Corrected XML (single file) or ZIP |
| `GET /api/jobs/{id}/trace` | The run's versioned `CorrectionReport` (per-line traces) |
| `GET /api/jobs/{id}/diff` | OCR vs corrected, per page/line |
| `GET /api/jobs/{id}/layout` | Blocks/lines with ALTO coordinates + signed `image_url`s |
| `GET /api/jobs/{id}/images/{name}` | Source scan image (auth via `?sig=` or header) |
| `GET /api/jobs/{id}/reviews` | Recorded human judgements, keyed by page and line |
| `PUT /api/jobs/{id}/reviews` | Record `accepted`, `refused` or `transcribed` judgements; does not rewrite XML |
| `GET /health`, `/health/live`, `/health/ready` | Probes — `ready` includes storage, frontend (when promised) and load gauges |

Job state machine: `queued → started → running → completed |
completed_with_fallbacks | completed_with_review_required |
completed_with_withheld_files | failed`, plus `cancel_requested → cancelled`.
Terminal jobs are evicted after a TTL (default 1 h); artefacts live
under `{JOB_STORAGE_DIR}/{job_id}/` (`input/`, `output/`, `images/`).

The status snapshot and terminal SSE payload carry `review_lines`,
`review_reasons` (counts by code), and `withheld_files` (source filename to
explanation). Withheld files take precedence over review referrals, which
take precedence over fallbacks; all counters remain available independently.
The layout includes every line's structured `review_reasons` for the review
queue. A referral retains the engine's proposed correction in the XML.

Downloads remain available for human review. When `review_lines > 0`, their
filenames contain `candidate`, and the response carries
`X-Saknussemm-Review-Lines`. Recorded human judgements never change these
bytes or remove this label: a refusal does not restore the source, and a
transcription does not replace the XML text. This demo has no approved-output
publication step.

## SSE events

Event names are defined by `saknussemm.core.schemas.PipelineEventType`
(the enum is the wire contract) and mirrored by
`frontend/src/hooks/useJobStream.ts::EVENTS`;
`backend/tests/test_sse_event_contract.py` fails CI on any drift.
Terminal events (`completed`, `failed`, `cancelled`) have guaranteed
delivery and are synthesised for late subscribers.
