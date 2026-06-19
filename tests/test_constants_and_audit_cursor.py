"""T2-engine-scaffolding: shared ``constants.py`` + audit cursor feed.

Prerequisites for the correlation engine (spec 2026-06-19, Sections 6.2/6.6):

* ``bastion.constants`` holds ``_fan_band`` + ``_AUTO_FAN_HYSTERESIS_C`` so the
  (future) ``correlation.py`` can reuse the definitive fan curve **without**
  importing ``bastion.dashboard.app`` (circular-import / wrong-direction hazard
  per ADR-005). Importing ``bastion.constants`` must therefore NOT drag in the
  Textual app module.
* ``bastion.dashboard.app`` keeps working by importing those names from
  ``constants`` (no behavioural change to the auto-fan curve).
* ``audit.get_events_since(cursor)`` returns only events appended since the
  given monotonic sequence number plus the new cursor, backed by a strictly
  monotonic ``_event_seq`` counter incremented on EVERY ``_recent_events``
  append. The cursor is a sequence number, not a deque index, so it is stable
  across ring wraps and external mutation of ``_recent_events``.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# constants.py — shared fan curve, no app side effects
# ---------------------------------------------------------------------------

def test_constants_module_exposes_fan_band_and_hysteresis() -> None:
    import bastion.constants as constants

    assert hasattr(constants, "_fan_band")
    assert callable(constants._fan_band)
    assert hasattr(constants, "_AUTO_FAN_HYSTERESIS_C")
    assert constants._AUTO_FAN_HYSTERESIS_C == 5.0


def test_fan_band_curve_unchanged() -> None:
    """The escalation curve preserves the operator-spec'd bands exactly."""
    from bastion.constants import _fan_band

    assert _fan_band(90.0) == "100"   # > 85 (exclusive top band)
    assert _fan_band(85.0) == "90"    # >= 80, not > 85
    assert _fan_band(80.0) == "90"
    assert _fan_band(70.0) == "50"
    assert _fan_band(60.0) == "30"
    assert _fan_band(59.9) is None    # below 60 -> BIOS auto
    assert _fan_band(0.0) is None


def test_importing_constants_does_not_import_app() -> None:
    """``bastion.constants`` must be reusable by the engine without pulling in
    the Textual TUI app (circular-import hazard the constants split exists to
    avoid)."""
    code = (
        "import sys; "
        "import bastion.constants; "
        "assert 'bastion.dashboard.app' not in sys.modules, "
        "    'importing bastion.constants pulled in bastion.dashboard.app'; "
        "print('NO_APP_IMPORT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"constants import side-effect check failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "NO_APP_IMPORT" in result.stdout


def test_app_still_imports_and_reexports_fan_band() -> None:
    """``app.py`` keeps the names working (imported from constants) so the TUI
    auto-fan logic and any back-compat references continue to resolve."""
    import bastion.constants as constants
    import bastion.dashboard.app as app

    assert hasattr(app, "_fan_band")
    assert hasattr(app, "_AUTO_FAN_HYSTERESIS_C")
    # Same object — app re-uses the single source of truth, not a copy.
    assert app._fan_band is constants._fan_band
    assert app._AUTO_FAN_HYSTERESIS_C == constants._AUTO_FAN_HYSTERESIS_C
    # And the class still constructs (app remains importable + usable).
    assert app.BastionDashboard.TITLE == "BASTION Dashboard"


# ---------------------------------------------------------------------------
# audit.get_events_since — monotonic cursor feed for the engine
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_audit():
    """Isolate the module-level audit ring + seq counter per test."""
    from bastion import audit

    saved_events = list(audit._recent_events)
    saved_seq = audit._event_seq
    audit._recent_events.clear()
    audit._event_seq = 0
    try:
        yield audit
    finally:
        audit._recent_events.clear()
        audit._recent_events.extend(saved_events)
        audit._event_seq = saved_seq


def test_get_events_since_returns_only_new(fresh_audit) -> None:
    audit = fresh_audit
    logger = audit.AuditLogger.__new__(audit.AuditLogger)  # avoid file handler
    # Emit via the convenience wrapper would need init; append directly through
    # the public-ish path by calling the module emit after stubbing the logger.
    audit._audit_logger = None  # force preinit buffering off-path; use direct

    # Drive appends through the real append path (AuditLogger.emit) using a
    # logger whose .logger swallows output.
    import logging

    logger.logger = logging.getLogger("test.audit.swallow")
    logger.tier = 2

    events0, cur0 = audit.get_events_since(0)
    assert events0 == []
    assert cur0 == 0

    logger.emit("a", {"i": 1})
    logger.emit("b", {"i": 2})

    new_events, cur1 = audit.get_events_since(cur0)
    assert [e["event"] for e in new_events] == ["a", "b"]
    assert cur1 == 2

    # No further appends -> empty slice, cursor unchanged.
    none_events, cur2 = audit.get_events_since(cur1)
    assert none_events == []
    assert cur2 == cur1 == 2

    logger.emit("c", {"i": 3})
    tail, cur3 = audit.get_events_since(cur1)
    assert [e["event"] for e in tail] == ["c"]
    assert cur3 == 3


def test_event_seq_strictly_monotonic_across_appends(fresh_audit) -> None:
    audit = fresh_audit
    import logging

    logger = audit.AuditLogger.__new__(audit.AuditLogger)
    logger.logger = logging.getLogger("test.audit.swallow")
    logger.tier = 2

    seqs = []
    for i in range(5):
        logger.emit("e", {"i": i})
        seqs.append(audit._event_seq)
    # Strictly increasing, one per append.
    assert seqs == [1, 2, 3, 4, 5]
    assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))


def test_get_events_since_stable_across_ring_wrap(fresh_audit) -> None:
    """Cursor is a sequence number, not a deque index: it survives the ring
    discarding its left end (and external mutation), returning the correct
    *newest* slice rather than drifting."""
    audit = fresh_audit
    import logging

    logger = audit.AuditLogger.__new__(audit.AuditLogger)
    logger.logger = logging.getLogger("test.audit.swallow")
    logger.tier = 2

    maxlen = audit._recent_events.maxlen
    assert maxlen is not None
    # Overflow the ring well past its capacity.
    total = maxlen + 10
    for i in range(total):
        logger.emit("x", {"i": i})

    # _event_seq counts ALL appends, even discarded ones.
    assert audit._event_seq == total

    # Ask for everything since a cursor that predates the discarded events:
    # we can only return what the bounded ring still holds (the newest maxlen),
    # but the returned cursor must be the true latest seq and the slice must be
    # the ring tail in order — no crash, no negative index, no duplication.
    events, cur = audit.get_events_since(0)
    assert cur == total
    assert len(events) == maxlen
    assert events[-1]["details"]["i"] == total - 1
    assert events[0]["details"]["i"] == total - maxlen
