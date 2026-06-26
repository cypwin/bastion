"""Pydantic field-default assertions for the swap-brake config models (M1–M4).

This test file is owned exclusively by the models.py task chain (M1→M2→M3→M4)
so those tasks never share a test file with their consumers — see the
implementation plan's test-file ownership map.
"""

from __future__ import annotations

from bastion.models import (
    PinDetectionConfig,
    SchedulerConfig,
    SwapBrakeConfig,
)


class TestSwapBrakeConfigDefaults:
    def test_swap_brake_nested_in_scheduler(self) -> None:
        sched = SchedulerConfig()
        assert isinstance(sched.swap_brake, SwapBrakeConfig)
        assert isinstance(sched.pin_detection, PinDetectionConfig)

    def test_swap_brake_defaults(self) -> None:
        b = SchedulerConfig().swap_brake
        assert b.enabled is True
        assert b.min_spacing_seconds == 8.0
        assert b.bucket_capacity == 3.0
        assert b.refill_per_minute == 5.0
        assert b.count_evictions is True
        assert b.cooloff_seconds == 30.0
        assert b.cooloff_backoff_max_seconds == 60.0
        assert b.min_state_hold_seconds == 5.0
        assert b.release_rate_per_minute == 3.0
        assert b.shed_when_infeasible is True
        assert b.infeasible_evict_reload_threshold == 3
        assert b.infeasible_window_seconds == 120.0
        assert b.degraded_refill_factor == 0.5

    def test_pin_detection_defaults(self) -> None:
        p = SchedulerConfig().pin_detection
        assert p.enabled is True
        assert p.expires_horizon_seconds == 3600.0
