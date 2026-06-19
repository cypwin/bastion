"""Regression tests for VRAM-state-unknown propagation through ResidencyCache
and VRAMManager.reconcile.

Companion to test_vram.py::TestCanLoadModel::test_fail_closed_when_tracker_state_unknown
and TestUnloadModel::test_unload_does_not_falsely_confirm_when_ps_unreachable.

These two callers were the second-order failure points of the original
``get_loaded_models()`` returning ``[]`` on transient backend failure:

  * ResidencyCache used to silently collapse "unknown" into "empty resident
    set", causing the scheduler to mis-classify resident models as evictable
    and to skip co-resident dispatch decisions.
  * VRAMManager.reconcile used to interpret an empty loaded-set as "every
    ledger entry is stale" and wipe the ledger — exactly the safety landmine
    that approved double-loads above the 24 GB budget during transient
    /api/ps outages.

Both regressions are now gated by an explicit ``None`` sentinel.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from bastion.models import BrokerConfig, GPUConfig, LoadedModel, ModelInfo
from bastion.vram import ResidencyCache, VRAMManager, VRAMTracker


@pytest.fixture
def config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
    )


@pytest.fixture
def tracker(config: BrokerConfig) -> VRAMTracker:
    return VRAMTracker(config)


class TestResidencyCacheStateUnknown:
    @pytest.mark.asyncio
    async def test_returns_none_when_first_refresh_fails(
        self, tracker: VRAMTracker,
    ) -> None:
        """Cold cache + tracker returns None → propagate None, not empty set.

        Returning ``set()`` here would tell the scheduler 'no models resident',
        which is the same false signal that previously let the scheduler
        approve loads exceeding the VRAM budget.
        """
        async def fail_get_loaded():
            return None

        with patch.object(tracker, "get_loaded_models", side_effect=fail_get_loaded):
            cache = ResidencyCache(tracker, ttl_seconds=10.0)
            result = await cache.get_resident_models()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_stale_set_when_refresh_fails_after_prior_success(
        self, tracker: VRAMTracker,
    ) -> None:
        """Stale-OK semantics: prior known state survives a transient outage.

        A short Ollama hiccup during deploy shouldn't promote a known-good
        residency snapshot to 'unknown' — the cache may legitimately serve
        slightly stale data until the next successful refresh.
        """
        responses: list = [
            [LoadedModel(name="qwen3:14b", vram_gb=9.3)],
            None,
        ]

        async def varied_get_loaded():
            return responses.pop(0)

        with patch.object(tracker, "get_loaded_models", side_effect=varied_get_loaded):
            cache = ResidencyCache(tracker, ttl_seconds=0.01)
            first = await cache.get_resident_models()
            assert first == {"qwen3:14b"}

            await asyncio.sleep(0.02)  # Force TTL expiry on the next call
            second = await cache.get_resident_models()

        # Prior known state is preserved across the transient None
        assert second == {"qwen3:14b"}


class TestVRAMManagerReconcileStateUnknown:
    @pytest.mark.asyncio
    async def test_reconcile_with_none_does_not_wipe_ledger(
        self, tracker: VRAMTracker, config: BrokerConfig,
    ) -> None:
        """Passing ``None`` to reconcile must be a no-op.

        The scheduler invokes reconcile each tick with the resident set
        derived from /api/ps. When that set is None (state unknown), the
        ledger MUST be preserved — otherwise a transient outage wipes all
        per-model allocations and the next can_load_model gate would see
        an empty ledger and approve a budget-exceeding load.
        """
        total = 32 * 1024 * 1024 * 1024
        manager = VRAMManager(tracker, total, safety_margin_pct=10.0)

        # Establish a committed allocation in the ledger
        reservation = await manager.reserve("qwen3:14b", 9_000_000_000)
        await manager.commit(reservation)
        assert manager.allocated_bytes == 9_000_000_000
        assert "qwen3:14b" in manager._model_allocations

        # Reconcile under unknown state must not touch the ledger
        freed = await manager.reconcile(None)
        assert freed == 0
        assert manager.allocated_bytes == 9_000_000_000
        assert manager._model_allocations.get("qwen3:14b") == 9_000_000_000

    @pytest.mark.asyncio
    async def test_reconcile_with_empty_set_still_frees_stale(
        self, tracker: VRAMTracker,
    ) -> None:
        """An *empty* set (Ollama explicitly reports no models) still frees
        stale ledger entries — the safety semantics only kick in on ``None``
        (state unknown), not on a legitimate empty reading.
        """
        total = 32 * 1024 * 1024 * 1024
        manager = VRAMManager(tracker, total, safety_margin_pct=10.0)

        reservation = await manager.reserve("qwen3:14b", 9_000_000_000)
        await manager.commit(reservation)
        assert manager.allocated_bytes == 9_000_000_000

        # Empty set = Ollama is up and reports no models loaded → free the stale entry
        freed = await manager.reconcile(set())
        assert freed == 9_000_000_000
        assert manager.allocated_bytes == 0


class TestGetLoadedVramGbStateUnknown:
    @pytest.mark.asyncio
    async def test_returns_zero_and_skips_metric_on_unknown_state(
        self, tracker: VRAMTracker,
    ) -> None:
        """/api/ps unreachable → 0.0, and the bastion_vram_used_mb gauge must
        NOT be published (a '0 MB used' reading during an outage would
        mislead operators into thinking VRAM is free)."""
        async def fail_get_loaded():
            return None

        with patch.object(
            tracker, "get_loaded_models", side_effect=fail_get_loaded,
        ), patch("bastion.vram.update_vram_used_mb") as gauge:
            result = await tracker.get_loaded_vram_gb()

        assert result == 0.0
        gauge.assert_not_called()
