"""What this runner reads off a finished run, verified BEFORE the run.

`saknussemm` is installed from `git+…@main`, unpinned, and its
`__version__` does not move on every API change: it read `0.9.0` both
before and after `CorrectionResult.undeliverable_files` landed (library
PR #134, 2026-08-19). So an environment carrying a months-old engine is
indistinguishable from a current one by version, and a Docker layer that
installs `@main` is cached on its command string — rebuilding the image
after the library moved re-uses the old install without a word.

The symptoms an engine-too-old install produces split in two, and only
one half takes care of itself:

* everything this backend IMPORTS fails at import, i.e. at server
  startup, before any job exists. Loud enough.
* everything it reads as an ATTRIBUTE off the object `pipeline.run()`
  returns fails at the END of a run — every page corrected, every
  provider call paid for, and then a FAILED job with nothing to
  download. That is what `AttributeError: 'CorrectionResult' object has
  no attribute 'undeliverable_files'` was: a full volume lost at the
  last statement, to an incompatibility that was knowable at startup.

This module makes the second half behave like the first, and names
EVERY missing attribute at once rather than one per run.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from saknussemm import CorrectionResult

#: Reinstall recipe, kept identical to `backend/requirements.txt`.
#: `--force-reinstall` is not decoration: pip can consider a direct URL
#: requirement satisfied by the copy already installed, and the version
#: numbers match.
REINSTALL = (
    "pip install --force-reinstall --no-deps "
    "'saknussemm[vision] @ git+https://github.com/maribakulj/saknussemm@main'"
    "   (Docker: rebuild the saknussemm layer — "
    "`docker compose build --no-cache`, or pass "
    "`--build-arg SAKNUSSEMM_REF=<commit>`)"
)

#: Every attribute `JobRunner` reads off the run's `CorrectionResult`.
#: The rule for this list is mechanical, so it cannot become a matter of
#: taste: an attribute belongs here when the runner reads it AFTER
#: `pipeline.run()` has returned. Nested reads (`reconcile_metrics.total`,
#: `decisions.by_ref`) are not enumerated — they travel with the field
#: that carries them, and this guard is about the version gap, not about
#: type-checking the engine.
REQUIRED_RESULT_ATTRS = (
    "corrected_files",
    "decisions",
    "fallback_lines",
    "reconcile_metrics",
    "report",
    "retry_count",
    "total_chunks",
    "total_reconciled",
    "undeliverable_files",
)


class EngineTooOldError(RuntimeError):
    """The installed saknussemm predates what this backend reads."""


def missing_result_attrs(result_type: type = CorrectionResult) -> list[str]:
    """Names of `REQUIRED_RESULT_ATTRS` absent from `result_type`.

    Dataclass fields WITHOUT a default are not class attributes, so
    `hasattr` alone answers "no" for half a correct `CorrectionResult`.
    The field list is the truth; `hasattr` only covers the case where the
    engine stops being a dataclass.
    """
    if is_dataclass(result_type):
        declared = {f.name for f in fields(result_type)}
    else:  # pragma: no cover — defensive, the engine has always been one
        declared = set(getattr(result_type, "__annotations__", {}))
    return [
        name
        for name in REQUIRED_RESULT_ATTRS
        if name not in declared and not hasattr(result_type, name)
    ]


def require_engine_result_api(result_type: type = CorrectionResult) -> None:
    """Raise unless `result_type` carries everything the runner reads."""
    missing = missing_result_attrs(result_type)
    if not missing:
        return
    raise EngineTooOldError(
        f"the installed saknussemm is older than this backend: "
        f"{result_type.__name__} is missing "
        f"{', '.join(missing)}. Reading those is the last thing a job "
        f"does, so an unchecked run would have corrected every page "
        f"before failing. Update the engine:\n    " + REINSTALL
    )
