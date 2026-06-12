"""Tests for Scheduler — dispatch, cooldown, model swaps, GPU gating."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
    PriorityTier,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMManager, VRAMTracker
from tests.conftest import make_request


def _noop_has_inflight(model: str) -> bool:
    """Default: no models have in-flight requests."""
    return False


def _noop_inflight_count() -> int:
    """Default: no in-flight requests."""
    return 0


@pytest.fixture
def sched_config() -> BrokerConfig:
    """Config tuned for fast scheduler tests."""
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.05,
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=32,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            "nomic-embed-text": ModelInfo(vram_gb=0.4, always_allowed=True),
        },
    )


@pytest.fixture
def dispatch_log():
    """Collects dispatched requests for assertions."""
    log = []

    async def dispatch_fn(request, needs_swap=True):
        log.append(request)

    return log, dispatch_fn


def _safe_gpu():
    return GPUStatus(
        temperature_c=50, vram_used_mb=8000, vram_free_mb=24000,
        vram_total_mb=32000, power_draw_watts=150.0,
    )


class TestSchedulerStartStop:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, sched_config, dispatch_log):
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            assert sched.is_running is True
            await sched.stop()
            assert sched.is_running is False

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self, sched_config, dispatch_log):
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            await sched.start()  # Should not create second task
            assert sched.is_running is True
            await sched.stop()


class TestSchedulerDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_enqueued_request(self, sched_config, dispatch_log):
        """Enqueue a request, scheduler should dispatch it."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        req = make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        queue.enqueue(req)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Wait for scheduler to process
            for _ in range(50):
                await asyncio.sleep(0.02)
                if len(log) > 0:
                    break

            await sched.stop()

        assert len(log) == 1
        assert log[0].id == req.id
        assert sched.total_dispatched == 1

    @pytest.mark.asyncio
    async def test_multiple_same_model_no_swap(self, sched_config, dispatch_log):
        """Multiple requests for the same model should not cause swaps."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        for _ in range(3):
            queue.enqueue(make_request(model="qwen3:14b"))

        # Mock: model is already loaded (simulating it was pre-loaded or from a previous request)
        # All three requests see the model as resident, so no swaps occur
        loaded_model = LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={})

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=[loaded_model],
             ), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(log) >= 3:
                    break

            await sched.stop()

        assert len(log) == 3
        # Model is resident for all requests, no swaps occur
        assert sched.total_swaps == 0


class TestGPUGating:
    @pytest.mark.asyncio
    async def test_pauses_when_gpu_unsafe(self, sched_config, dispatch_log):
        """Scheduler should not dispatch when GPU is unsafe."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        queue.enqueue(make_request(model="qwen3:14b"))

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch(
                 "bastion.scheduler.check_gpu_safe",
                 AsyncMock(return_value=(False, "GPU too hot")),
             ):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Give it time — should NOT dispatch
            await asyncio.sleep(0.3)
            await sched.stop()

        assert len(log) == 0


class TestGPUGatingMidSwap:
    """The GPU-hot gate must be re-checked inside the swap path.

    The top-of-tick check_gpu_safe happens many awaits before the actual
    swap dispatch (eviction, VRAM reservation). A GPU that transitions hot
    in that window must abort the swap — exactly the load-cycle BASTION
    exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_swap_aborts_when_gpu_hot_at_dispatch_time(
        self, sched_config, dispatch_log,
    ):
        """_handle_swap_dispatch with a hot GPU must not dispatch."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        queue.enqueue(make_request(model="qwen3:14b"))

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch(
                 "bastion.scheduler.check_gpu_safe",
                 AsyncMock(return_value=(False, "GPU too hot (mid-swap)")),
             ), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            sched._last_swap_time = 0.0

            candidate = queue.pick_next(None)
            result = await sched._handle_swap_dispatch(candidate)

        assert result is False
        assert len(log) == 0
        assert sched.total_swaps == 0

    @pytest.mark.asyncio
    async def test_swap_abort_releases_vram_reservation(self, sched_config, dispatch_log):
        """A reservation made before the GPU went hot must be released on abort."""
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)

        queue.enqueue(make_request(model="qwen3:14b"))

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch(
                 "bastion.scheduler.check_gpu_safe",
                 AsyncMock(return_value=(False, "GPU too hot (mid-swap)")),
             ), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn, vram_manager=mgr)
            sched._last_swap_time = 0.0

            candidate = queue.pick_next(None)
            result = await sched._handle_swap_dispatch(candidate)

        assert result is False
        assert len(log) == 0
        assert mgr.reserved_bytes == 0
        assert mgr.allocated_bytes == 0


class TestDrainMode:
    @pytest.mark.asyncio
    async def test_drain_mode(self, sched_config, dispatch_log):
        log, dispatch_fn = dispatch_log
        queue = AffinityQueue(sched_config.scheduler)
        tracker = VRAMTracker(sched_config)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]):
            sched = Scheduler(sched_config, queue, tracker, dispatch_fn)
            await sched.start()
            await sched.drain()
            assert sched.is_draining is True
            await sched.resume()
            assert sched.is_draining is False
            await sched.stop()


class TestConcurrentDispatch:
    """Tests for concurrent dispatch to co-resident models."""

    @pytest.fixture
    def concurrent_config(self) -> BrokerConfig:
        """Config with 3 co-resident council models."""
        return BrokerConfig(
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
            scheduler=SchedulerConfig(
                cooldown_seconds=0.05,
                model_affinity_bonus=10.0,
                aging_rate=2.0,
                max_queue_size=32,
                max_concurrent_dispatches=3,
            ),
            models={
                "granite3.1-dense:8b": ModelInfo(vram_gb=5.2),
                "llama3.1:8b": ModelInfo(vram_gb=4.4),
                "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            },
        )

    @pytest.mark.asyncio
    async def test_concurrent_dispatch_coresident(self, concurrent_config):
        """3 co-resident models with queued requests should all dispatch without
        waiting for completion (non-blocking path)."""
        dispatched = []
        dispatch_calls = []

        async def dispatch_fn(request, needs_swap=True):
            dispatched.append(request)
            dispatch_calls.append({"model": request.model, "needs_swap": needs_swap})

        queue = AffinityQueue(concurrent_config.scheduler)
        tracker = VRAMTracker(concurrent_config)

        # Enqueue one request per model
        queue.enqueue(make_request(model="granite3.1-dense:8b"))
        queue.enqueue(make_request(model="llama3.1:8b"))
        queue.enqueue(make_request(model="mistral-nemo:12b"))

        # Mock: all 3 models are resident
        loaded = [
            LoadedModel(name="granite3.1-dense:8b", vram_gb=5.2),
            LoadedModel(name="llama3.1:8b", vram_gb=4.4),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ):
            sched = Scheduler(concurrent_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            # Wait for scheduler to process all 3
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(dispatched) >= 3:
                    break

            await sched.stop()

        assert len(dispatched) == 3
        models_dispatched = {r.model for r in dispatched}
        assert models_dispatched == {"granite3.1-dense:8b", "llama3.1:8b", "mistral-nemo:12b"}

        # All should be non-blocking (needs_swap=False) since they are co-resident
        for call in dispatch_calls:
            assert call["needs_swap"] is False, f"Expected non-blocking for {call['model']}"

        # No swaps — all models were already resident
        assert sched.total_swaps == 0

    @pytest.mark.asyncio
    async def test_same_model_serialized(self, concurrent_config):
        """2 requests for the same resident model: second should block because
        has_inflight returns True after the first dispatch."""
        dispatched = []
        dispatch_order = []

        async def dispatch_fn(request, needs_swap=True):
            dispatched.append(request)
            dispatch_order.append({"model": request.model, "needs_swap": needs_swap})

        queue = AffinityQueue(concurrent_config.scheduler)
        tracker = VRAMTracker(concurrent_config)

        # 2 requests for same model
        req1 = make_request(model="granite3.1-dense:8b")
        req2 = make_request(model="granite3.1-dense:8b")
        queue.enqueue(req1)
        queue.enqueue(req2)

        loaded = [LoadedModel(name="granite3.1-dense:8b", vram_gb=5.2)]

        # After first dispatch, has_inflight should return True for granite
        def mock_has_inflight(model):
            return bool(model == "granite3.1-dense:8b" and len(dispatched) >= 1)

        def mock_inflight_count():
            return len(dispatched)

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ):
            sched = Scheduler(
                concurrent_config, queue, tracker, dispatch_fn,
                has_inflight_fn=mock_has_inflight,
                inflight_count_fn=mock_inflight_count,
            )
            await sched.start()
            sched.notify()

            # Wait for first dispatch
            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(dispatched) >= 1:
                    break

            await sched.stop()

        # First request dispatches, second stays queued because same-model is in-flight
        assert len(dispatched) >= 1
        assert dispatched[0].model == "granite3.1-dense:8b"

    @pytest.mark.asyncio
    async def test_swap_blocks_dispatch(self, concurrent_config):
        """Non-resident model request should use blocking dispatch (needs_swap=True)."""
        dispatched = []
        dispatch_calls = []

        async def dispatch_fn(request, needs_swap=True):
            dispatched.append(request)
            dispatch_calls.append({"model": request.model, "needs_swap": needs_swap})

        queue = AffinityQueue(concurrent_config.scheduler)
        tracker = VRAMTracker(concurrent_config)

        # Request for a non-resident model
        queue.enqueue(make_request(model="mistral-nemo:12b"))

        # No models resident
        loaded = []

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ):
            sched = Scheduler(concurrent_config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            for _ in range(100):
                await asyncio.sleep(0.02)
                if len(dispatched) >= 1:
                    break

            await sched.stop()

        assert len(dispatched) == 1
        assert dispatched[0].model == "mistral-nemo:12b"
        # Should be blocking (swap needed)
        assert dispatch_calls[0]["needs_swap"] is True
        assert sched.total_swaps == 1

    @pytest.mark.asyncio
    async def test_inflight_prevents_eviction(self, concurrent_config):
        """Model with in-flight request should not be evicted."""
        unloaded_models = []

        async def dispatch_fn(request, needs_swap=True):
            pass

        queue = AffinityQueue(concurrent_config.scheduler)
        tracker = VRAMTracker(concurrent_config)

        # granite has inflight, try to evict it to load a large model
        loaded = [
            LoadedModel(name="granite3.1-dense:8b", vram_gb=5.2),
            LoadedModel(name="llama3.1:8b", vram_gb=4.4),
        ]

        async def mock_unload(model_name):
            unloaded_models.append(model_name)
            return True

        def mock_has_inflight(model):
            # granite is in-flight
            return model == "granite3.1-dense:8b"

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch.object(tracker, "unload_model", side_effect=mock_unload), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))):
            sched = Scheduler(
                concurrent_config, queue, tracker, dispatch_fn,
                has_inflight_fn=mock_has_inflight,
                inflight_count_fn=lambda: 1,
            )
            sched._current_model = "granite3.1-dense:8b"

            # Try to unload granite (has inflight) — should be deferred
            await sched._unload_model("granite3.1-dense:8b")

            # granite should NOT have been unloaded
            assert "granite3.1-dense:8b" not in unloaded_models

            # llama should be unloadable (no inflight)
            await sched._unload_model("llama3.1:8b")
            assert "llama3.1:8b" in unloaded_models


# ---------------------------------------------------------------------------
# Swap cooldown dynamic escalation tests
# ---------------------------------------------------------------------------


class TestSwapCooldown:
    """Tests for _get_swap_cooldown() dynamic escalation logic."""

    @pytest.fixture
    def cooldown_config(self) -> BrokerConfig:
        """Config with known swap rate thresholds for deterministic tests."""
        return BrokerConfig(
            scheduler=SchedulerConfig(
                cooldown_seconds=0.5,
                swap_rate_window_seconds=60.0,
                swap_rate_warn_threshold=4,
                swap_rate_critical_threshold=6,
                swap_rate_warn_cooldown_seconds=5.0,
                swap_rate_critical_cooldown_seconds=10.0,
            ),
            models={
                "qwen3:14b": ModelInfo(vram_gb=9.3),
            },
        )

    def _make_scheduler(self, config: BrokerConfig) -> Scheduler:
        """Create a Scheduler with noop dispatch for unit tests."""
        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)

        async def noop_dispatch(request, needs_swap=True) -> None:
            pass

        return Scheduler(config, queue, tracker, noop_dispatch)

    def test_normal_cooldown_when_no_swaps(self, cooldown_config: BrokerConfig) -> None:
        """With no recent swaps, cooldown should be the base value."""
        sched = self._make_scheduler(cooldown_config)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.cooldown_seconds

    def test_normal_cooldown_below_warn_threshold(self, cooldown_config: BrokerConfig) -> None:
        """With fewer swaps than warn_threshold, cooldown stays at base."""
        sched = self._make_scheduler(cooldown_config)
        now = time.time()
        # Add 3 swaps (below warn threshold of 4)
        for i in range(3):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.cooldown_seconds

    def test_warn_cooldown_at_warn_threshold(self, cooldown_config: BrokerConfig) -> None:
        """At warn threshold, cooldown should escalate to warn level."""
        sched = self._make_scheduler(cooldown_config)
        now = time.time()
        # Add exactly 4 swaps (= warn threshold)
        for i in range(4):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.swap_rate_warn_cooldown_seconds

    def test_critical_cooldown_at_critical_threshold(self, cooldown_config: BrokerConfig) -> None:
        """At critical threshold, cooldown should escalate to critical level."""
        sched = self._make_scheduler(cooldown_config)
        now = time.time()
        # Add 6 swaps (= critical threshold)
        for i in range(6):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.swap_rate_critical_cooldown_seconds

    def test_old_timestamps_are_pruned(self, cooldown_config: BrokerConfig) -> None:
        """Timestamps older than the window should be pruned."""
        sched = self._make_scheduler(cooldown_config)
        now = time.time()
        window = cooldown_config.scheduler.swap_rate_window_seconds
        # Add 8 swaps, all outside the window
        for i in range(8):
            sched._swap_timestamps.append(now - window - 10 - i)
        cooldown = sched._get_swap_cooldown()
        # All pruned, should be normal cooldown
        assert cooldown == cooldown_config.scheduler.cooldown_seconds
        assert len(sched._swap_timestamps) == 0

    def test_level_transition_is_tracked(self, cooldown_config: BrokerConfig) -> None:
        """Swap rate level transitions should update _swap_rate_level."""
        sched = self._make_scheduler(cooldown_config)
        assert sched._swap_rate_level == "normal"
        now = time.time()
        for i in range(4):
            sched._swap_timestamps.append(now - i)
        sched._get_swap_cooldown()
        assert sched._swap_rate_level == "warn"

    def test_critical_to_normal_transition(self, cooldown_config: BrokerConfig) -> None:
        """After timestamps expire, level should drop back to normal."""
        sched = self._make_scheduler(cooldown_config)
        now = time.time()
        window = cooldown_config.scheduler.swap_rate_window_seconds
        # Set up critical level
        for i in range(6):
            sched._swap_timestamps.append(now - i)
        sched._get_swap_cooldown()
        assert sched._swap_rate_level == "critical"
        # Now expire all timestamps
        sched._swap_timestamps.clear()
        for i in range(6):
            sched._swap_timestamps.append(now - window - 1 - i)
        sched._get_swap_cooldown()
        assert sched._swap_rate_level == "normal"


# ---------------------------------------------------------------------------
# Swap dispatch with VRAM reservation tests
# ---------------------------------------------------------------------------


class TestHandleSwapDispatch:
    """Tests for _handle_swap_dispatch() VRAM reservation path."""

    @pytest.fixture(autouse=True)
    def _gpu_safe(self):
        """Pin the mid-swap GPU gate safe — these tests target the
        reservation lifecycle, not GPU gating (see TestGPUGatingMidSwap)."""
        with patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))):
            yield

    @pytest.fixture
    def swap_config(self) -> BrokerConfig:
        return BrokerConfig(
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
            scheduler=SchedulerConfig(
                cooldown_seconds=0.0,  # No cooldown for deterministic tests
                max_queue_size=32,
            ),
            models={
                "qwen3:14b": ModelInfo(vram_gb=9.3),
                "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            },
        )

    @pytest.mark.asyncio
    async def test_reserve_succeeds_dispatch_succeeds_commits(
        self, swap_config: BrokerConfig,
    ) -> None:
        """reserve OK -> dispatch OK -> commit reservation."""
        dispatched = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            dispatched.append(request)

        queue = AffinityQueue(swap_config.scheduler)
        tracker = VRAMTracker(swap_config)
        total_bytes = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total_bytes, safety_margin_pct=10.0)

        req = make_request(model="qwen3:14b")
        queue.enqueue(req)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(swap_config, queue, tracker, dispatch_fn, vram_manager=mgr)
            sched._last_swap_time = 0.0  # Ensure cooldown is satisfied

            candidate = queue.pick_next(None)
            result = await sched._handle_swap_dispatch(candidate)

        assert result is True
        assert len(dispatched) == 1
        # Reservation should be committed (no pending reservations)
        assert mgr.reserved_bytes == 0
        assert mgr.allocated_bytes > 0

    @pytest.mark.asyncio
    async def test_reserve_succeeds_dispatch_fails_releases(
        self, swap_config: BrokerConfig,
    ) -> None:
        """reserve OK -> dispatch fails -> release reservation."""

        async def failing_dispatch(request, needs_swap=True) -> None:
            raise RuntimeError("dispatch failed")

        queue = AffinityQueue(swap_config.scheduler)
        tracker = VRAMTracker(swap_config)
        total_bytes = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total_bytes, safety_margin_pct=10.0)

        req = make_request(model="qwen3:14b")
        queue.enqueue(req)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(swap_config, queue, tracker, failing_dispatch, vram_manager=mgr)
            sched._last_swap_time = 0.0

            candidate = queue.pick_next(None)
            # dispatch_fn raises -> _dispatch_for_model catches and returns False,
            # so _handle_swap_dispatch releases the reservation
            result = await sched._handle_swap_dispatch(candidate)

        # Dispatch failed -> reservation released
        assert result is False
        assert mgr.reserved_bytes == 0
        assert mgr.allocated_bytes == 0

    @pytest.mark.asyncio
    async def test_reserve_fails_evict_retry_succeeds(self, swap_config: BrokerConfig) -> None:
        """reserve fails -> evict -> retry reserve -> dispatch -> commit."""
        dispatched = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            dispatched.append(request)

        queue = AffinityQueue(swap_config.scheduler)
        tracker = VRAMTracker(swap_config)
        total_bytes = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total_bytes, safety_margin_pct=10.0)

        # Fill up VRAM so reserve fails initially
        mgr._allocated = total_bytes - mgr._safety_margin - 1024  # Almost full

        req = make_request(model="qwen3:14b")
        queue.enqueue(req)

        # Mock eviction to free VRAM
        async def mock_evict(candidate):
            mgr._allocated = 0  # Free all VRAM
            return True

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(True, "OK"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(swap_config, queue, tracker, dispatch_fn, vram_manager=mgr)
            sched._last_swap_time = 0.0
            sched._evict_for_model = mock_evict  # type: ignore[assignment]

            candidate = queue.pick_next(None)
            result = await sched._handle_swap_dispatch(candidate)

        assert result is True
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_reserve_fails_after_eviction_returns_false(
        self, swap_config: BrokerConfig,
    ) -> None:
        """reserve fails -> evict fails -> return False."""

        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(swap_config.scheduler)
        tracker = VRAMTracker(swap_config)
        total_bytes = 32 * 1024 * 1024 * 1024
        mgr = VRAMManager(tracker, total_bytes, safety_margin_pct=10.0)
        mgr._allocated = total_bytes  # Completely full

        req = make_request(model="qwen3:14b")
        queue.enqueue(req)

        async def mock_evict_fails(candidate):
            return False

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]), \
             patch.object(
                 tracker, "can_load_model",
                 new_callable=AsyncMock, return_value=(False, "no space"),
             ), \
             patch.object(tracker, "log_vram_snapshot", new_callable=AsyncMock), \
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0):
            sched = Scheduler(swap_config, queue, tracker, dispatch_fn, vram_manager=mgr)
            sched._last_swap_time = 0.0
            sched._evict_for_model = mock_evict_fails  # type: ignore[assignment]

            candidate = queue.pick_next(None)
            result = await sched._handle_swap_dispatch(candidate)

        assert result is False


# ---------------------------------------------------------------------------
# Eviction strategy tests
# ---------------------------------------------------------------------------


class TestEvictForModel:
    """Tests for _evict_for_model() eviction strategy."""

    @pytest.fixture
    def evict_config(self) -> BrokerConfig:
        return BrokerConfig(
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
            scheduler=SchedulerConfig(cooldown_seconds=0.0, max_queue_size=32),
            models={
                "qwen3:14b": ModelInfo(vram_gb=9.3),
                "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
                "llama3.1:8b": ModelInfo(vram_gb=4.4),
                "nomic-embed-text": ModelInfo(vram_gb=0.4, always_allowed=True),
            },
        )

    @pytest.mark.asyncio
    async def test_evicts_model_with_no_queued_requests_first(
        self, evict_config: BrokerConfig,
    ) -> None:
        """Models with no queued requests should be evicted before those with requests."""
        unloaded = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(evict_config.scheduler)
        tracker = VRAMTracker(evict_config)

        # Enqueue requests for mistral so it has queued work
        queue.enqueue(make_request(model="mistral-nemo:12b"))

        loaded = [
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]

        can_load_calls = [0]

        async def mock_can_load(model):
            can_load_calls[0] += 1
            if can_load_calls[0] >= 2:
                return (True, "OK")
            return (False, "no space")

        async def mock_unload(model):
            unloaded.append(model)
            return True

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch.object(tracker, "can_load_model", side_effect=mock_can_load), \
             patch.object(tracker, "unload_model", side_effect=mock_unload):
            sched = Scheduler(evict_config, queue, tracker, dispatch_fn)
            candidate = make_request(model="llama3.1:8b")
            result = await sched._evict_for_model(candidate)

        assert result is True
        # qwen3:14b has no queued requests, should be evicted first
        assert unloaded[0] == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_always_allowed_never_evicted(self, evict_config: BrokerConfig) -> None:
        """Models with always_allowed=True should never be evicted."""
        unloaded = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(evict_config.scheduler)
        tracker = VRAMTracker(evict_config)

        loaded = [
            LoadedModel(name="nomic-embed-text", vram_gb=0.4),
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
        ]

        async def mock_can_load(model):
            return (True, "OK")

        async def mock_unload(model):
            unloaded.append(model)
            return True

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch.object(tracker, "can_load_model", side_effect=mock_can_load), \
             patch.object(tracker, "unload_model", side_effect=mock_unload):
            sched = Scheduler(evict_config, queue, tracker, dispatch_fn)
            candidate = make_request(model="mistral-nemo:12b")
            await sched._evict_for_model(candidate)

        # nomic-embed-text is always_allowed and should never be evicted
        assert "nomic-embed-text" not in unloaded

    @pytest.mark.asyncio
    async def test_reserved_models_protected(self, evict_config: BrokerConfig) -> None:
        """Models with active A2A reservations should not be evicted."""
        unloaded = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(evict_config.scheduler)
        tracker = VRAMTracker(evict_config)

        loaded = [
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]

        def reservation_check(model: str) -> bool:
            return model == "qwen3:14b"

        async def mock_can_load(model):
            return (True, "OK")

        async def mock_unload(model):
            unloaded.append(model)
            return True

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch.object(tracker, "can_load_model", side_effect=mock_can_load), \
             patch.object(tracker, "unload_model", side_effect=mock_unload):
            sched = Scheduler(
                evict_config, queue, tracker, dispatch_fn,
                reservation_check_fn=reservation_check,
            )
            candidate = make_request(model="llama3.1:8b")
            await sched._evict_for_model(candidate)

        # qwen3:14b has a reservation, should not be evicted
        assert "qwen3:14b" not in unloaded

    @pytest.mark.asyncio
    async def test_all_models_protected_returns_false(self, evict_config: BrokerConfig) -> None:
        """When all models are protected (always_allowed, reserved, in-flight), return False."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(evict_config.scheduler)
        tracker = VRAMTracker(evict_config)

        loaded = [
            LoadedModel(name="nomic-embed-text", vram_gb=0.4),  # always_allowed
        ]

        async def mock_can_load(model):
            return (False, "no space")

        with patch.object(
                 tracker, "get_loaded_models",
                 new_callable=AsyncMock, return_value=loaded,
             ), \
             patch.object(tracker, "can_load_model", side_effect=mock_can_load), \
             patch.object(tracker, "unload_model", new_callable=AsyncMock):
            sched = Scheduler(evict_config, queue, tracker, dispatch_fn)
            candidate = make_request(model="qwen3:14b")
            result = await sched._evict_for_model(candidate)

        assert result is False

    @pytest.mark.asyncio
    async def test_eviction_stuck_streak_increments_then_clears(
        self, evict_config: BrokerConfig,
    ) -> None:
        """T3.2: _evict_for_model maintains a per-candidate stuck-streak counter
        that grows on consecutive failures and clears on success.  Used to
        suppress log spam when all resident models are temporarily un-evictable."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(evict_config.scheduler)
        tracker = VRAMTracker(evict_config)

        # Only an always_allowed model is loaded so eviction is impossible.
        loaded_protected = [LoadedModel(name="nomic-embed-text", vram_gb=0.4)]
        # Recovery scenario: an evictable model also appears, freeing space.
        loaded_recovered = [
            LoadedModel(name="nomic-embed-text", vram_gb=0.4),
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
        ]

        get_loaded_mock = AsyncMock(side_effect=[
            loaded_protected,   # 1st failure
            loaded_protected,   # 2nd failure
            loaded_recovered,   # 3rd call: succeeds
        ])

        can_load_calls = [0]

        async def mock_can_load(model):
            can_load_calls[0] += 1
            # First two _evict calls fail (empty evictable, returns False
            # before reaching can_load).  Third call evicts qwen3:14b and
            # then queries can_load which returns True.
            return (True, "OK") if can_load_calls[0] >= 1 else (False, "no space")

        with patch.object(
            tracker, "get_loaded_models", side_effect=get_loaded_mock,
        ), patch.object(
            tracker, "can_load_model", side_effect=mock_can_load,
        ), patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ):
            sched = Scheduler(evict_config, queue, tracker, dispatch_fn)
            candidate = make_request(model="llama3.1:8b")

            # First failed eviction: streak 0 -> 1
            r1 = await sched._evict_for_model(candidate)
            assert r1 is False
            assert sched._eviction_stuck_streak["llama3.1:8b"] == 1

            # Second failed eviction: streak 1 -> 2 (log suppressed)
            r2 = await sched._evict_for_model(candidate)
            assert r2 is False
            assert sched._eviction_stuck_streak["llama3.1:8b"] == 2

            # Third call: evictable list non-empty, unload succeeds, can_load
            # returns True; streak cleared.
            r3 = await sched._evict_for_model(candidate)
            assert r3 is True
            assert "llama3.1:8b" not in sched._eviction_stuck_streak


# ---------------------------------------------------------------------------
# Sync current model on startup tests
# ---------------------------------------------------------------------------


class TestSyncCurrentModel:
    """Tests for _sync_current_model() startup behavior."""

    @pytest.fixture
    def sync_config(self) -> BrokerConfig:
        return BrokerConfig(
            scheduler=SchedulerConfig(cooldown_seconds=0.0),
            models={
                "qwen3:14b": ModelInfo(vram_gb=9.3),
                "llama3.1:8b": ModelInfo(vram_gb=4.4),
            },
        )

    @pytest.mark.asyncio
    async def test_picks_largest_model_as_current(self, sync_config: BrokerConfig) -> None:
        """With multiple models loaded, the largest should become current."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(sync_config.scheduler)
        tracker = VRAMTracker(sync_config)

        loaded = [
            LoadedModel(name="llama3.1:8b", vram_gb=4.4),
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
        ]

        with patch.object(
            tracker, "get_loaded_models",
            new_callable=AsyncMock, return_value=loaded,
        ):
            sched = Scheduler(sync_config, queue, tracker, dispatch_fn)
            await sched._sync_current_model()

        assert sched.current_model == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_no_models_sets_none(self, sync_config: BrokerConfig) -> None:
        """With no models loaded, current should be None."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(sync_config.scheduler)
        tracker = VRAMTracker(sync_config)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]):
            sched = Scheduler(sync_config, queue, tracker, dispatch_fn)
            await sched._sync_current_model()

        assert sched.current_model is None

    @pytest.mark.asyncio
    async def test_tracker_failure_sets_none(self, sync_config: BrokerConfig) -> None:
        """If VRAMTracker fails, current should gracefully fall back to None."""
        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(sync_config.scheduler)
        tracker = VRAMTracker(sync_config)

        with patch.object(
            tracker, "get_loaded_models",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ):
            sched = Scheduler(sync_config, queue, tracker, dispatch_fn)
            await sched._sync_current_model()

        assert sched.current_model is None


# ---------------------------------------------------------------------------
# Stop timeout tests
# ---------------------------------------------------------------------------


class TestStopTimeout:
    """Tests for scheduler stop() timeout handling."""

    @pytest.mark.asyncio
    async def test_stop_cancels_on_timeout(self) -> None:
        """If the scheduler loop doesn't stop within timeout, the task should be cancelled."""
        config = BrokerConfig(
            scheduler=SchedulerConfig(
                cooldown_seconds=0.0,
                shutdown_timeout_seconds=0.1,  # Very short timeout
                loop_interval_seconds=10.0,  # Long interval so loop blocks
            ),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )

        async def dispatch_fn(request, needs_swap=True) -> None:
            pass

        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)

        with patch.object(tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[]):
            sched = Scheduler(config, queue, tracker, dispatch_fn)
            await sched.start()
            assert sched.is_running is True

            # Replace the loop task with one that sleeps forever
            sched._task.cancel()
            await asyncio.sleep(0.01)

            async def hang_forever() -> None:
                await asyncio.sleep(3600)

            sched._running = True
            sched._task = asyncio.create_task(hang_forever())

            # stop() should cancel after timeout
            await sched.stop()

        assert sched._task is None
