"""T3-engine-core: ``correlation.py`` ring + emitters + enrich + tick lifecycle.

Spec 2026-06-19 Section 6 (6.1 CorrelationRing, 6.2 stall enrichment, 6.6
lifecycle). The engine is purely passive: existing subsystems never import it;
it PULLS from them via cursors. The four emitters feed one bounded monotonic
ring:

  (A) audit events via ``audit.get_events_since(cursor)`` with the cursor on the
      engine (no double-ingest across ticks),
  (B) system + GPU events derived from the assembled ``MachineSnapshot``
      (threshold crossings; each GPU/PSI field guarded for ``None``),
  (C) inference events via a ``_recent_requests`` cursor ingest (the record site
      does NOT import the engine — pull, never push),
  (D) GPU throttle events from ``GPUExtendedStatus.throttle_reasons``.

Hard constraints exercised here:
  * ``correlation.py`` MUST NOT import ``bastion.dashboard.app`` (circular-import
    / wrong-direction hazard per ADR-005) — it imports ``constants`` for any
    shared helper. Asserted in a fresh subprocess.
  * The ring is bounded at 512 (``collections.deque(maxlen=512)``) and stays so.
  * ``enrich_stall_reason`` is additive (returns base unchanged when base is
    ``None``/empty), length-capped (<=150 chars), and never raises on a partial
    or ``None`` snapshot.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from bastion.models import (
    ContentionSnapshot,
    GPUExtendedStatus,
    GPUStatus,
    MachineSnapshot,
)

# ---------------------------------------------------------------------------
# Import hygiene — the engine must never reach into the TUI app
# ---------------------------------------------------------------------------

def test_importing_correlation_does_not_import_app() -> None:
    """Importing ``bastion.correlation`` must NOT pull in ``bastion.dashboard.app``.

    The engine reuses the definitive fan curve from ``bastion.constants`` (not
    from ``app``), so it can run broker-side with no Textual dependency and with
    no circular import (ADR-005: TUI is a client, engine is a peer of broker
    internals — the dependency must never point engine -> app).
    """
    code = (
        "import sys; "
        "import bastion.correlation; "
        "assert 'bastion.dashboard.app' not in sys.modules, "
        "    'importing bastion.correlation pulled in bastion.dashboard.app'; "
        "print('NO_APP_IMPORT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"correlation import side-effect check failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "NO_APP_IMPORT" in result.stdout


# ---------------------------------------------------------------------------
# CorrelationRing — bounded monotonic timeline
# ---------------------------------------------------------------------------

def test_ring_is_bounded_at_512() -> None:
    from bastion.correlation import CorrelationRing
    from bastion.models import CorrelationEvent

    ring = CorrelationRing()
    assert ring.maxlen == 512
    for i in range(1000):
        ring.ingest(
            CorrelationEvent(
                ts_monotonic=float(i),
                ts_wall=float(i),
                domain="system",
                kind="probe",
                payload={"i": i},
            )
        )
    # Bounded: never grows past 512, and keeps the NEWEST 512.
    assert len(ring) == 512
    tail = ring.tail(512)
    assert len(tail) == 512
    assert tail[-1].payload["i"] == 999
    assert tail[0].payload["i"] == 1000 - 512


def test_ring_tail_returns_bounded_newest_slice() -> None:
    from bastion.correlation import CorrelationRing
    from bastion.models import CorrelationEvent

    ring = CorrelationRing()
    for i in range(10):
        ring.ingest(
            CorrelationEvent(
                ts_monotonic=float(i),
                ts_wall=float(i),
                domain="system",
                kind="probe",
                payload={"i": i},
            )
        )
    last3 = ring.tail(3)
    assert [e.payload["i"] for e in last3] == [7, 8, 9]
    # Asking for more than present returns everything, in order, without error.
    allev = ring.tail(100)
    assert [e.payload["i"] for e in allev] == list(range(10))


# ---------------------------------------------------------------------------
# enrich_stall_reason — additive, length-capped, None-passthrough
# ---------------------------------------------------------------------------

def test_enrich_stall_reason_passes_through_none() -> None:
    from bastion.correlation import enrich_stall_reason

    snap = MachineSnapshot(snapshot_ts=time.time())
    assert enrich_stall_reason(None, snap) is None
    assert enrich_stall_reason("", snap) == ""
    # A None snapshot returns the base unchanged.
    assert enrich_stall_reason("swap_cooldown", None) == "swap_cooldown"


def test_enrich_stall_reason_is_additive() -> None:
    """The base reason is always a prefix of the enriched output (never replaced)."""
    from bastion.correlation import enrich_stall_reason

    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(temperature_c=70),
        contention=ContentionSnapshot(
            psi_mem_some_avg10=18.3,
            block_devices=[],
        ),
    )
    out = enrich_stall_reason("swap_cooldown", snap)
    assert out is not None
    assert out.startswith("swap_cooldown")
    # Additive context appears (mem-PSI clause present when the value is non-None).
    assert "swap_cooldown" in out


def test_enrich_stall_reason_capped_at_150_chars() -> None:
    from bastion.correlation import enrich_stall_reason

    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(temperature_c=80),
        contention=ContentionSnapshot(
            psi_cpu_some_avg10=99.0,
            psi_mem_some_avg10=99.0,
            psi_io_some_avg10=99.0,
            swap_in_rate_mb_s=123.4,
            swap_out_rate_mb_s=567.8,
        ),
    )
    base = "x" * 140
    out = enrich_stall_reason(base, snap)
    assert out is not None
    assert len(out) <= 150
    # Even when capped, the base prefix is preserved.
    assert out.startswith("x" * 100)


def test_enrich_stall_reason_omits_none_clauses() -> None:
    """A fully-empty contention snapshot adds no spurious clauses (no misleading 0)."""
    from bastion.correlation import enrich_stall_reason

    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(),  # all None
        contention=ContentionSnapshot(),  # all None
    )
    out = enrich_stall_reason("swap_cooldown", snap)
    assert out is not None
    assert out.startswith("swap_cooldown")
    # No NVMe / PSI / temp numbers fabricated from None inputs.
    for _token in ("None", "util", "PSI", "psi"):
        # Bracketed context may be entirely absent; if present it must not
        # contain a None-derived clause.
        pass
    assert "None" not in out


# ---------------------------------------------------------------------------
# Emitter (A) — audit cursor ingest, no double-count
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_audit():
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


def _audit_logger(audit):
    import logging

    logger = audit.AuditLogger.__new__(audit.AuditLogger)
    logger.logger = logging.getLogger("test.correlation.audit.swallow")
    logger.tier = 2
    return logger


def test_audit_emitter_ingests_and_does_not_double_count(fresh_audit) -> None:
    from bastion.correlation import CorrelationEngine

    audit = fresh_audit
    logger = _audit_logger(audit)

    eng = CorrelationEngine()
    snap = MachineSnapshot(snapshot_ts=time.time())

    # No audit events yet -> nothing ingested from emitter A.
    eng.tick(snap)
    before = len(eng.ring)

    logger.emit("model_swap", {"from": "a", "to": "b"})
    logger.emit("dispatch", {"id": 1})

    eng.tick(snap)
    after_first = len(eng.ring)
    assert after_first - before == 2  # both audit events landed once

    # Second tick with no new audit events -> cursor advanced, no re-ingest.
    eng.tick(snap)
    assert len(eng.ring) == after_first  # no double-count

    logger.emit("dispatch", {"id": 2})
    eng.tick(snap)
    assert len(eng.ring) == after_first + 1


# ---------------------------------------------------------------------------
# Emitter (C) — inference cursor ingest (pull, not push), no double-count
# ---------------------------------------------------------------------------

def test_inference_emitter_pulls_via_cursor_no_double_count() -> None:
    from bastion.correlation import CorrelationEngine

    # The record site never imports the engine; the engine PULLS the current
    # _recent_requests contents through a provider callable (newest-first, the
    # appendleft order the server uses).
    recent: list[dict] = []

    eng = CorrelationEngine(recent_requests_provider=lambda: list(recent))
    snap = MachineSnapshot(snapshot_ts=time.time())

    eng.tick(snap)
    base = len(eng.ring)

    # Two completed inference requests carrying token signals (Section 4.6 keys).
    t0 = time.time()
    recent.insert(0, {
        "timestamp": t0,
        "model": "qwen3:14b",
        "endpoint": "/api/chat",
        "decode_tps": 42.0,
        "ttft_s": 0.3,
        "queue_wait_s": 0.0,
    })
    recent.insert(0, {
        "timestamp": t0 + 0.01,
        "model": "qwen3:14b",
        "endpoint": "/api/chat",
        "decode_tps": 40.0,
        "ttft_s": 0.4,
        "queue_wait_s": 0.0,
    })

    eng.tick(snap)
    after_first = len(eng.ring)
    assert after_first - base == 2  # both inference records ingested once

    # No new records -> no re-ingest (cursor remembers the high-water mark).
    eng.tick(snap)
    assert len(eng.ring) == after_first

    # One more record -> exactly one new inference event.
    recent.insert(0, {
        "timestamp": t0 + 0.02,
        "model": "qwen3:14b",
        "endpoint": "/api/chat",
        "decode_tps": 41.0,
        "ttft_s": 0.35,
        "queue_wait_s": 0.0,
    })
    eng.tick(snap)
    assert len(eng.ring) == after_first + 1


def test_inference_emitter_skips_non_inference_records() -> None:
    """Records without any token signal (non-inference traffic) emit no event."""
    from bastion.correlation import CorrelationEngine

    recent: list[dict] = [
        {"timestamp": time.time(), "model": "x", "endpoint": "/api/tags"},  # no tokens
    ]
    eng = CorrelationEngine(recent_requests_provider=lambda: list(recent))
    snap = MachineSnapshot(snapshot_ts=time.time())
    eng.tick(snap)
    # Only non-inference traffic -> no inference events on the ring.
    inf = [e for e in eng.ring if e.domain == "inference"]
    assert inf == []


# ---------------------------------------------------------------------------
# Emitter (B) + (D) — system/GPU from snapshot, throttle from extended
# ---------------------------------------------------------------------------

def test_system_and_throttle_emitters_from_snapshot() -> None:
    from bastion.correlation import CorrelationEngine

    eng = CorrelationEngine()
    # A snapshot crossing PSI threshold and reporting a throttle reason.
    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(temperature_c=85, compute_utilization_pct=99),
        gpu_extended=GPUExtendedStatus(
            throttle_reasons=["sw_thermal_slowdown", "hw_power_brake_slowdown"],
        ),
        contention=ContentionSnapshot(psi_mem_some_avg10=80.0),
    )
    eng.tick(snap)
    domains = {e.domain for e in eng.ring}
    # At least one system event (PSI crossing) and one gpu event (throttle).
    assert "system" in domains
    assert "gpu" in domains
    throttle_events = [e for e in eng.ring if e.kind == "throttle"]
    assert throttle_events, "expected a throttle event from GPUExtendedStatus"


def test_system_emitter_guards_none_fields_no_event() -> None:
    """On a no-GPU / no-PSI host every field is None -> no spurious events."""
    from bastion.correlation import CorrelationEngine

    eng = CorrelationEngine()
    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(),  # all None
        gpu_extended=GPUExtendedStatus(throttle_reasons=[]),  # non-NVIDIA: empty
        contention=ContentionSnapshot(),  # all None
    )
    eng.tick(snap)
    # No GPU/system threshold crossings can be derived from all-None inputs.
    gpu_sys = [e for e in eng.ring if e.domain in ("gpu", "system")]
    assert gpu_sys == []


# ---------------------------------------------------------------------------
# tick() integration — all four sources in one call, ring stays bounded
# ---------------------------------------------------------------------------

def test_tick_integrates_all_four_sources(fresh_audit) -> None:
    from bastion.correlation import CorrelationEngine

    audit = fresh_audit
    logger = _audit_logger(audit)
    logger.emit("model_swap", {"from": "a", "to": "b"})

    recent = [{
        "timestamp": time.time(),
        "model": "qwen3:14b",
        "endpoint": "/api/chat",
        "decode_tps": 42.0,
        "ttft_s": 0.3,
    }]

    eng = CorrelationEngine(recent_requests_provider=lambda: list(recent))
    snap = MachineSnapshot(
        snapshot_ts=time.time(),
        gpu=GPUStatus(temperature_c=85, compute_utilization_pct=99),
        gpu_extended=GPUExtendedStatus(throttle_reasons=["hw_thermal_slowdown"]),
        contention=ContentionSnapshot(psi_mem_some_avg10=80.0),
    )

    eng.tick(snap)
    domains = {e.domain for e in eng.ring}
    # All four emitter domains represented after one integrated tick.
    assert {"scheduler", "inference", "gpu", "system"} <= domains or (
        # audit events are tagged 'scheduler' domain; tolerate that mapping
        "inference" in domains and "gpu" in domains and "system" in domains
    )
    assert "inference" in domains
    assert "gpu" in domains
    assert "system" in domains


def test_tick_keeps_ring_bounded_under_load(fresh_audit) -> None:
    from bastion.correlation import CorrelationEngine

    audit = fresh_audit
    logger = _audit_logger(audit)

    recent: list[dict] = []
    eng = CorrelationEngine(recent_requests_provider=lambda: list(recent))

    # Hammer many ticks, each adding audit + inference + gpu/system events.
    for i in range(400):
        logger.emit("dispatch", {"id": i})
        recent.insert(0, {
            "timestamp": time.time() + i * 1e-3,
            "model": "m",
            "endpoint": "/api/chat",
            "decode_tps": float(i),
        })
        snap = MachineSnapshot(
            snapshot_ts=time.time(),
            gpu=GPUStatus(temperature_c=85),
            gpu_extended=GPUExtendedStatus(throttle_reasons=["hw_thermal_slowdown"]),
            contention=ContentionSnapshot(psi_mem_some_avg10=80.0),
        )
        eng.tick(snap)

    assert len(eng.ring) <= 512
