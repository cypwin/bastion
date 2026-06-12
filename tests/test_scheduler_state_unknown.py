"""Scheduler fail-closed behavior when VRAM tracker state is unknown.

Covers the four branches introduced with the None state-unknown sentinel
(S130 review: these were shipped untested — a regression that coerces the
sentinel to an empty set would silently re-enable dispatch-on-unknown-state,
the exact budget-overrun failure mode BASTION exists to prevent):

  1. _process_tick bails out and records stall reason 'tracker_state_unknown'
  2. _diagnose_stall guards the defensive None -> set() path
  3. post-swap proactive eviction is skipped when residency is unknown
  4. _evict_for_model refuses to evict on unknown state
  5. (S130 fix) _evict_for_model STOPS mid-loop when can_load_model starts
     failing closed with the state-unknown reason
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    PriorityTier,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAM_STATE_UNKNOWN_REASON, VRAMTracker
from tests.conftest import make_request

GB = 1024 ** 3


@pytest.fixture
def sched_config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            max_queue_size=32,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
        },
    )


def _lm(name: str, vram_gb: float) -> LoadedModel:
    return LoadedModel(
        name=name, size_bytes=int(vram_gb * GB), vram_gb=vram_gb, details={}
    )


def _make_scheduler(sched_config, dispatch_log) -> tuple[Scheduler, VRAMTracker]:
    tracker = VRAMTracker(sched_config)
    queue = AffinityQueue(sched_config.scheduler)

    async def dispatch_fn(request, needs_swap=True):
        dispatch_log.append(request)

    sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
    return sched, tracker


class TestTickBailsOnUnknownState:
    @pytest.mark.asyncio
    async def test_tick_dispatches_nothing_and_records_stall_reason(
        self, sched_config
    ):
        """Ollama restarts mid-tick: the tick must NOT make dispatch
        decisions on missing residency data."""
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        sched.queue.enqueue(
            make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        )

        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "bastion.scheduler.check_gpu_safe",
            AsyncMock(return_value=(True, "OK")),
        ):
            dispatched = await sched._process_tick()

        assert dispatched is False
        assert log == []
        assert sched._last_stall_reason == "tracker_state_unknown"

    @pytest.mark.asyncio
    async def test_stall_log_is_one_shot_across_consecutive_ticks(
        self, sched_config, caplog
    ):
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        sched.queue.enqueue(
            make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        )

        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "bastion.scheduler.check_gpu_safe",
            AsyncMock(return_value=(True, "OK")),
        ), caplog.at_level("INFO", logger="bastion.scheduler"):
            await sched._process_tick()
            await sched._process_tick()
            await sched._process_tick()

        hits = [
            r for r in caplog.records
            if "VRAM tracker state unknown" in r.message
        ]
        assert len(hits) == 1  # logged once, not per 100ms tick
        assert log == []


class TestDiagnoseStallDefensiveGuard:
    @pytest.mark.asyncio
    async def test_diagnose_stall_survives_unknown_state(self, sched_config):
        """Defensive None -> set() guard: helper must not raise if a future
        caller invokes it while state is unknown."""
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        sched.queue.enqueue(
            make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        )

        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=None,
        ):
            await sched._diagnose_stall()  # must not raise


class TestPostSwapEvictionSkippedOnUnknownState:
    @pytest.mark.asyncio
    async def test_no_proactive_unload_when_residency_unknown(
        self, sched_config
    ):
        """After a swap, the proactive max_loaded_models eviction must skip
        entirely when get_loaded_models() returns the None sentinel."""
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        sched._last_swap_time = 0.0  # no cooldown
        candidate = make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        sched.queue.enqueue(candidate)

        unload = AsyncMock(return_value=True)
        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            tracker, "can_load_model", new_callable=AsyncMock,
            return_value=(True, "OK"),
        ), patch.object(
            tracker, "log_vram_snapshot", new_callable=AsyncMock,
        ), patch.object(
            tracker, "get_loaded_vram_gb", new_callable=AsyncMock,
            return_value=0.0,
        ), patch.object(sched, "_unload_model", unload), patch.object(
            sched, "_dispatch_for_model",
            new_callable=AsyncMock, return_value=True,
        ):
            await sched._handle_swap_dispatch(candidate)

        unload.assert_not_called()


class TestEvictForModelFailClosed:
    @pytest.mark.asyncio
    async def test_refuses_to_evict_on_unknown_state(self, sched_config):
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        candidate = make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)

        unload = AsyncMock(return_value=True)
        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=None,
        ), patch.object(sched, "_unload_model", unload):
            freed = await sched._evict_for_model(candidate)

        assert freed is False
        unload.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_mid_loop_when_state_becomes_unknown(self, sched_config):
        """S130 fix: if can_load_model starts failing closed with the
        state-unknown reason mid-eviction, further unloads cannot succeed —
        the loop must stop after the first one, not tear down every
        resident."""
        log: list = []
        sched, tracker = _make_scheduler(sched_config, log)
        candidate = make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)

        resident = [_lm("mistral-nemo:12b", 8.1), _lm("extra:7b", 5.0)]
        unload = AsyncMock(return_value=True)
        with patch.object(
            tracker, "get_loaded_models", new_callable=AsyncMock,
            return_value=resident,
        ), patch.object(
            tracker, "can_load_model", new_callable=AsyncMock,
            return_value=(False, VRAM_STATE_UNKNOWN_REASON),
        ), patch.object(sched, "_unload_model", unload):
            freed = await sched._evict_for_model(candidate)

        assert freed is False
        assert unload.await_count == 1  # stopped after the first eviction
