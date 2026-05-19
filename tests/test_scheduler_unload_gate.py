"""Regression tests for the unload return-value gate in the scheduler.

Per KNOWN_ISSUES.md (Important, resolved in v0.4.1): the 901c910 fix made
``VRAMTracker.unload_model()`` honest about whether VRAM actually converged
after the unload request. But the scheduler's ``_unload_model`` was still
returning ``None`` and ignoring the result — so eviction could silently fail
and the loop kept paying the cost of ``wait_for_vram_convergence()`` and
``can_load_model()`` on iterations where no VRAM was actually freed.

The contract these tests pin:

  1. ``Scheduler._unload_model`` propagates the bool from ``vram.unload_model``
     rather than returning ``None``.
  2. ``Scheduler._evict_for_model`` does NOT call ``can_load_model`` or
     ``wait_for_vram_convergence`` after a failed unload — those checks
     are only meaningful when an unload actually freed VRAM.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMManager, VRAMTracker
from tests.conftest import make_request


@pytest.fixture
def cfg() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(cooldown_seconds=0.0, max_queue_size=32),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            "llama3.1:8b": ModelInfo(vram_gb=4.4),
        },
    )


class TestUnloadReturnGate:
    @pytest.mark.asyncio
    async def test_unload_model_returns_false_when_vram_unload_fails(
        self, cfg: BrokerConfig,
    ) -> None:
        """``_unload_model`` MUST return the bool from ``vram.unload_model``.

        Returning ``None`` (the old behavior) lost the "no convergence" signal
        that ``unload_model`` was specifically designed to surface, so callers
        couldn't distinguish a real eviction from a no-op.
        """
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=False,
        ):
            sched = Scheduler(cfg, queue, tracker, dispatch_fn)
            result = await sched._unload_model("qwen3:14b")

        assert result is False, (
            f"_unload_model lost the failure signal: returned {result!r} "
            "(must propagate vram.unload_model's bool)"
        )

    @pytest.mark.asyncio
    async def test_unload_model_returns_true_when_vram_unload_succeeds(
        self, cfg: BrokerConfig,
    ) -> None:
        """Symmetric: successful unload also surfaces as True (not None)."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        total = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total, safety_margin_pct=10.0)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ), patch.object(
            mgr, "wait_for_vram_convergence", new_callable=AsyncMock, return_value=True,
        ):
            sched = Scheduler(cfg, queue, tracker, dispatch_fn, vram_manager=mgr)
            result = await sched._unload_model("qwen3:14b")

        assert result is True

    @pytest.mark.asyncio
    async def test_evict_for_model_skips_post_unload_work_when_unload_fails(
        self, cfg: BrokerConfig,
    ) -> None:
        """When unload returns False, ``_evict_for_model`` MUST NOT call
        ``can_load_model`` or ``wait_for_vram_convergence`` for that iteration.

        Those checks model "post-unload state transition" and their precondition
        (VRAM was actually freed) wasn't met. Calling them anyway wastes a
        nvidia-smi query each tick and produces misleading "evicted N models"
        log lines when N is the count of *attempted* (not successful) unloads.
        """
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        total = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total, safety_margin_pct=10.0)

        loaded = [
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]
        can_load_calls: list[str] = []
        convergence_calls: list[int] = []

        async def mock_can_load(model: str):
            can_load_calls.append(model)
            return (False, "still no space")

        async def mock_convergence(*args, **kwargs):
            convergence_calls.append(1)
            return True

        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock, return_value=loaded,
        ), patch.object(
            tracker, "can_load_model", side_effect=mock_can_load,
        ), patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=False,
        ), patch.object(
            mgr, "wait_for_vram_convergence", side_effect=mock_convergence,
        ):
            sched = Scheduler(cfg, queue, tracker, dispatch_fn, vram_manager=mgr)
            candidate = make_request(model="llama3.1:8b")
            result = await sched._evict_for_model(candidate)

        assert result is False, "no eviction succeeded — must return False"
        assert can_load_calls == [], (
            f"can_load_model was called after failed unloads: {can_load_calls}. "
            "The gate must skip post-unload work when unload returned False."
        )
        assert convergence_calls == [], (
            f"wait_for_vram_convergence was called after failed unloads: "
            f"{len(convergence_calls)} times. Convergence only makes sense after "
            "a successful unload — skip it otherwise."
        )

    @pytest.mark.asyncio
    async def test_evict_for_model_proceeds_after_successful_unload(
        self, cfg: BrokerConfig,
    ) -> None:
        """Mixed case: first unload fails, second succeeds → ``_evict_for_model``
        only counts the second and proceeds to can_load_model after it.
        """
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        total = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total, safety_margin_pct=10.0)

        loaded = [
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]
        unload_returns = {"qwen3:14b": False, "mistral-nemo:12b": True}
        can_load_calls: list[str] = []

        async def mock_can_load(model: str):
            can_load_calls.append(model)
            # Approve after the successful unload (call #1)
            return (True, "OK") if len(can_load_calls) >= 1 else (False, "no space")

        async def mock_unload(model: str) -> bool:
            return unload_returns[model]

        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock, return_value=loaded,
        ), patch.object(
            tracker, "can_load_model", side_effect=mock_can_load,
        ), patch.object(
            tracker, "unload_model", side_effect=mock_unload,
        ), patch.object(
            mgr, "wait_for_vram_convergence", new_callable=AsyncMock, return_value=True,
        ):
            sched = Scheduler(cfg, queue, tracker, dispatch_fn, vram_manager=mgr)
            candidate = make_request(model="llama3.1:8b")
            result = await sched._evict_for_model(candidate)

        assert result is True
        # can_load_model called exactly once — only after the *successful* unload
        assert len(can_load_calls) == 1, (
            f"can_load_model call count off: {can_load_calls} "
            "(expected 1 — only after the successful unload)"
        )
