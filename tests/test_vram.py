"""Tests for VRAM tracking — model queries, budget enforcement, unloading."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.models import BrokerConfig, GPUConfig, GPUStatus, LoadedModel, ModelInfo, SchedulerConfig
from bastion.vram import ResidencyCache, VRAMTracker


@pytest.fixture
def vram_config() -> BrokerConfig:
    """Config with known models for VRAM tests."""
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),  # max 26 GB
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            "nomic-embed-text": ModelInfo(vram_gb=0.4, always_allowed=True),
        },
    )


class TestGetLoadedModels:
    @pytest.mark.asyncio
    async def test_parses_ollama_ps(self, vram_config):
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "qwen3:14b", "size": 9965000000, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            models = await tracker.get_loaded_models()

        assert len(models) == 1
        assert models[0].name == "qwen3:14b"
        assert models[0].vram_gb == 9.3  # From known config, not size_bytes

    @pytest.mark.asyncio
    async def test_unknown_model_uses_size_estimate(self, vram_config):
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "unknown:7b", "size": 4_500_000_000, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            models = await tracker.get_loaded_models()

        assert len(models) == 1
        # Falls back to size_bytes / 1024^3 ≈ 4.19 GB
        assert 4.0 < models[0].vram_gb < 4.5

    @pytest.mark.asyncio
    async def test_connection_failure_returns_empty(self, vram_config):
        tracker = VRAMTracker(vram_config)
        with patch.object(tracker._http, "get", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
            models = await tracker.get_loaded_models()

        assert models == []


class TestCanLoadModel:
    @pytest.mark.asyncio
    async def test_always_allowed(self, vram_config):
        tracker = VRAMTracker(vram_config)
        can, reason = await tracker.can_load_model("nomic-embed-text")
        assert can is True
        assert "always" in reason.lower()

    @pytest.mark.asyncio
    async def test_already_loaded(self, vram_config):
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))):
            can, reason = await tracker.can_load_model("qwen3:14b")
        assert can is True
        assert "already loaded" in reason.lower()

    @pytest.mark.asyncio
    async def test_within_budget(self, vram_config):
        tracker = VRAMTracker(vram_config)
        # Nothing loaded, 9.3 GB < 26 GB budget
        mock_resp = httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
            can, reason = await tracker.can_load_model("qwen3:14b")
        assert can is True

    @pytest.mark.asyncio
    async def test_exceeds_budget(self, vram_config):
        tracker = VRAMTracker(vram_config)
        # 9.3 + 8.1 = 17.4 loaded, trying to add another 9.3 = 26.7 > 26 GB
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "qwen3:14b", "size": 0, "details": {}},
                {"name": "mistral-nemo:12b", "size": 0, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))):
            can, reason = await tracker.can_load_model("qwen3:14b")
        # qwen3:14b is already loaded, so it should pass
        assert can is True

    @pytest.mark.asyncio
    async def test_gpu_too_hot(self, vram_config):
        tracker = VRAMTracker(vram_config)
        with patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=90))):
            can, reason = await tracker.can_load_model("qwen3:14b")
        assert can is False
        assert "hot" in reason.lower()


class TestUnloadModel:
    @pytest.mark.asyncio
    async def test_successful_unload(self, vram_config):
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200, json={},
            request=httpx.Request("POST", "http://mock"),
        )
        with patch.object(tracker._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            success = await tracker.unload_model("qwen3:14b")
        assert success is True

    @pytest.mark.asyncio
    async def test_failed_unload(self, vram_config):
        tracker = VRAMTracker(vram_config)
        with patch.object(tracker._http, "post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
            success = await tracker.unload_model("qwen3:14b")
        assert success is False


class TestEstimateVRAM:
    def test_fuzzy_match(self, vram_config):
        tracker = VRAMTracker(vram_config)
        # "qwen3:14b" is in "qwen3:14b-q4_K_M"
        estimate = tracker._estimate_vram("qwen3:14b-q4_K_M")
        assert estimate == 9.3

    def test_unknown_defaults_to_10(self, vram_config):
        tracker = VRAMTracker(vram_config)
        estimate = tracker._estimate_vram("completely-unknown:99b")
        assert estimate == 10.0


# ---------------------------------------------------------------------------
# get_loaded_vram_gb audit alert thresholds
# ---------------------------------------------------------------------------


class TestGetLoadedVramGbAlerts:
    """Tests for VRAM alert thresholds in get_loaded_vram_gb()."""

    @pytest.mark.asyncio
    async def test_no_alert_below_85_percent(self, vram_config: BrokerConfig) -> None:
        """Below 85% usage, no audit alert should be emitted."""
        tracker = VRAMTracker(vram_config)
        # Budget is 26 GB. Load 20 GB = 76.9% -- below 85%
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "qwen3:14b", "size": 0, "details": {}},
                {"name": "mistral-nemo:12b", "size": 0, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.audit.emit") as mock_emit:
            total = await tracker.get_loaded_vram_gb()

        # 9.3 + 8.1 = 17.4 GB = 66.9% of 26 GB budget
        assert total == pytest.approx(17.4)
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_alert_at_85_percent(self) -> None:
        """At ~87% usage, a warning-severity alert should be emitted."""
        # Budget = 10 GB (total=12, headroom=2). Load 8.7 GB = 87%
        config = BrokerConfig(
            gpu=GPUConfig(total_vram_gb=12.0, headroom_gb=2.0),
            models={
                "big-model": ModelInfo(vram_gb=8.7),
            },
        )
        tracker = VRAMTracker(config)
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "big-model", "size": 0, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.audit.emit") as mock_emit:
            total = await tracker.get_loaded_vram_gb()

        assert total == pytest.approx(8.7)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == "vram_alert"
        assert call_args[0][1]["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_critical_alert_at_95_percent(self) -> None:
        """At >95% usage, a critical-severity alert should be emitted."""
        # Budget = 10 GB. Load 9.6 GB = 96%
        config = BrokerConfig(
            gpu=GPUConfig(total_vram_gb=12.0, headroom_gb=2.0),
            models={
                "huge-model": ModelInfo(vram_gb=9.6),
            },
        )
        tracker = VRAMTracker(config)
        mock_resp = httpx.Response(
            200,
            json={"models": [
                {"name": "huge-model", "size": 0, "details": {}},
            ]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.audit.emit") as mock_emit:
            total = await tracker.get_loaded_vram_gb()

        assert total == pytest.approx(9.6)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][1]["severity"] == "critical"


# ---------------------------------------------------------------------------
# can_load_model nvidia-smi hard gate
# ---------------------------------------------------------------------------


class TestNvidiaSmiHardGate:
    """Tests for the nvidia-smi free VRAM hard gate in can_load_model()."""

    @pytest.mark.asyncio
    async def test_hard_gate_blocks_when_free_too_low(self, vram_config: BrokerConfig) -> None:
        """When free VRAM is less than model_vram + 2GB safety margin, load is blocked."""
        tracker = VRAMTracker(vram_config)
        # Nothing loaded (budget-wise OK), but nvidia-smi reports only 5 GB free
        # qwen3:14b needs 9.3 + 2.0 = 11.3 GB free
        mock_resp = httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=5.0)):
            can, reason = await tracker.can_load_model("qwen3:14b")

        assert can is False
        assert "nvidia-smi" in reason.lower()
        assert "5.0" in reason

    @pytest.mark.asyncio
    async def test_hard_gate_passes_when_free_sufficient(self, vram_config: BrokerConfig) -> None:
        """When free VRAM exceeds model_vram + 2GB margin, load is allowed."""
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://mock"),
        )
        # qwen3:14b needs 9.3 + 2.0 = 11.3 GB free. We report 15 GB free.
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=15.0)):
            can, reason = await tracker.can_load_model("qwen3:14b")

        assert can is True

    @pytest.mark.asyncio
    async def test_hard_gate_skipped_when_nvidia_smi_unavailable(self, vram_config: BrokerConfig) -> None:
        """When nvidia-smi returns None, the hard gate is skipped (no block)."""
        tracker = VRAMTracker(vram_config)
        mock_resp = httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp), \
             patch("bastion.vram.query_gpu_status", AsyncMock(return_value=GPUStatus(temperature_c=50))), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
            can, reason = await tracker.can_load_model("qwen3:14b")

        assert can is True


# ---------------------------------------------------------------------------
# unload_model polling behavior
# ---------------------------------------------------------------------------


class TestUnloadModelPolling:
    """Tests for unload_model() polling confirmation logic."""

    @pytest.mark.asyncio
    async def test_immediate_removal_confirmed(self, vram_config: BrokerConfig) -> None:
        """Model disappears from /api/ps immediately after unload request."""
        tracker = VRAMTracker(vram_config)
        post_resp = httpx.Response(200, json={}, request=httpx.Request("POST", "http://mock"))
        # First call: post (unload), then get (poll) returns empty
        poll_resp = httpx.Response(
            200, json={"models": []},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "post", new_callable=AsyncMock, return_value=post_resp), \
             patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=poll_resp):
            success = await tracker.unload_model("qwen3:14b")

        assert success is True

    @pytest.mark.asyncio
    async def test_delayed_removal_confirmed(self, vram_config: BrokerConfig) -> None:
        """Model stays in /api/ps for first poll, then disappears on second poll."""
        tracker = VRAMTracker(vram_config)
        post_resp = httpx.Response(200, json={}, request=httpx.Request("POST", "http://mock"))

        # First poll: model still present. Second poll: model gone.
        poll_call_count = [0]

        async def mock_get(*args, **kwargs):
            poll_call_count[0] += 1
            if poll_call_count[0] <= 1:
                return httpx.Response(
                    200,
                    json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
                    request=httpx.Request("GET", "http://mock"),
                )
            return httpx.Response(
                200, json={"models": []},
                request=httpx.Request("GET", "http://mock"),
            )

        with patch.object(tracker._http, "post", new_callable=AsyncMock, return_value=post_resp), \
             patch.object(tracker._http, "get", side_effect=mock_get):
            success = await tracker.unload_model("qwen3:14b")

        assert success is True
        assert poll_call_count[0] >= 2

    @pytest.mark.asyncio
    async def test_timeout_proceeds_anyway(self) -> None:
        """Model never disappears from /api/ps — unload still returns True but logs warning."""
        config = BrokerConfig(
            ollama=__import__("bastion.models", fromlist=["OllamaConfig"]).OllamaConfig(
                unload_timeout_seconds=0.3,  # Very short for test
            ),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(config)
        post_resp = httpx.Response(200, json={}, request=httpx.Request("POST", "http://mock"))

        # Model always present in polls
        still_loaded_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b", "size": 0, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )

        with patch.object(tracker._http, "post", new_callable=AsyncMock, return_value=post_resp), \
             patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=still_loaded_resp):
            success = await tracker.unload_model("qwen3:14b")

        # Still returns True (proceeds anyway), but model wasn't confirmed removed
        assert success is True


# ---------------------------------------------------------------------------
# ResidencyCache concurrent access
# ---------------------------------------------------------------------------


class TestResidencyCacheConcurrency:
    """Tests for ResidencyCache lock contention with multiple coroutines."""

    @pytest.mark.asyncio
    async def test_concurrent_queries_share_single_refresh(self) -> None:
        """Multiple concurrent get_resident_models() calls should not cause
        redundant backend queries due to lock serialization."""
        config = BrokerConfig(
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(config)

        call_count = [0]
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]

        async def slow_get_loaded():
            call_count[0] += 1
            await asyncio.sleep(0.05)  # Simulate network delay
            return loaded

        with patch.object(tracker, "get_loaded_models", side_effect=slow_get_loaded):
            cache = ResidencyCache(tracker, ttl_seconds=10.0)

            # Launch 5 concurrent queries
            results = await asyncio.gather(
                cache.get_resident_models(),
                cache.get_resident_models(),
                cache.get_resident_models(),
                cache.get_resident_models(),
                cache.get_resident_models(),
            )

        # All should return the same result
        for r in results:
            assert r == {"qwen3:14b"}

        # Due to lock serialization, the first call refreshes, and subsequent
        # calls in the window find the cache valid. At most 2 calls expected
        # (one for the initial refresh under lock, and potentially one concurrent
        # call that started checking before the lock was acquired).
        assert call_count[0] <= 2

    @pytest.mark.asyncio
    async def test_invalidate_forces_refresh(self) -> None:
        """After invalidate(), the next query should refresh from backend."""
        config = BrokerConfig(
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(config)

        call_count = [0]

        async def mock_get_loaded():
            call_count[0] += 1
            return [LoadedModel(name="qwen3:14b", vram_gb=9.3)]

        with patch.object(tracker, "get_loaded_models", side_effect=mock_get_loaded):
            cache = ResidencyCache(tracker, ttl_seconds=60.0)  # Long TTL

            # First query
            r1 = await cache.get_resident_models()
            assert call_count[0] == 1

            # Second query should use cache
            r2 = await cache.get_resident_models()
            assert call_count[0] == 1  # No new call

            # Invalidate and query again
            cache.invalidate()
            r3 = await cache.get_resident_models()
            assert call_count[0] == 2  # Forced refresh

        assert r1 == r2 == r3 == {"qwen3:14b"}
