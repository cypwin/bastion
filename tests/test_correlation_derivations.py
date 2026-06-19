"""T4-engine-derivations: ``correlation.py`` contention detector + RiskIndex + thermal coupling.

Spec 2026-06-19 Sections 6.3 (ContentionEventDetector), 6.4 (RiskIndex), 6.5
(CPU<->GPU thermal coupling). All three are derivations layered on top of the
already-collected ``MachineSnapshot`` signals; none issues I/O of its own.

Contracts exercised here:

  6.3 ContentionEventDetector
    * TWO separate unit thresholds, both from ``CorrelationConfig``: block-device
      write throughput (``contention_block_write_mb_s_threshold``, MB/s) and PSI
      (``contention_psi_threshold``, on ``psi_mem_some_avg10``).
    * Edge detection + a 2-tick hysteresis (``contention_hysteresis_ticks``): a
      single over-threshold tick fires nothing; the event fires only after the
      condition holds for two consecutive ticks.
    * The coincidence join is the moat: an event fires ONLY when a threshold
      crossing coincides with an active inference stall. Over-threshold with no
      stall -> no event (htop sees the IO; only BASTION sees the coincidence).
    * Capped at 50 in a ``deque(maxlen=50)``.
    * PSI / disk legs degrade independently when one input is ``None``.

  6.4 compute_risk_index
    * ``score`` always in [0, 1] for any input, incl. all-None -> 0.0/nominal.
    * ``dominant_factor`` is always one of the 5 component names.
    * A ``None`` component contributes 0 to its own term without crashing (the
      term is *absent*, not a misleading zero-risk reading).

  6.5 build_thermal_coupling
    * ``coupling_active == (cpu_temp_c is not None and _fan_band(cpu_temp_c) is
      not None)`` — derived from the definitive fan curve, never a duplicated
      constant.
    * ``thermal_headroom_min_c`` uses ``cpu_safe_ceiling_c`` (default 85, NOT 60)
      and the GPU ceiling, skipping the GPU term when ``gpu_temp_c`` is ``None``.
"""
from __future__ import annotations

import time

import pytest

from bastion.constants import _fan_band
from bastion.models import (
    BlockDeviceIOStats,
    ContentionEvent,
    ContentionSnapshot,
    CorrelationConfig,
    MachineSnapshot,
    RiskIndexResult,
    ThermalCoupling,
)

RISK_COMPONENTS = {
    "vram_headroom",
    "thermal_headroom",
    "swap_rate",
    "thrashing",
    "memory_psi",
}


def _snap(*, write_mb_s: float | None = None, psi_mem: float | None = None) -> MachineSnapshot:
    """A MachineSnapshot whose busiest block device writes ``write_mb_s`` MB/s."""
    block_devices: list[BlockDeviceIOStats] = []
    if write_mb_s is not None:
        block_devices = [
            BlockDeviceIOStats(
                device="nvme0n1",
                util_pct=90.0,
                read_rate_mb_s=0.0,
                write_rate_mb_s=write_mb_s,
            )
        ]
    return MachineSnapshot(
        snapshot_ts=time.time(),
        contention=ContentionSnapshot(
            psi_mem_some_avg10=psi_mem,
            block_devices=block_devices,
        ),
    )


# ---------------------------------------------------------------------------
# 6.3 ContentionEventDetector — coincidence join + hysteresis + cap
# ---------------------------------------------------------------------------


def test_detector_two_unit_thresholds_from_config() -> None:
    """The detector reads BOTH thresholds from CorrelationConfig (no hard-codes)."""
    from bastion.correlation import ContentionEventDetector

    cfg = CorrelationConfig(
        contention_block_write_mb_s_threshold=123.0,
        contention_psi_threshold=33.0,
        contention_hysteresis_ticks=2,
    )
    det = ContentionEventDetector(config=cfg)
    assert det.write_mb_s_threshold == 123.0
    assert det.psi_threshold == 33.0
    assert det.hysteresis_ticks == 2


def test_detector_single_tick_no_event() -> None:
    """One over-threshold tick fires nothing (hysteresis requires two)."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    ev = det.feed(_snap(write_mb_s=10_000.0), inference_stalled=True, stall_reason="swap_cooldown")
    assert ev is None
    assert list(det.recent_contentions) == []


def test_detector_fires_only_after_two_ticks_with_stall() -> None:
    """Edge + 2-tick hysteresis: fires on the SECOND consecutive over-threshold stall tick."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    snap = _snap(write_mb_s=10_000.0)
    # Tick 1: condition holds but hysteresis not yet satisfied.
    assert det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown") is None
    # Tick 2: condition still holds AND stalled -> fire.
    ev = det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown")
    assert isinstance(ev, ContentionEvent)
    assert ev.inference_was_stalled is True
    assert ev.stall_reason_at_time == "swap_cooldown"
    assert len(det.recent_contentions) == 1


def test_detector_no_event_when_not_stalled_even_over_threshold() -> None:
    """The coincidence join is the contract: over-threshold WITHOUT a stall -> no event."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    snap = _snap(write_mb_s=10_000.0)
    # Hold the condition for many ticks, but never stalled.
    for _ in range(5):
        ev = det.feed(snap, inference_stalled=False, stall_reason=None)
        assert ev is None
    assert list(det.recent_contentions) == []


def test_detector_no_event_when_stall_reason_empty() -> None:
    """A stall flag with an empty reason is not an inference stall -> no event."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    snap = _snap(write_mb_s=10_000.0)
    det.feed(snap, inference_stalled=False, stall_reason="")
    ev = det.feed(snap, inference_stalled=False, stall_reason="")
    assert ev is None
    assert list(det.recent_contentions) == []


def test_detector_psi_leg_fires_independently() -> None:
    """The PSI leg fires on mem-PSI over threshold + stall, with no block device at all."""
    from bastion.correlation import ContentionEventDetector

    cfg = CorrelationConfig(contention_psi_threshold=20.0)
    det = ContentionEventDetector(config=cfg)
    # No block devices (disk leg input None); only PSI is over threshold.
    snap = _snap(psi_mem=80.0)
    assert det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown") is None
    ev = det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown")
    assert isinstance(ev, ContentionEvent)
    assert "mem" in ev.attribution.lower() or "psi" in ev.attribution.lower()


def test_detector_disk_leg_fires_independently_when_psi_none() -> None:
    """The disk leg fires when PSI is None (old kernel) but write throughput is high."""
    from bastion.correlation import ContentionEventDetector

    cfg = CorrelationConfig(contention_block_write_mb_s_threshold=200.0)
    det = ContentionEventDetector(config=cfg)
    snap = _snap(write_mb_s=5_000.0, psi_mem=None)  # PSI absent
    assert det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown") is None
    ev = det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown")
    assert isinstance(ev, ContentionEvent)


def test_detector_below_threshold_no_event() -> None:
    """Below both thresholds -> no event, even while stalled (nothing to attribute)."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    snap = _snap(write_mb_s=1.0, psi_mem=0.1)
    for _ in range(4):
        assert det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown") is None
    assert list(det.recent_contentions) == []


def test_detector_recent_contentions_capped_at_50() -> None:
    """The dedicated deque is bounded at 50 across many fire cycles."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    assert det.recent_contentions.maxlen == 50
    over = _snap(write_mb_s=10_000.0)
    clear = _snap(write_mb_s=0.0)
    # Each cycle: prime (tick1), fire (tick2), then drop below to reset the edge.
    for _ in range(120):
        det.feed(over, inference_stalled=True, stall_reason="swap_cooldown")
        det.feed(over, inference_stalled=True, stall_reason="swap_cooldown")
        det.feed(clear, inference_stalled=False, stall_reason=None)
    assert len(det.recent_contentions) == 50


def test_detector_partial_snapshot_never_raises() -> None:
    """A None contention block (no host data) degrades to no event, never an exception."""
    from bastion.correlation import ContentionEventDetector

    det = ContentionEventDetector(config=CorrelationConfig())
    snap = MachineSnapshot(snapshot_ts=time.time(), contention=None)
    assert det.feed(snap, inference_stalled=True, stall_reason="swap_cooldown") is None
    assert det.feed(None, inference_stalled=True, stall_reason="swap_cooldown") is None


# ---------------------------------------------------------------------------
# 6.4 compute_risk_index — score in [0,1], dominant_factor one of 5, None-tolerant
# ---------------------------------------------------------------------------


def test_risk_index_all_none_is_nominal_zero() -> None:
    from bastion.correlation import compute_risk_index

    res = compute_risk_index(
        vram_utilization_pct=None,
        thermal_headroom_c=None,
        swap_rate_level=None,
        thrashing_verdict=None,
        memory_psi=None,
        config=CorrelationConfig(),
    )
    assert isinstance(res, RiskIndexResult)
    assert res.score == 0.0
    assert res.level == "nominal"
    assert res.dominant_factor in RISK_COMPONENTS


@pytest.mark.parametrize(
    "vram,thermal,swap,thrash,psi",
    [
        (0.0, 0.0, "normal", "ok", 0.0),
        (100.0, 0.0, "critical", "halt", 100.0),
        (50.0, 5.0, "warn", "warn", 40.0),
        (95.0, None, "critical", None, None),
        (None, 60.0, None, "ok", 10.0),
        (130.0, -10.0, "critical", "halt", 250.0),  # out-of-range inputs clamp
    ],
)
def test_risk_index_score_in_unit_interval(vram, thermal, swap, thrash, psi) -> None:
    from bastion.correlation import compute_risk_index

    res = compute_risk_index(
        vram_utilization_pct=vram,
        thermal_headroom_c=thermal,
        swap_rate_level=swap,
        thrashing_verdict=thrash,
        memory_psi=psi,
        config=CorrelationConfig(),
    )
    assert 0.0 <= res.score <= 1.0
    assert res.dominant_factor in RISK_COMPONENTS
    assert res.level in {"nominal", "elevated", "high", "critical"}
    # Every component score is itself a normalized [0,1] value.
    for name, val in res.component_scores.items():
        assert name in RISK_COMPONENTS
        assert 0.0 <= val <= 1.0


def test_risk_index_none_component_contributes_zero_not_crash() -> None:
    """A present-but-unmeasured signal is absent from the weighted sum, not 0-risk-zeroed."""
    from bastion.correlation import compute_risk_index

    # Only VRAM is measured and it is maxed; thermal/psi None on a no-GPU/old-kernel host.
    res = compute_risk_index(
        vram_utilization_pct=100.0,
        thermal_headroom_c=None,
        swap_rate_level=None,
        thrashing_verdict=None,
        memory_psi=None,
        config=CorrelationConfig(),
    )
    assert 0.0 <= res.score <= 1.0
    # VRAM is the only measured (and maxed) component -> it dominates.
    assert res.dominant_factor == "vram_headroom"
    # The None components are absent from component_scores (term absent, not 0).
    assert "thermal_headroom" not in res.component_scores
    assert "memory_psi" not in res.component_scores


def test_risk_index_high_pressure_raises_score() -> None:
    """Maxed-out inputs across all components yield a high/critical score near 1."""
    from bastion.correlation import compute_risk_index

    res = compute_risk_index(
        vram_utilization_pct=100.0,
        thermal_headroom_c=0.0,
        swap_rate_level="critical",
        thrashing_verdict="halt",
        memory_psi=100.0,
        config=CorrelationConfig(),
    )
    assert res.score > 0.7
    assert res.level in {"high", "critical"}


# ---------------------------------------------------------------------------
# 6.5 build_thermal_coupling — fan-curve-derived active flag + config ceiling
# ---------------------------------------------------------------------------


def test_thermal_coupling_active_matches_fan_curve() -> None:
    from bastion.correlation import build_thermal_coupling

    # Below the curve minimum (60C) -> BIOS auto -> not coupled.
    tc = build_thermal_coupling(
        cpu_temp_c=50.0, gpu_temp_c=60.0, fan_speed_pct=30,
        gpu_max_temperature_c=83, config=CorrelationConfig(),
    )
    assert isinstance(tc, ThermalCoupling)
    assert tc.coupling_active is (_fan_band(50.0) is not None)
    assert tc.coupling_active is False

    # At/above the curve engagement (>=60C) -> coupled.
    tc2 = build_thermal_coupling(
        cpu_temp_c=72.0, gpu_temp_c=60.0, fan_speed_pct=50,
        gpu_max_temperature_c=83, config=CorrelationConfig(),
    )
    assert tc2.coupling_active is (_fan_band(72.0) is not None)
    assert tc2.coupling_active is True


def test_thermal_coupling_active_false_when_cpu_temp_none() -> None:
    from bastion.correlation import build_thermal_coupling

    tc = build_thermal_coupling(
        cpu_temp_c=None, gpu_temp_c=70.0, fan_speed_pct=50,
        gpu_max_temperature_c=83, config=CorrelationConfig(),
    )
    assert tc.coupling_active is False


def test_thermal_headroom_uses_config_ceiling_not_60() -> None:
    """The CPU headroom term uses cpu_safe_ceiling_c (85 default), not the fan threshold (60)."""
    from bastion.correlation import build_thermal_coupling

    # CPU at 60C: a 60-ceiling formula would read 0 headroom (misleading). With
    # the 85 default ceiling, CPU headroom is 25C. GPU at 70C, ceiling 83 -> 13C.
    tc = build_thermal_coupling(
        cpu_temp_c=60.0, gpu_temp_c=70.0, fan_speed_pct=50,
        gpu_max_temperature_c=83, config=CorrelationConfig(),
    )
    # min(83-70, 85-60) = min(13, 25) = 13
    assert tc.thermal_headroom_min_c == pytest.approx(13.0)


def test_thermal_headroom_custom_ceiling() -> None:
    from bastion.correlation import build_thermal_coupling

    cfg = CorrelationConfig(cpu_safe_ceiling_c=95.0)
    tc = build_thermal_coupling(
        cpu_temp_c=90.0, gpu_temp_c=None, fan_speed_pct=None,
        gpu_max_temperature_c=83, config=cfg,
    )
    # GPU term skipped (gpu_temp None) -> CPU-only headroom = 95 - 90 = 5.
    assert tc.thermal_headroom_min_c == pytest.approx(5.0)


def test_thermal_headroom_skips_gpu_term_when_gpu_temp_none() -> None:
    """On a no-GPU host the GPU term is skipped; headroom is the CPU-only value (not 0)."""
    from bastion.correlation import build_thermal_coupling

    tc = build_thermal_coupling(
        cpu_temp_c=70.0, gpu_temp_c=None, fan_speed_pct=None,
        gpu_max_temperature_c=0, config=CorrelationConfig(),
    )
    # CPU-only: 85 - 70 = 15. GPU term absent (gpu_temp None AND ceiling 0).
    assert tc.thermal_headroom_min_c == pytest.approx(15.0)


def test_thermal_headroom_gpu_ceiling_override() -> None:
    """gpu_safe_ceiling_c, when set, overrides gpu.max_temperature_c for the GPU term."""
    from bastion.correlation import build_thermal_coupling

    cfg = CorrelationConfig(gpu_safe_ceiling_c=90.0)
    tc = build_thermal_coupling(
        cpu_temp_c=50.0, gpu_temp_c=80.0, fan_speed_pct=60,
        gpu_max_temperature_c=83, config=cfg,
    )
    # GPU term uses the override 90: 90-80=10; CPU 85-50=35 -> min 10.
    assert tc.thermal_headroom_min_c == pytest.approx(10.0)


def test_thermal_headroom_none_when_neither_term_computable() -> None:
    """No CPU temp and no GPU temp -> headroom is None (nothing to measure), not 0."""
    from bastion.correlation import build_thermal_coupling

    tc = build_thermal_coupling(
        cpu_temp_c=None, gpu_temp_c=None, fan_speed_pct=None,
        gpu_max_temperature_c=0, config=CorrelationConfig(),
    )
    assert tc.thermal_headroom_min_c is None
    assert tc.coupling_active is False


def test_thermal_coupling_passes_through_inputs() -> None:
    from bastion.correlation import build_thermal_coupling

    tc = build_thermal_coupling(
        cpu_temp_c=65.0, gpu_temp_c=72.0, fan_speed_pct=55,
        gpu_max_temperature_c=83, config=CorrelationConfig(),
    )
    assert tc.cpu_temp_c == 65.0
    assert tc.gpu_temp_c == 72.0
    assert tc.fan_speed_pct == 55
