"""Tests for residency-aware scheduling.

Covers co-resident transitions, cache expiry, affinity interaction.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    PriorityTier,
    ResidencyState,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import ResidencyCache, VRAMTracker
from tests.conftest import make_request


@pytest.fixture
def residency_config() -> BrokerConfig:
    """Config for residency tests with short cache TTL."""
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.2,  # Long enough to verify skip
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=32,
            residency_cache_ttl_seconds=0.1,  # Fast expiry for tests
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            "llama3.1:8b": ModelInfo(vram_gb=4.4),
        },
    )


@pytest.fixture
def dispatch_log():
    """Collects dispatched requests for assertions."""
    log = []

    async def dispatch_fn(request, needs_swap=True):
        log.append(request)

    return log, dispatch_fn


# ---------------------------------------------------------------------------
# ResidencyCache tests
# ---------------------------------------------------------------------------


class TestResidencyCache:
    @pytest.mark.asyncio
    async def test_cache_populated_on_first_query(self, residency_config):
        """First query should hit VRAMTracker and populate cache."""
        tracker = VRAMTracker(residency_config)
        cache = ResidencyCache(tracker, ttl_seconds=1.0)

        # Mock Ollama response
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "qwen3:14b", "size": 9965000000, "details": {}},
                {"name": "mistral-nemo:12b", "size": 8700000000, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            resident = await cache.get_resident_models()

        assert resident == {"qwen3:14b", "mistral-nemo:12b"}

    @pytest.mark.asyncio
    async def test_cache_returns_cached_data_within_ttl(self, residency_config):
        """Subsequent queries within TTL should not hit VRAMTracker."""
        tracker = VRAMTracker(residency_config)
        cache = ResidencyCache(tracker, ttl_seconds=1.0)

        # First query
        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        get_mock = AsyncMock(return_value=mock_resp)
        with patch.object(tracker._http, "get", get_mock):
            await cache.get_resident_models()
            # Second query within TTL
            await cache.get_resident_models()

        # Should only hit /api/ps once
        assert get_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_refreshes_after_ttl_expires(self, residency_config):
        """Cache should refresh after TTL expires."""
        tracker = VRAMTracker(residency_config)
        cache = ResidencyCache(tracker, ttl_seconds=0.05)  # 50ms TTL

        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        get_mock = AsyncMock(return_value=mock_resp)

        with patch.object(tracker._http, "get", get_mock):
            await cache.get_resident_models()
            await asyncio.sleep(0.1)  # Wait for TTL to expire
            await cache.get_resident_models()

        # Should hit /api/ps twice (initial + after expiry)
        assert get_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_is_model_resident(self, residency_config):
        """is_model_resident should check cache correctly."""
        tracker = VRAMTracker(residency_config)
        cache = ResidencyCache(tracker, ttl_seconds=1.0)

        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            is_resident = await cache.is_model_resident("qwen3:14b")
            is_not_resident = await cache.is_model_resident("mistral-nemo:12b")

        assert is_resident is True
        assert is_not_resident is False

    @pytest.mark.asyncio
    async def test_invalidate_forces_refresh(self, residency_config):
        """Invalidate should force cache refresh on next query."""
        tracker = VRAMTracker(residency_config)
        cache = ResidencyCache(tracker, ttl_seconds=10.0)  # Long TTL

        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        get_mock = AsyncMock(return_value=mock_resp)

        with patch.object(tracker._http, "get", get_mock):
            await cache.get_resident_models()
            cache.invalidate()
            await cache.get_resident_models()

        # Should hit /api/ps twice (initial + after invalidate)
        assert get_mock.call_count == 2


# ---------------------------------------------------------------------------
# ResidencyState model tests
# ---------------------------------------------------------------------------


class TestResidencyState:
    def test_from_loaded_models(self):
        """ResidencyState.from_loaded_models() should create correct snapshot."""
        models = [
            LoadedModel(name="qwen3:14b", size_bytes=9965000000, vram_gb=9.3, details={}),
            LoadedModel(name="mistral-nemo:12b", size_bytes=8700000000, vram_gb=8.1, details={}),
        ]

        state = ResidencyState.from_loaded_models(models)

        assert set(state.resident_models) == {"qwen3:14b", "mistral-nemo:12b"}
        assert state.vram_usage == {"qwen3:14b": 9.3, "mistral-nemo:12b": 8.1}
        assert state.total_vram_gb == 17.4

    def test_total_vram_gb_computed(self):
        """total_vram_gb property should sum VRAM usage."""
        state = ResidencyState(
            resident_models=["qwen3:14b", "mistral-nemo:12b"],
            last_refreshed=time.time(),
            vram_usage={"qwen3:14b": 9.3, "mistral-nemo:12b": 8.1},
        )
        assert state.total_vram_gb == 17.4

    def test_age_seconds(self):
        """age_seconds should measure staleness correctly."""
        past_time = time.time() - 5.0
        state = ResidencyState(
            resident_models=["qwen3:14b"],
            last_refreshed=past_time,
            vram_usage={"qwen3:14b": 9.3},
        )
        assert 4.9 < state.age_seconds < 5.1

    def test_serialization(self):
        """ResidencyState should serialize correctly for admin API."""
        state = ResidencyState(
            resident_models=["qwen3:14b"],
            last_refreshed=1234567890.0,
            vram_usage={"qwen3:14b": 9.3},
        )
        data = state.model_dump()

        assert data["resident_models"] == ["qwen3:14b"]
        assert data["last_refreshed"] == 1234567890.0
        assert data["vram_usage"] == {"qwen3:14b": 9.3}


# ---------------------------------------------------------------------------
# Scheduler residency-aware behavior tests
# ---------------------------------------------------------------------------


class TestCoResidentSkipCooldown:
    @pytest.mark.asyncio
    async def test_alternating_coresident_models_skip_cooldown(
        self, residency_config, dispatch_log,
    ):
        """Alternating requests between two co-resident models should NOT trigger cooldown."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(residency_config.scheduler)
        tracker = VRAMTracker(residency_config)

        # Both models are already loaded
        loaded_models = [
            LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={}),
            LoadedModel(name="mistral-nemo:12b", size_bytes=0, vram_gb=8.1, details={}),
        ]

        # Enqueue alternating requests
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))
        queue.enqueue(make_request(model="mistral-nemo:12b", tier=PriorityTier.INTERACTIVE))
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))

        with (
            patch.object(tracker, "get_loaded_models",
                         new_callable=AsyncMock, return_value=loaded_models),
            patch("bastion.scheduler.check_gpu_safe",
                  AsyncMock(return_value=(True, "OK"))),
            patch.object(tracker, "can_load_model",
                         new_callable=AsyncMock, return_value=(True, "OK")),
        ):
            sched = Scheduler(residency_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Wait for all requests to dispatch
            # Without residency-aware scheduling, this would take
            # 0.2s * 2 swaps = 0.4s+
            # With residency-aware, should complete quickly (no cooldown)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(log) >= 3:
                    break

            await sched.stop()

        # All three requests should have been dispatched
        assert len(log) == 3
        # Verify no swaps were counted (co-resident transitions)
        assert sched.total_swaps == 0


class TestEvictionTriggersSwap:
    @pytest.mark.asyncio
    async def test_non_resident_model_triggers_cooldown(self, residency_config, dispatch_log):
        """When VRAM budget would be exceeded, full cooldown should apply."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(residency_config.scheduler)
        tracker = VRAMTracker(residency_config)

        # Only qwen3:14b is loaded
        loaded_models = [
            LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={}),
        ]

        # Enqueue requests: qwen3 → mistral (will trigger swap) → qwen3 again
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))
        queue.enqueue(make_request(model="mistral-nemo:12b", tier=PriorityTier.INTERACTIVE))
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))

        start_time = time.time()

        with (
            patch.object(tracker, "get_loaded_models",
                         new_callable=AsyncMock, return_value=loaded_models),
            patch("bastion.scheduler.check_gpu_safe",
                  AsyncMock(return_value=(True, "OK"))),
            patch.object(tracker, "can_load_model",
                         new_callable=AsyncMock, return_value=(True, "OK")),
        ):
            sched = Scheduler(residency_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Wait for all requests to dispatch
            for _ in range(150):
                await asyncio.sleep(0.02)
                if len(log) >= 3:
                    break

            await sched.stop()

        elapsed = time.time() - start_time

        # All three requests should have been dispatched
        assert len(log) == 3
        # Should have 1 swap (qwen3 to mistral)
        # Note: The third request (back to qwen3) doesn't trigger a swap because
        # qwen3:14b is still resident in VRAM (per the mock). This is the S3
        # residency-aware behavior - co-resident transitions skip cooldown and don't count as swaps.
        assert sched.total_swaps == 1
        # Should have taken at least 1 * cooldown (0.2s) = 0.2s
        assert elapsed >= 0.15  # Allow some margin


class TestResidencyWithAffinity:
    @pytest.mark.asyncio
    async def test_residency_works_with_affinity_bonus(self, residency_config, dispatch_log):
        """Residency awareness should work correctly with affinity scheduling."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(residency_config.scheduler)
        tracker = VRAMTracker(residency_config)

        # Two models loaded
        loaded_models = [
            LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={}),
            LoadedModel(name="mistral-nemo:12b", size_bytes=0, vram_gb=8.1, details={}),
        ]

        # Enqueue: qwen3 (interactive) → mistral (agent) → qwen3 (agent)
        # First qwen3 should dispatch, then despite mistral having lower base priority,
        # second qwen3 should get affinity bonus and dispatch next (skipping cooldown)
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))
        queue.enqueue(make_request(model="mistral-nemo:12b", tier=PriorityTier.AGENT))
        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.AGENT))

        with (
            patch.object(tracker, "get_loaded_models",
                         new_callable=AsyncMock, return_value=loaded_models),
            patch("bastion.scheduler.check_gpu_safe",
                  AsyncMock(return_value=(True, "OK"))),
            patch.object(tracker, "can_load_model",
                         new_callable=AsyncMock, return_value=(True, "OK")),
        ):
            sched = Scheduler(residency_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Wait for all requests to dispatch
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(log) >= 3:
                    break

            await sched.stop()

        assert len(log) == 3
        # Affinity should have kept us on qwen3 for requests 1 and 3
        assert log[0].model == "qwen3:14b"
        assert log[1].model == "qwen3:14b"  # Affinity bonus wins
        assert log[2].model == "mistral-nemo:12b"
        # No swaps (all co-resident transitions)
        assert sched.total_swaps == 0


class TestUnloadInvalidatesCache:
    @pytest.mark.asyncio
    async def test_unload_invalidates_residency_cache(self, residency_config):
        """VRAMTracker.unload_model() should invalidate residency cache."""
        tracker = VRAMTracker(residency_config)

        # Mock successful unload
        mock_resp = httpx.Response(
            200, json={},
            request=httpx.Request("POST", "http://mock"),
        )

        # Populate cache first
        loaded_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )

        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=loaded_resp):
            # Populate cache
            resident = await tracker.residency_cache.get_resident_models()
            assert "qwen3:14b" in resident

        # Now unload the model
        with patch.object(tracker._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            await tracker.unload_model("qwen3:14b")

        # Cache timestamp should be zeroed (invalidated)
        assert tracker.residency_cache._cache_timestamp == 0.0
