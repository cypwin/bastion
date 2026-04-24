"""Tests for per-agent swap thrashing detection (M58)."""

from __future__ import annotations

import time

from bastion.models import ThrashingDetectionConfig
from bastion.thrashing import ThrashingDetector, ThrashingVerdict


class TestThrashingVerdict:
    def test_verdict_values(self):
        assert ThrashingVerdict.OK == "ok"
        assert ThrashingVerdict.WARN == "warn"
        assert ThrashingVerdict.HALT == "halt"


class TestThrashingDetectorCheck:
    def _make_detector(self, **kwargs) -> ThrashingDetector:
        cfg = ThrashingDetectionConfig(**kwargs)
        return ThrashingDetector(cfg)

    def test_ok_when_below_threshold(self):
        det = self._make_detector(window_size=6, min_requests_before_eval=3)
        # 3 requests to same model = 0 swaps
        for _ in range(3):
            det.record_request("agent1", "modelA")
        verdict = det.check("agent1")
        assert verdict.level == ThrashingVerdict.OK

    def test_no_eval_before_min_requests(self):
        det = self._make_detector(min_requests_before_eval=6)
        # 4 alternating = 3 swaps out of 3 transitions = 100% ratio
        # but only 4 requests, below min_requests_before_eval=6
        for i in range(4):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        assert verdict.level == ThrashingVerdict.OK

    def test_warn_at_threshold(self):
        det = self._make_detector(
            window_size=10, min_requests_before_eval=6,
            warn_swap_ratio=0.5, mode="warn",
        )
        # 8 alternating requests = 7 swaps out of 7 transitions = 100%
        for i in range(8):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        assert verdict.level == ThrashingVerdict.WARN

    def test_no_halt_in_warn_mode(self):
        det = self._make_detector(
            window_size=10, min_requests_before_eval=3,
            warn_swap_ratio=0.3, halt_swap_ratio=0.7, mode="warn",
        )
        # 10 alternating = 9 swaps / 9 transitions = 100% ratio
        for i in range(10):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        # Even above halt_swap_ratio, warn mode caps at WARN
        assert verdict.level == ThrashingVerdict.WARN

    def test_halt_in_strict_mode(self):
        det = self._make_detector(
            window_size=10, min_requests_before_eval=3,
            warn_swap_ratio=0.3, halt_swap_ratio=0.7, mode="strict",
        )
        for i in range(10):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        assert verdict.level == ThrashingVerdict.HALT

    def test_multiple_agents_independent(self):
        det = self._make_detector(
            window_size=10, min_requests_before_eval=3,
            warn_swap_ratio=0.5,
        )
        # agent1: thrashing
        for i in range(8):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        # agent2: stable
        for _ in range(8):
            det.record_request("agent2", "modelA")
        assert det.check("agent1").level == ThrashingVerdict.WARN
        assert det.check("agent2").level == ThrashingVerdict.OK

    def test_window_slides(self):
        det = self._make_detector(
            window_size=6, min_requests_before_eval=3,
            warn_swap_ratio=0.5,
        )
        # Fill window with alternating (thrashing)
        for i in range(6):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        assert det.check("agent1").level == ThrashingVerdict.WARN
        # Now add 6 stable requests (same model) — old entries slide out
        for _ in range(6):
            det.record_request("agent1", "modelA")
        assert det.check("agent1").level == ThrashingVerdict.OK

    def test_unknown_agent_returns_ok(self):
        det = self._make_detector()
        verdict = det.check("never_seen")
        assert verdict.level == ThrashingVerdict.OK

    def test_disabled_always_ok(self):
        det = self._make_detector(enabled=False)
        for i in range(20):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        assert det.check("agent1").level == ThrashingVerdict.OK


class TestThrashingDetectorCooloff:
    def test_cooloff_active_after_halt(self):
        det = ThrashingDetector(ThrashingDetectionConfig(
            window_size=6, min_requests_before_eval=3,
            warn_swap_ratio=0.3, halt_swap_ratio=0.5,
            mode="strict", cooloff_seconds=60,
        ))
        for i in range(6):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        assert verdict.level == ThrashingVerdict.HALT
        # Immediately after halt, still in cooloff
        verdict2 = det.check("agent1")
        assert verdict2.level == ThrashingVerdict.HALT
        assert verdict2.cooloff_remaining > 0

    def test_cooloff_expires(self):
        det = ThrashingDetector(ThrashingDetectionConfig(
            window_size=6, min_requests_before_eval=3,
            warn_swap_ratio=0.3, halt_swap_ratio=0.5,
            mode="strict", cooloff_seconds=1,
        ))
        for i in range(6):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        det.check("agent1")  # triggers halt + cooloff
        time.sleep(1.1)
        # After cooloff, re-evaluate (window still has swaps, so still HALT)
        # but cooloff_remaining should be 0
        verdict = det.check("agent1")
        assert verdict.cooloff_remaining == 0


class TestThrashingDetectorEstimate:
    def test_verdict_includes_swap_ratio(self):
        det = ThrashingDetector(ThrashingDetectionConfig(
            window_size=10, min_requests_before_eval=3,
            warn_swap_ratio=0.5,
        ))
        for i in range(8):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        verdict = det.check("agent1")
        assert verdict.swap_ratio > 0.5
        assert verdict.window_size > 0


class TestThrashingDetectorStats:
    def test_stats_counting(self):
        det = ThrashingDetector(ThrashingDetectionConfig(
            window_size=6, min_requests_before_eval=3,
            warn_swap_ratio=0.3, halt_swap_ratio=0.7, mode="strict",
        ))
        for i in range(6):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        det.check("agent1")  # warn
        assert det.total_warnings >= 1

        # Push ratio above halt
        for i in range(6):
            det.record_request("agent1", "modelA" if i % 2 == 0 else "modelB")
        det.check("agent1")  # halt
        assert det.total_halts >= 1
