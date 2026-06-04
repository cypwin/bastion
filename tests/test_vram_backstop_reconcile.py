"""nvidia-smi reserve() backstop + bidirectional reconcile import.

Covers the 2026-06 admission-gate honesty work:
  - _hardware_admits() helper (fail-open / reject / admit)
  - VRAMManager.reserve() rejects on insufficient hardware free VRAM
  - ResidencyCache.get_resident_loaded_models() accessor
  - reconcile() imports resident-but-untracked models (skipping always_allowed
    and mid-reservation models) while still removing stale allocations
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import BrokerConfig, GPUConfig, LoadedModel, ModelInfo
from bastion.vram import (
    HARDWARE_MARGIN_GB,
    ResidencyCache,
    VRAMManager,
    VRAMTracker,
    _hardware_admits,
)

GB = 1024 ** 3


@pytest.fixture(autouse=True)
def _default_fail_open():
    """Default backstop to fail-open; per-test patches override this."""
    with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
        yield


@pytest.fixture
def config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={
            "tracked:7b": ModelInfo(vram_gb=5.0),
            "ext:13b": ModelInfo(vram_gb=9.0),
            "embed:v1": ModelInfo(vram_gb=7.0, always_allowed=True),
        },
    )


@pytest.fixture
def tracker(config: BrokerConfig) -> VRAMTracker:
    return VRAMTracker(config)


@pytest.fixture
def manager(tracker: VRAMTracker) -> VRAMManager:
    return VRAMManager(tracker, 32 * GB, safety_margin_pct=10.0)


def _lm(name: str, vram_gb: float) -> LoadedModel:
    return LoadedModel(name=name, size_bytes=int(vram_gb * GB), vram_gb=vram_gb, details={})


# ---------------------------------------------------------------------------
# _hardware_admits
# ---------------------------------------------------------------------------

class TestHardwareAdmits:
    @pytest.mark.asyncio
    async def test_fail_open_when_no_reading(self):
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
            admits, free = await _hardware_admits(9 * GB)
        assert admits is True
        assert free is None

    @pytest.mark.asyncio
    async def test_rejects_when_insufficient(self):
        # 9 GB + 2 GB margin = 11 GB needed; only 5 GB free
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=5.0)):
            admits, free = await _hardware_admits(9 * GB)
        assert admits is False
        assert free == 5.0

    @pytest.mark.asyncio
    async def test_admits_when_sufficient(self):
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=20.0)):
            admits, free = await _hardware_admits(9 * GB)
        assert admits is True
        assert free == 20.0

    def test_margin_constant(self):
        assert HARDWARE_MARGIN_GB == 2.0


# ---------------------------------------------------------------------------
# reserve() backstop
# ---------------------------------------------------------------------------

class TestReserveBackstop:
    @pytest.mark.asyncio
    async def test_rejected_when_hardware_insufficient(self, manager):
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=5.0)):
            with pytest.raises(ValueError, match="nvidia-smi backstop"):
                await manager.reserve("ext:13b", 9 * GB)
        assert manager.reserved_bytes == 0

    @pytest.mark.asyncio
    async def test_succeeds_when_hardware_sufficient(self, manager):
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=20.0)):
            res = await manager.reserve("ext:13b", 9 * GB)
        assert res.vram_bytes == 9 * GB
        assert manager.reserved_bytes == 9 * GB

    @pytest.mark.asyncio
    async def test_fail_open_when_no_reading(self, manager):
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
            await manager.reserve("ext:13b", 9 * GB)
        assert manager.reserved_bytes == 9 * GB


# ---------------------------------------------------------------------------
# ResidencyCache.get_resident_loaded_models
# ---------------------------------------------------------------------------

class TestResidencyLoadedModels:
    @pytest.mark.asyncio
    async def test_returns_loaded_model_list(self, tracker):
        models = [_lm("a:7b", 5.0)]
        cache = ResidencyCache(tracker, ttl_seconds=10.0)
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=models)):
            result = await cache.get_resident_loaded_models()
        assert result == models

    @pytest.mark.asyncio
    async def test_returns_none_when_unknown(self, tracker):
        cache = ResidencyCache(tracker, ttl_seconds=10.0)
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=None)):
            result = await cache.get_resident_loaded_models()
        assert result is None


# ---------------------------------------------------------------------------
# reconcile() — bidirectional import
# ---------------------------------------------------------------------------

class TestReconcileImport:
    @pytest.mark.asyncio
    async def test_imports_untracked_resident_model(self, manager):
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("ext:13b", 9.0)]
        )
        await manager.reconcile({"ext:13b"})
        assert manager._model_allocations.get("ext:13b") == 9 * GB
        assert manager.allocated_bytes == 9 * GB

    @pytest.mark.asyncio
    async def test_skips_model_with_active_reservation(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)  # reserved, not committed
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("tracked:7b", 5.0)]
        )
        await manager.reconcile({"tracked:7b"})
        assert "tracked:7b" not in manager._model_allocations
        assert manager.allocated_bytes == 0
        assert manager.reserved_bytes == 5 * GB
        assert res.reservation_id in manager._reservations

    @pytest.mark.asyncio
    async def test_skips_always_allowed(self, manager):
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("embed:v1", 7.0)]
        )
        await manager.reconcile({"embed:v1"})
        assert "embed:v1" not in manager._model_allocations
        assert manager.allocated_bytes == 0

    @pytest.mark.asyncio
    async def test_still_removes_stale_and_imports(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        assert manager.allocated_bytes == 5 * GB
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("ext:13b", 9.0)]
        )
        # tracked:7b no longer resident -> removed; ext:13b resident -> imported
        await manager.reconcile({"ext:13b"})
        assert "tracked:7b" not in manager._model_allocations
        assert manager._model_allocations.get("ext:13b") == 9 * GB
        assert manager.allocated_bytes == 9 * GB

    @pytest.mark.asyncio
    async def test_none_is_noop(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        await manager.reconcile(None)
        assert manager.allocated_bytes == 5 * GB

    @pytest.mark.asyncio
    async def test_empty_set_frees_stale_without_import(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        # Empty set must not trigger a residency-cache fetch (no import possible).
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            side_effect=AssertionError("should not fetch sizes for empty set")
        )
        freed = await manager.reconcile(set())
        assert freed == 5 * GB
        assert manager.allocated_bytes == 0
