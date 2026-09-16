"""An engine too old to finish a run must say so before the run starts.

The failure this guards against was observed, not imagined: a demo
running against a pre-2026-08-19 saknussemm corrected all 116 lines of
its page, spent every provider call, and then died on
``'CorrectionResult' object has no attribute 'undeliverable_files'`` —
the last statement of the job. Nothing was downloadable and the event
log ended in ``Failed``.

Three things keep that from coming back, and this file holds all three:
the guard fires, it names EVERY missing attribute at once, and its list
still matches what ``runner.py`` actually reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.jobs.engine_contract import (
    REQUIRED_RESULT_ATTRS,
    EngineTooOldError,
    missing_result_attrs,
    require_engine_result_api,
)

RUNNER_SOURCE = Path(__file__).resolve().parent.parent / "app" / "jobs" / "runner.py"


def test_the_installed_engine_satisfies_the_contract():
    """The real `CorrectionResult`, as installed — no stub, no mock."""
    assert missing_result_attrs() == []
    require_engine_result_api()  # must not raise


def test_a_stale_result_is_refused_and_every_gap_is_named_at_once():
    """One run, every missing attribute — not one bug report per field."""

    @dataclass
    class StaleResult:
        # A plausible pre-#134 shape: everything the runner reads except
        # the two attributes that landed later.
        total_chunks: int = 0
        total_reconciled: int = 0
        fallback_lines: int = 0
        retry_count: int = 0
        reconcile_metrics: object = None
        decisions: object = None
        report: object = None
        corrected_files: dict = field(default_factory=dict)

    assert missing_result_attrs(StaleResult) == ["undeliverable_files"]

    with pytest.raises(EngineTooOldError) as excinfo:
        require_engine_result_api(StaleResult)

    message = str(excinfo.value)
    assert "undeliverable_files" in message
    # Actionable, not merely correct: the message carries the command
    # that fixes it, including the --force-reinstall that a matching
    # version number would otherwise let pip skip.
    assert "--force-reinstall" in message
    assert "git+https://github.com/maribakulj/saknussemm" in message


def test_every_gap_is_named_in_one_message():
    @dataclass
    class VeryStaleResult:
        total_chunks: int = 0

    missing = missing_result_attrs(VeryStaleResult)
    assert len(missing) > 1
    with pytest.raises(EngineTooOldError) as excinfo:
        require_engine_result_api(VeryStaleResult)
    message = str(excinfo.value)
    for name in missing:
        assert name in message


def test_the_guard_list_covers_what_the_runner_actually_reads():
    """The ratchet: a new `result.<attr>` read must join the list.

    Without this, the guard would protect the 2026-09 set of attributes
    forever while the runner grew new ones — which is exactly how the
    first one got read without ever being checked.
    """
    read_by_runner = set(re.findall(r"\bresult\.([a-z_]+)", RUNNER_SOURCE.read_text()))
    assert read_by_runner, "regex found nothing — did `result` get renamed?"
    assert read_by_runner <= set(REQUIRED_RESULT_ATTRS), (
        "runner.py reads engine attributes the startup guard does not check: "
        f"{sorted(read_by_runner - set(REQUIRED_RESULT_ATTRS))}"
    )
