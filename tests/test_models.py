"""Tests for Pydantic models, priority tiers, and effective priority calculation."""

from __future__ import annotations

import time

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    ModelInfo,
    OllamaConfig,
    PriorityConfig,
    PriorityTier,
    QueuedRequest,
)

# ---------------------------------------------------------------------------
# Configuration model defaults
# ---------------------------------------------------------------------------

class TestOllamaConfig:
    def test_defaults(self):
        c = OllamaConfig()
        assert c.host == "127.0.0.1"
        assert c.port == 11435

    def test_base_url(self):
        c = OllamaConfig(host="10.0.0.1", port=9999)
        assert c.base_url == "http://10.0.0.1:9999"


class TestGPUConfig:
    def test_max_vram_gb(self):
        c = GPUConfig(total_vram_gb=32.0, headroom_gb=6.0)
        assert c.max_vram_gb == 26.0

    def test_custom_headroom(self):
        c = GPUConfig(total_vram_gb=24.0, headroom_gb=4.0)
        assert c.max_vram_gb == 20.0


class TestBrokerConfig:
    def test_defaults(self):
        c = BrokerConfig()
        assert c.server.port == 11434
        assert c.ollama.port == 11435
        # GPU defaults: total_vram_gb=0 (auto-detect), headroom_gb=6
        assert c.gpu.total_vram_gb == 0.0
        assert c.gpu.max_vram_gb == -6.0  # 0 - 6; resolve_gpu_defaults() fixes this
        assert c.scheduler.cooldown_seconds == 2.0
        assert c.request_overrides.use_mmap is False

    def test_with_models(self):
        c = BrokerConfig(models={"test:1b": ModelInfo(vram_gb=1.0)})
        assert "test:1b" in c.models
        assert c.models["test:1b"].vram_gb == 1.0


# ---------------------------------------------------------------------------
# Priority tiers
# ---------------------------------------------------------------------------

class TestPriorityTier:
    def test_values(self):
        assert PriorityTier.INTERACTIVE.value == "interactive"
        assert PriorityTier.AGENT.value == "agent"
        assert PriorityTier.PIPELINE.value == "pipeline"
        assert PriorityTier.BACKGROUND.value == "background"

    def test_base_priority(self):
        config = PriorityConfig()
        assert PriorityTier.INTERACTIVE.base_priority(config) == 100.0
        assert PriorityTier.AGENT.base_priority(config) == 50.0
        assert PriorityTier.PIPELINE.base_priority(config) == 25.0
        assert PriorityTier.BACKGROUND.base_priority(config) == 10.0

    def test_ordering(self):
        config = PriorityConfig()
        tiers = [PriorityTier.BACKGROUND, PriorityTier.PIPELINE,
                 PriorityTier.AGENT, PriorityTier.INTERACTIVE]
        priorities = [t.base_priority(config) for t in tiers]
        assert priorities == sorted(priorities)


# ---------------------------------------------------------------------------
# QueuedRequest
# ---------------------------------------------------------------------------

class TestQueuedRequest:
    def test_auto_id(self):
        r = QueuedRequest(model="test:1b", endpoint="/api/generate")
        assert len(r.id) == 12  # hex[:12]

    def test_unique_ids(self):
        ids = {QueuedRequest(model="m", endpoint="/api/generate").id for _ in range(100)}
        assert len(ids) == 100

    def test_age_seconds(self):
        r = QueuedRequest(
            model="m", endpoint="/api/generate",
            submitted_at=time.time() - 10.0,
        )
        assert r.age_seconds >= 10.0

    def test_effective_priority_no_aging(self):
        r = QueuedRequest(
            model="m", endpoint="/api/generate",
            base_priority=50.0, submitted_at=time.time(),
        )
        # With no aging (just submitted), effective ≈ base
        p = r.effective_priority(aging_rate=2.0)
        assert 50.0 <= p < 51.0

    def test_effective_priority_with_aging(self):
        r = QueuedRequest(
            model="m", endpoint="/api/generate",
            base_priority=10.0, submitted_at=time.time() - 45.0,
        )
        # 10 + 45*2 = 100
        p = r.effective_priority(aging_rate=2.0)
        assert p >= 99.0  # Allow small timing variance

    def test_effective_priority_with_affinity(self):
        r = QueuedRequest(
            model="m", endpoint="/api/generate",
            base_priority=50.0, submitted_at=time.time(),
        )
        p = r.effective_priority(aging_rate=2.0, affinity_bonus=10.0)
        assert p >= 60.0


# ---------------------------------------------------------------------------
# GPU status
# ---------------------------------------------------------------------------

class TestGPUStatus:
    def test_safe_status(self):
        s = GPUStatus(temperature_c=55, vram_used_mb=8000, vram_total_mb=32000)
        assert s.is_safe() is True

    def test_unsafe_temperature(self):
        s = GPUStatus(temperature_c=90, vram_used_mb=8000, vram_total_mb=32000)
        assert s.is_safe() is False

    def test_unsafe_vram(self):
        s = GPUStatus(temperature_c=55, vram_used_mb=31000, vram_total_mb=32000)
        assert s.is_safe() is False  # >95%

    def test_vram_utilization(self):
        s = GPUStatus(vram_used_mb=16000, vram_total_mb=32000)
        assert s.vram_utilization_pct == 50.0

    def test_empty_status_is_safe(self):
        s = GPUStatus()
        assert s.is_safe() is True
        assert s.vram_utilization_pct is None

    # -- is_safe with GPUConfig (S8 bug fix) ---------------------------------

    def test_is_safe_default_threshold(self):
        """Without GPUConfig, is_safe uses the hardcoded default of 82C."""
        s = GPUStatus(temperature_c=80, vram_used_mb=8000, vram_total_mb=32000)
        assert s.is_safe() is True
        s2 = GPUStatus(temperature_c=83, vram_used_mb=8000, vram_total_mb=32000)
        assert s2.is_safe() is False

    def test_is_safe_custom_threshold(self):
        """With GPUConfig, is_safe uses the configured max_temperature_c."""
        gpu_cfg = GPUConfig(max_temperature_c=70)
        s = GPUStatus(temperature_c=65, vram_used_mb=8000, vram_total_mb=32000)
        assert s.is_safe(gpu_cfg) is True
        s2 = GPUStatus(temperature_c=75, vram_used_mb=8000, vram_total_mb=32000)
        assert s2.is_safe(gpu_cfg) is False

    def test_75c_safe_default_unsafe_custom(self):
        """75C is safe with default (82C) but unsafe with custom max_temp=70."""
        s = GPUStatus(temperature_c=75, vram_used_mb=8000, vram_total_mb=32000)
        assert s.is_safe() is True
        assert s.is_safe(GPUConfig(max_temperature_c=70)) is False


class TestModelInfo:
    def test_always_allowed(self):
        m = ModelInfo(vram_gb=0.4, always_allowed=True)
        assert m.always_allowed is True

    def test_default_not_always_allowed(self):
        m = ModelInfo(vram_gb=9.3)
        assert m.always_allowed is False
        assert m.tags == []
