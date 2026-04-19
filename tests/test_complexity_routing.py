"""Tests for M58 complexity routing config and model override logic."""

from __future__ import annotations

from bastion.models import (
    BrokerConfig,
    ComplexityRoutingConfig,
    ThrashingDetectionConfig,
)


class TestComplexityRoutingConfig:
    def test_defaults(self):
        c = ComplexityRoutingConfig()
        assert c.enabled is True
        assert c.routes == {}
        assert c.complex_action == "reject"

    def test_custom_routes(self):
        c = ComplexityRoutingConfig(
            routes={"simple": "qwen3.5:9b", "moderate": "qwen3.5:35b-a3b"},
        )
        assert c.routes["simple"] == "qwen3.5:9b"
        assert c.routes["moderate"] == "qwen3.5:35b-a3b"

    def test_disabled(self):
        c = ComplexityRoutingConfig(enabled=False)
        assert c.enabled is False


class TestThrashingDetectionConfig:
    def test_defaults(self):
        c = ThrashingDetectionConfig()
        assert c.enabled is True
        assert c.mode == "warn"
        assert c.window_size == 12
        assert c.warn_swap_ratio == 0.5
        assert c.halt_swap_ratio == 0.75
        assert c.cooloff_seconds == 30
        assert c.min_requests_before_eval == 6

    def test_strict_mode(self):
        c = ThrashingDetectionConfig(mode="strict")
        assert c.mode == "strict"


class TestBrokerConfigWithComplexity:
    def test_has_complexity_routing(self):
        c = BrokerConfig()
        assert hasattr(c, "complexity_routing")
        assert isinstance(c.complexity_routing, ComplexityRoutingConfig)

    def test_has_thrashing_detection(self):
        c = BrokerConfig()
        assert hasattr(c, "thrashing_detection")
        assert isinstance(c.thrashing_detection, ThrashingDetectionConfig)
