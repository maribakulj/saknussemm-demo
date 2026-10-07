---
title: Saknussemm
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# saknussemm-demo

**A web demonstration of [`saknussemm`](https://github.com/maribakulj/saknussemm)** — the
structure-safe post-OCR correction library for ALTO and PAGE XML.

Upload an ALTO or PAGE file, watch the engine correct it line by line, and
read the report it produces. That is all this repository is for.

Some delivered corrections are marked `review_required`: the engine retains
the proposed text and asks a person to check it against the scan. These jobs
finish as **completed with review required**, expose the affected lines and
reasons, and download as **candidate XML for review**. The existing review
panel records acceptance, refusal or a transcription; those judgements do
not edit the downloaded XML or certify it as approved. Files withheld by the
engine remain explicitly listed as missing from the output.

## What this is not

It is **not** the deliverable, and it is not where the interesting
guarantees live. The library is: it never merges lines, it falls back to
the source rather than guess, and it accounts for every alteration it
makes. This repository is a browser front door to that behaviour.

Nor is it the benchmark. Comparing transcription pipelines — CER, WER,
hallucination, cost, significance testing — is
[`cinoc`](https://github.com/maribakulj/cinoc).

The dependency runs **one way only**: the demo imports the library, never
the reverse. A demo need that seems to require the library to know about
this application is either a missing injection point — fix it generically,
in the library — or out of scope.

## Layout

| | |
|---|---|
| `backend/` | FastAPI: job lifecycle, SSE, capability tokens, provider adapters (OpenAI, Anthropic, Mistral, Google) |
| `frontend/` | React + TypeScript + Vite + Tailwind |
| `Dockerfile` | single container for Hugging Face Spaces (port 7860) |
| `docker-compose.yml` | local dev stack — backend :8000, frontend :5173 |
| `examples/` | the ALTO and PAGE fixtures the backend suite reads |
| `docs/API.md` | the HTTP surface |
| `SECURITY.md` | **read before deploying** — the deployment profiles, and what each one does and does not protect |

## Running it

```bash
# The library is not published yet. This reviewed commit is shared by
# CI and both Dockerfile defaults (saknussemm PR #178). The
# [vision] extra is Pillow, and this backend needs it: without it every
# vision job fails at the first crop.
pip install 'saknussemm[vision] @ git+https://github.com/maribakulj/saknussemm@50bb6e58f8baa24dbd66f34e655ec071fb3f7d54'
# After changing this commit in an existing environment, add
# --force-reinstall --no-deps: the version number may stay unchanged.
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd backend && uvicorn app.main:app --reload --port 8000

cd frontend && npm install && npm run dev          # :5173
```

Or the whole stack at once: `docker-compose up`.

```bash
cd backend && pytest -m "not e2e"    # 462 tests, coverage gate 80% on `app`
cd backend && pytest tests/e2e       # real uvicorn + a fake provider
cd frontend && npx vitest run && npx tsc --noEmit && npm run lint
```

## Security, in one paragraph

The default profile is `demo`: **no authentication**, and both the
documents you submit and the LLM API key you supply transit through the
server. Per-job isolation rests on a capability token returned once at
creation, carried in a header and never in a URL. Do not put sensitive
documents or a valuable key into a public deployment. `SECURITY.md` says
exactly what each profile asserts, and what it does not.

## Why it has its own repository

It used to live beside the library. It moved out on 2026-08-16 so the
library could be packaged, versioned and published as one thing rather
than one thing among several — and so the two would stop sharing a CI, a
security document and a release cadence they never had in common.

The history came with it: 282 commits, filtered to the paths that are
actually this application. The library was called `corrigenda` until the
same day; nothing in this repository carries the old name.
