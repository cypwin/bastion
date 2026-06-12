"""ResidencyCache flicker-hold debounce (S130).

Ollama's /api/ps returns partial views under concurrent inference: a busy,
resident model can be missing from 1-2 consecutive polls while serving warm
requests. These tests pin the declassification debounce that keeps such
models classified resident until they are missing from ``declassify_after``
consecutive successful refreshes — preventing phantom scheduler swaps,
spurious cooldowns, and reconcile ledger churn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bastion.models import BrokerConfig, GPUConfig, LoadedModel, ModelInfo
from bastion.vram import ResidencyCache, VRAMTracker

GB = 1024 ** 3


@pytest.fixture
def tracker() -> VRAMTracker:
    config = BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={
            "a:7b": ModelInfo(vram_gb=5.0),
            "b:8b": ModelInfo(vram_gb=6.0),
        },
    )
    return VRAMTracker(config)


def _lm(name: str, vram_gb: float = 5.0) -> LoadedModel:
    return LoadedModel(
        name=name, size_bytes=int(vram_gb * GB), vram_gb=vram_gb, details={}
    )


def _cache(tracker, **kwargs) -> ResidencyCache:
    # ttl 0 → every call refreshes, so each AsyncMock swap is one "poll".
    return ResidencyCache(tracker, ttl_seconds=0.0, **kwargs)


class TestFlickerHold:
    @pytest.mark.asyncio
    async def test_single_missing_poll_is_held(self, tracker):
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}

        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])  # flicker
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}  # b held

    @pytest.mark.asyncio
    async def test_two_consecutive_misses_declassify(self, tracker):
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        await cache.get_resident_models()

        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        await cache.get_resident_models()  # miss 1 — held
        assert await cache.get_resident_models() == {"a:7b"}  # miss 2 — gone

    @pytest.mark.asyncio
    async def test_reappearance_resets_miss_streak(self, tracker):
        cache = _cache(tracker)
        both = [_lm("a:7b"), _lm("b:8b")]
        only_a = [_lm("a:7b")]

        tracker.get_loaded_models = AsyncMock(return_value=both)
        await cache.get_resident_models()
        tracker.get_loaded_models = AsyncMock(return_value=only_a)
        await cache.get_resident_models()  # miss 1
        tracker.get_loaded_models = AsyncMock(return_value=both)
        await cache.get_resident_models()  # back — streak resets
        tracker.get_loaded_models = AsyncMock(return_value=only_a)
        # miss 1 again, NOT miss 2 — still held
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}

    @pytest.mark.asyncio
    async def test_new_model_accepted_immediately(self, tracker):
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        await cache.get_resident_models()
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}

    @pytest.mark.asyncio
    async def test_held_model_keeps_size_info_for_reconcile(self, tracker):
        """get_resident_loaded_models() must include the held LoadedModel so
        reconcile() doesn't lose size info mid-flicker."""
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b", 6.0)])
        await cache.get_resident_loaded_models()
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        loaded = await cache.get_resident_loaded_models()
        by_name = {m.name: m for m in loaded}
        assert by_name["b:8b"].vram_gb == 6.0


class TestDebounceBypass:
    @pytest.mark.asyncio
    async def test_invalidate_makes_next_read_authoritative(self, tracker):
        """BASTION-initiated unloads must declassify immediately: unload paths
        call invalidate(), so the next refresh is taken verbatim."""
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        await cache.get_resident_models()

        cache.invalidate()
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        assert await cache.get_resident_models() == {"a:7b"}  # no hold

    @pytest.mark.asyncio
    async def test_declassify_after_one_disables_holding(self, tracker):
        cache = _cache(tracker, declassify_after=1)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        await cache.get_resident_models()
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        assert await cache.get_resident_models() == {"a:7b"}


class TestInteractionWithStateUnknown:
    @pytest.mark.asyncio
    async def test_none_refresh_does_not_count_as_miss(self, tracker):
        """A failed /api/ps read (state unknown) is stale-OK territory, not a
        flicker miss — the streak only advances on successful partial reads."""
        cache = _cache(tracker)
        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b"), _lm("b:8b")])
        await cache.get_resident_models()

        tracker.get_loaded_models = AsyncMock(return_value=None)  # outage
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}  # stale-OK

        tracker.get_loaded_models = AsyncMock(return_value=[_lm("a:7b")])
        # First successful partial read after the outage = miss 1 — held.
        assert await cache.get_resident_models() == {"a:7b", "b:8b"}
