"""Tests for Scheduler — dispatch, cooldown, model swaps, GPU gating."""

from __future__ import annotations

import asyncio
import time
from collections import deque
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
    SwapBrakeConfig,
)

# Brake-neutral config for tests that exercise dispatch/swap mechanics rather
# than the brake itself (the brake's behavior is covered by test_swapbrake.py
# and the dedicated S2 wiring tests). min-spacing 0 + a huge bucket make the
# brake non-throttling, so the startup "just-swapped" seed never blocks a swap.
_NEUTRAL_BRAKE = SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=1_000_000.0)
from bastion import audit
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.swapbrake import BrakeDecision, BrakeState, SwapBrake
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
            swap_brake=_NEUTRAL_BRAKE,
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
             patch.object(tracker, "get_loaded_vram_gb", new_callable=AsyncMock, return_value=0.0), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=24.0)):
            # F-2 — supply a plausible free-VRAM reading so the cold-swap reserve
            # (is_swap=True) actually SUCCEEDS; otherwise it would fail closed before
            # the GPU gate, leaving the reservation never made and this abort-release
            # path untested (vacuous pass).
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
                swap_brake=_NEUTRAL_BRAKE,
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
        now = time.monotonic()
        # Add 3 swaps (below warn threshold of 4)
        for i in range(3):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.cooldown_seconds

    def test_warn_cooldown_at_warn_threshold(self, cooldown_config: BrokerConfig) -> None:
        """At warn threshold, cooldown should escalate to warn level."""
        sched = self._make_scheduler(cooldown_config)
        now = time.monotonic()
        # Add exactly 4 swaps (= warn threshold)
        for i in range(4):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.swap_rate_warn_cooldown_seconds

    def test_critical_cooldown_at_critical_threshold(self, cooldown_config: BrokerConfig) -> None:
        """At critical threshold, cooldown should escalate to critical level."""
        sched = self._make_scheduler(cooldown_config)
        now = time.monotonic()
        # Add 6 swaps (= critical threshold)
        for i in range(6):
            sched._swap_timestamps.append(now - i)
        cooldown = sched._get_swap_cooldown()
        assert cooldown == cooldown_config.scheduler.swap_rate_critical_cooldown_seconds

    def test_old_timestamps_are_pruned(self, cooldown_config: BrokerConfig) -> None:
        """Timestamps older than the window should be pruned."""
        sched = self._make_scheduler(cooldown_config)
        now = time.monotonic()
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
        now = time.monotonic()
        for i in range(4):
            sched._swap_timestamps.append(now - i)
        sched._get_swap_cooldown()
        assert sched._swap_rate_level == "warn"

    def test_critical_to_normal_transition(self, cooldown_config: BrokerConfig) -> None:
        """After timestamps expire, level should drop back to normal."""
        sched = self._make_scheduler(cooldown_config)
        now = time.monotonic()
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

    def test_cooldown_window_uses_monotonic_clock(self, cooldown_config: BrokerConfig) -> None:
        """F1: the swap-rate window must prune on time.monotonic(), not wall clock.

        A wall-clock backward NTP step / suspend-resume would otherwise read the
        trailing window as ~0 swaps and silently disarm the throttle.
        """
        sched = self._make_scheduler(cooldown_config)
        window = cooldown_config.scheduler.swap_rate_window_seconds
        with patch("bastion.scheduler.time.monotonic", return_value=1000.0):
            old = 1000.0 - window - 5.0  # outside window -> pruned (oldest, appended first)
            sched._swap_timestamps.append(old)
            sched._swap_timestamps.append(999.0)  # 1s ago -> within window -> kept
            sched._get_swap_cooldown()
            assert old not in sched._swap_timestamps
            assert 999.0 in sched._swap_timestamps

    def test_swap_rate_gauge_published(self, cooldown_config: BrokerConfig) -> None:
        """F1: _get_swap_cooldown publishes the live per-minute swap-rate gauge."""
        sched = self._make_scheduler(cooldown_config)
        now = time.monotonic()
        for i in range(3):
            sched._swap_timestamps.append(now - i)
        with patch("bastion.scheduler.update_swap_rate_per_min") as gauge:
            sched._get_swap_cooldown()
            gauge.assert_called_once()
            assert gauge.call_args[0][0] == 3.0


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

    @pytest.fixture(autouse=True)
    def _hw_free(self):
        """F-2 — the scheduler now reserves with is_swap=True (cold-swap path), so a
        missing nvidia-smi reading fails CLOSED. These reservation-lifecycle tests
        assume a healthy hardware gate, so pin a plausible free-VRAM reading; the
        deliberate fail-closed/degrade path is covered by TestColdSwapFailClosed."""
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=24.0)):
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


class TestSwapBrakeWiring:
    """S2 — the SwapBrake is actually wired into the swap path (not inert)."""

    def _make(self, config, vram_manager=None, dispatch=None) -> Scheduler:
        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)

        async def _default(request, needs_swap=True) -> None:
            pass

        return Scheduler(config, queue, tracker, dispatch or _default, vram_manager=vram_manager)

    @pytest.mark.asyncio
    async def test_drain_holds_brake_state(self, sched_config: BrokerConfig) -> None:
        sched = self._make(sched_config)
        await sched.drain()
        assert sched.swap_brake.snapshot()["drain_active"] is True
        await sched.resume()
        assert sched.swap_brake.snapshot()["drain_active"] is False

    @pytest.mark.asyncio
    async def test_sync_seeds_brake_just_swapped(self, sched_config: BrokerConfig) -> None:
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=8.0)
        sched = self._make(cfg)
        with patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=[])):
            await sched._sync_current_model()
        # the startup seed denies a free first swap (closes the _last_swap_time=0.0 hole)
        assert sched.swap_brake.peek("qwen3:14b").action == "stall"

    @pytest.mark.asyncio
    async def test_braked_pregate_skips_eviction(self, sched_config: BrokerConfig) -> None:
        sched = self._make(sched_config)
        # force the cheap pre-gate to stall -> a doomed swap must NOT evict
        sched._brake.peek = lambda model: BrakeDecision("stall", "test-hold", 0.0)
        sched._evict_for_model = AsyncMock(return_value=False)
        result = await sched._handle_swap_dispatch(make_request(model="qwen3:14b"))
        assert result is False
        sched._evict_for_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_vram_manager_swap_debits_brake(self, sched_config: BrokerConfig) -> None:
        # R2-1: the no-VRAMManager branch must still acquire the serializer and
        # debit the brake — else velocity is unbounded on uncalibrated hosts.
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=5.0)
        dispatched: list = []

        async def dispatch(request, needs_swap=True) -> None:
            dispatched.append(request)

        sched = self._make(cfg, dispatch=dispatch)  # vram_manager=None
        req = make_request(model="qwen3:14b")
        sched.queue.enqueue(req)
        with (
            patch.object(sched.vram, "can_load_model", AsyncMock(return_value=(True, "ok"))),
            patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))),
            patch.object(sched.vram, "get_loaded_vram_gb", AsyncMock(return_value=0.0)),
            patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=[])),
        ):
            before = sched.swap_brake.snapshot()["tokens"]
            result = await sched._handle_swap_dispatch(req)
        assert result is True
        assert len(dispatched) == 1
        assert sched.swap_brake.snapshot()["tokens"] == before - 1.0


# ---------------------------------------------------------------------------
# S3 — pinned-exclusion + behavioral infeasible latch (F4)
# ---------------------------------------------------------------------------


def _make_sched(config, **kw) -> Scheduler:
    queue = AffinityQueue(config.scheduler)
    tracker = VRAMTracker(config)

    async def _d(request, needs_swap=True) -> None:
        pass

    return Scheduler(config, queue, tracker, _d, **kw)


class TestPinAwareInfeasibleLatch:
    """F4 — externally pinned models are never evicted; the demanding CANDIDATE
    (not the pinned victim) is latched INFEASIBLE; latch clears only on a real
    residency delta; clear_on_residency_delta runs every tick."""

    @pytest.mark.asyncio
    async def test_pinned_model_never_evicted(self, sched_config: BrokerConfig) -> None:
        unloaded: list[str] = []
        sched = _make_sched(sched_config)
        sched.vram._pinned = {"qwen3:14b"}  # caller keep_alive=-1 pin
        loaded = [
            LoadedModel(name="qwen3:14b", vram_gb=9.3),
            LoadedModel(name="mistral-nemo:12b", vram_gb=8.1),
        ]

        async def mock_can_load(model):
            return (True, "OK")

        async def mock_unload(model):
            unloaded.append(model)
            return True

        with patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=loaded)), \
             patch.object(sched.vram, "can_load_model", side_effect=mock_can_load), \
             patch.object(sched.vram, "unload_model", side_effect=mock_unload):
            result = await sched._evict_for_model(make_request(model="llama3.1:8b"))

        assert result is True
        assert "qwen3:14b" not in unloaded         # pinned victim protected
        assert "mistral-nemo:12b" in unloaded       # non-pinned freely evicted

    @pytest.mark.asyncio
    async def test_behavioral_latch_fires_on_candidate_not_victim(
        self, sched_config: BrokerConfig,
    ) -> None:
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(
            min_spacing_seconds=0.0, bucket_capacity=1000.0,
            infeasible_evict_reload_threshold=3, infeasible_window_seconds=120.0,
        )
        sched = _make_sched(cfg)
        sched.vram._pinned = {"qwen3:14b"}
        # Budget (26 GB) NOT overflowed -> only the behavioral signal can trip.
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]
        now = time.monotonic()
        sched._evict_reload_history["qwen3:14b"] = deque([now, now, now])  # 3 oscillations

        candidate = make_request(model="mistral-nemo:12b")
        assert sched._maybe_latch_infeasible(candidate, loaded) is True
        assert sched.swap_brake.is_latched("mistral-nemo:12b") is True   # CANDIDATE latched
        assert sched.swap_brake.is_latched("qwen3:14b") is False         # victim NOT latched

    @pytest.mark.asyncio
    async def test_proactive_overflow_latches_candidate(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        sched.vram._pinned = {"qwen3:14b"}
        # Pinned 22 GB + candidate 8.1 GB = 30.1 > 26 GB budget -> proactive latch.
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=22.0)]
        candidate = make_request(model="mistral-nemo:12b")
        assert sched._maybe_latch_infeasible(candidate, loaded) is True
        assert sched.swap_brake.is_latched("mistral-nemo:12b") is True
        assert sched.swap_brake.is_latched("qwen3:14b") is False

    @pytest.mark.asyncio
    async def test_behavioral_latch_fires_without_pin_metadata(
        self, sched_config: BrokerConfig,
    ) -> None:
        """F-3: on Ollama builds without parseable expires_at, vram._pinned is
        empty — yet the version-independent evict↔reload oscillation signature
        must STILL latch (degrade to the behavioral signature, never to no
        protection). Without the fallback, the :628 _pinned_resident early-return
        AND _pinned_oscillation_count's pinned-only loop strand the history."""
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(
            min_spacing_seconds=0.0, bucket_capacity=1000.0,
            infeasible_evict_reload_threshold=3, infeasible_window_seconds=120.0,
        )
        sched = _make_sched(cfg)
        sched.vram._pinned = set()  # NO parseable expires_at → pin set invisible
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]
        now = time.monotonic()
        # A model BASTION evicted that keeps reappearing — the pin-fight fingerprint.
        sched._evict_reload_history["qwen3:14b"] = deque([now, now, now])

        candidate = make_request(model="mistral-nemo:12b")
        assert sched._maybe_latch_infeasible(candidate, loaded) is True
        assert sched.swap_brake.is_latched("mistral-nemo:12b") is True

    @pytest.mark.asyncio
    async def test_no_latch_without_pins_below_threshold(
        self, sched_config: BrokerConfig,
    ) -> None:
        """F-3 guard: empty pin set + oscillation below threshold ⇒ NO latch — the
        fallback must not false-positive on benign one-off churn."""
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(
            min_spacing_seconds=0.0, bucket_capacity=1000.0,
            infeasible_evict_reload_threshold=3, infeasible_window_seconds=120.0,
        )
        sched = _make_sched(cfg)
        sched.vram._pinned = set()
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]
        now = time.monotonic()
        sched._evict_reload_history["qwen3:14b"] = deque([now, now])  # only 2 < 3
        candidate = make_request(model="mistral-nemo:12b")
        assert sched._maybe_latch_infeasible(candidate, loaded) is False

    @pytest.mark.asyncio
    async def test_latch_clears_on_residency_delta_only(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        sched.vram._pinned = {"qwen3:14b"}
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=22.0)]
        sched.swap_brake.clear_on_residency_delta({"qwen3:14b"})  # establish baseline
        sched._maybe_latch_infeasible(make_request(model="mistral-nemo:12b"), loaded)
        assert sched.swap_brake.is_latched("mistral-nemo:12b") is True
        # A pure residency delta clears it (TTL aside).
        sched.swap_brake.clear_on_residency_delta({"qwen3:14b", "llama3.1:8b"})
        assert sched.swap_brake.is_latched("mistral-nemo:12b") is False

    @pytest.mark.asyncio
    async def test_clear_on_residency_delta_called_each_tick(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        sched.queue.enqueue(make_request(model="qwen3:14b"))
        loaded = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]
        with patch.object(
                 sched.vram.residency_cache, "get_resident_models",
                 AsyncMock(return_value={"qwen3:14b"}),
             ), \
             patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=loaded)), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(sched._brake, "clear_on_residency_delta") as mock_clear:
            await sched._process_tick()
            mock_clear.assert_called()

    @pytest.mark.asyncio
    async def test_force_unload_refuses_next_loads(self, sched_config: BrokerConfig) -> None:
        sched = _make_sched(sched_config)
        with patch.object(sched.vram, "unload_model", AsyncMock(return_value=True)):
            status, _ = await sched.unload_model_admin("qwen3:14b")
        assert status == "unloaded"
        assert sched.swap_brake.is_latched("qwen3:14b") is True

    @pytest.mark.asyncio
    async def test_unload_feeds_oscillation_watch(self, sched_config: BrokerConfig) -> None:
        """A BASTION-driven unload that REAPPEARS resident next tick is recorded."""
        sched = _make_sched(sched_config)
        with patch.object(sched.vram, "unload_model", AsyncMock(return_value=True)):
            assert await sched._unload_model("qwen3:14b") is True
        assert "qwen3:14b" in sched._recently_unloaded
        # reappears resident -> one oscillation recorded, watch cleared
        sched._detect_evict_reload_oscillation({"qwen3:14b"})
        assert "qwen3:14b" not in sched._recently_unloaded
        assert len(sched._evict_reload_history["qwen3:14b"]) == 1


# ---------------------------------------------------------------------------
# S4 — queued-work tiering (aging snapshot, feasible probe, ceilings)
# ---------------------------------------------------------------------------


class TestQueuedWorkTiering:
    @pytest.mark.asyncio
    async def test_feasible_probe_skips_latched_and_pinned(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        sched.vram._pinned = {"qwen3:14b"}
        # pinned 22 GB + mistral 8.1 = 30.1 > 26 budget -> pinned-evicting -> infeasible
        loaded_big = [LoadedModel(name="qwen3:14b", vram_gb=22.0)]
        assert sched._is_feasible_candidate("mistral-nemo:12b", loaded_big) is False
        # latched model -> infeasible
        sched.swap_brake.note_infeasible("nomic-embed-text")
        assert sched._is_feasible_candidate("nomic-embed-text", loaded_big) is False
        # fits + not latched -> feasible
        loaded_small = [LoadedModel(name="qwen3:14b", vram_gb=2.0)]
        assert sched._is_feasible_candidate("mistral-nemo:12b", loaded_small) is True

    @pytest.mark.asyncio
    async def test_aging_snapshot_prefers_engage_ranked_model(
        self, sched_config: BrokerConfig,
    ) -> None:
        """At release, the ENGAGE-time ranking governs — a background model that
        merely aged during the brake does not get loaded over the foreground one."""
        sched = _make_sched(sched_config)
        sched.queue.enqueue(make_request(model="qwen3:14b"))
        sched.queue.enqueue(make_request(model="mistral-nemo:12b"))
        # Engage snapshot favors mistral even if live aging would favor qwen.
        sched._engage_ranking = {"mistral-nemo:12b": 1000.0, "qwen3:14b": 1.0}
        with patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=[])):
            chosen = await sched._select_swap_candidate(set())
        assert chosen is not None
        assert chosen.model == "mistral-nemo:12b"

    @pytest.mark.asyncio
    async def test_select_returns_base_when_not_engaged(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        req = make_request(model="qwen3:14b")
        sched.queue.enqueue(req)
        chosen = await sched._select_swap_candidate(set())  # _engage_ranking None
        assert chosen is not None
        assert chosen.model == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_backlog_ceiling_sheds(self, sched_config: BrokerConfig) -> None:
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=1000.0)
        sched = _make_sched(cfg)
        sched._engage_ranking = {"qwen3:14b": 1.0}  # engaged
        sched._brake_backlog_ceiling = 2
        sched._brake_backlog_count = 3  # already past the ceiling
        with patch.object(sched.vram, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(sched.vram, "log_vram_snapshot", AsyncMock()):
            result = await sched._handle_swap_dispatch(make_request(model="qwen3:14b"))
        assert result is False

    def test_swap_starvation_ceiling(self, sched_config: BrokerConfig) -> None:
        sched = _make_sched(sched_config)
        sched._swap_starvation_ceiling = 0.0
        sched._note_swap_starvation("qwen3:14b")
        assert sched._swap_starved("qwen3:14b") is True
        sched._clear_swap_starvation("qwen3:14b")
        assert sched._swap_starved("qwen3:14b") is False


# ---------------------------------------------------------------------------
# S5 — forward hardware-gate-blind to the brake (R1-2 wiring seam)
# ---------------------------------------------------------------------------


class TestHardwareGateBlindForward:
    @pytest.mark.asyncio
    async def test_blind_halves_refill_recovery_restores(
        self, sched_config: BrokerConfig,
    ) -> None:
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=100.0)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)

        async def dispatch(request, needs_swap=True) -> None:
            pass

        sched = Scheduler(cfg, queue, tracker, dispatch, vram_manager=mgr)
        full_refill = sched.swap_brake._refill_rate_per_sec()

        # F-2 — the scheduler now reserves with is_swap=True, so the blind signal must
        # arise THROUGH the real cold-swap reserve, not by hand-setting the flag (a
        # plausible reading would otherwise reset it). Drive it via the degraded verdict.
        free = {"gb": None}

        async def _free():
            return free["gb"]

        with patch("bastion.vram.get_vram_free_gb", _free), \
             patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "can_load_model", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)):
            # Blind: sensor returns no reading (miss). Pre-load the streak so the
            # cold-swap reserve DEGRADES to blind and ADMITS (hands the floor to the
            # velocity brake), which the scheduler forwards as a HALVED refill.
            free["gb"] = None
            mgr._hw_miss_streak = mgr._miss_degrade_after
            mgr._hardware_gate_blind = True
            queue.enqueue(make_request(model="qwen3:14b"))
            await sched._handle_swap_dispatch(queue.pick_next(None))
            assert sched.swap_brake.snapshot()["hardware_gate_blind"] is True
            assert sched.swap_brake._refill_rate_per_sec() == pytest.approx(full_refill * 0.5)

            # Recovery: a plausible reading resets the gate; full refill restored.
            free["gb"] = 24.0
            queue.enqueue(make_request(model="qwen3:14b"))
            await sched._handle_swap_dispatch(queue.pick_next(None))
            assert sched.swap_brake.snapshot()["hardware_gate_blind"] is False
            assert sched.swap_brake._refill_rate_per_sec() == pytest.approx(full_refill)


# ---------------------------------------------------------------------------
# F-1 [BLOCKER] — scheduler aborts an orphaned HALF_OPEN probe on dispatch failure
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _brake_primed_for_probe(model: str) -> tuple[SwapBrake, _FakeClock]:
    """A SwapBrake driven to OPEN and advanced past its cooloff with a healthy
    bucket, so the NEXT acquire() (the one inside _handle_swap_dispatch's load
    serializer) transitions OPEN→HALF_OPEN and grants the single probe."""
    clk = _FakeClock(1000.0)
    cfg = SwapBrakeConfig(
        min_spacing_seconds=0.0, bucket_capacity=3.0, refill_per_minute=0.0,
        cooloff_seconds=30.0, min_state_hold_seconds=5.0, release_rate_per_minute=3.0,
    )
    b = SwapBrake(cfg, clock=clk)
    for _ in range(3):
        b.acquire(model)
        b.record_load(model)
    for _ in range(60):
        b.acquire(model)
        clk.advance(0.1)
    assert b.snapshot()["state"] == BrakeState.OPEN
    clk.advance(31.0)
    clk.advance(60.0)  # past cooloff + window prune
    # refill is 0 in this drive; prime the bucket so the pre-gate peek() proceeds
    # to the serializer where the authoritative acquire() grants the probe.
    b._tokens = float(cfg.bucket_capacity)
    return b, clk


class TestAbortProbeWiring:
    """F-1 [BLOCKER]: when the in-serializer acquire() grants a HALF_OPEN probe but
    dispatch then fails (result False) or raises, _handle_swap_dispatch must call
    brake.abort_probe() so the brake re-OPENs instead of wedging forever at
    'half-open probe in flight' (a real post-storm liveness outage)."""

    def _build(self, sched_config: BrokerConfig, dispatch):
        cfg = sched_config.model_copy(deep=True)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)
        sched = Scheduler(cfg, queue, tracker, dispatch, vram_manager=mgr)
        sched._last_swap_time = 0.0
        b, _clk = _brake_primed_for_probe("qwen3:14b")
        sched._brake = b
        queue.enqueue(make_request(model="qwen3:14b"))
        return sched, queue, tracker, b

    @pytest.mark.asyncio
    async def test_failed_dispatch_aborts_orphan_probe(self, sched_config: BrokerConfig) -> None:
        async def failing_dispatch(request, needs_swap=True) -> None:
            # _dispatch_for_model catches this and returns False (else branch).
            raise RuntimeError("dispatch failed")

        sched, queue, tracker, b = self._build(sched_config, failing_dispatch)
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=30.0)):
            result = await sched._handle_swap_dispatch(queue.pick_next(None))

        assert result is False
        # acquire() granted the probe; dispatch failed so record_load never fired.
        # Without abort_probe the brake stays HALF_OPEN with the probe outstanding
        # (every future acquire → 'half-open probe in flight'); the wiring re-OPENs.
        assert b._probe_outstanding is False
        assert b.snapshot()["state"] == BrakeState.OPEN

    @pytest.mark.asyncio
    async def test_raised_dispatch_aborts_orphan_probe(self, sched_config: BrokerConfig) -> None:
        async def ok_dispatch(request, needs_swap=True) -> None:
            pass

        sched, queue, tracker, b = self._build(sched_config, ok_dispatch)

        async def boom(model, needs_swap=True):
            raise RuntimeError("boom after acquire")  # except branch

        sched._dispatch_for_model = boom  # type: ignore[assignment]
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=30.0)):
            with pytest.raises(RuntimeError, match="boom after acquire"):
                await sched._handle_swap_dispatch(queue.pick_next(None))

        # The except branch must abort the orphaned probe before re-raising.
        assert b._probe_outstanding is False
        assert b.snapshot()["state"] == BrakeState.OPEN

    @pytest.mark.asyncio
    async def test_failed_dispatch_advances_min_spacing_at_issue(
        self, sched_config: BrokerConfig,
    ) -> None:
        """NH-1 — a load that is ISSUED then FAILS (record_load never fires) must
        still advance the min-spacing floor at the issue point, so the next swap
        attempt is spaced rather than retried into an immediate inrush."""
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=8.0, bucket_capacity=100.0)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)

        async def failing_dispatch(request, needs_swap=True) -> None:
            raise RuntimeError("dispatch failed")  # _dispatch_for_model → returns False

        sched = Scheduler(cfg, queue, tracker, failing_dispatch, vram_manager=mgr)
        sched._last_swap_time = 0.0
        assert sched._brake._last_load_t is None  # fresh brake — nothing issued yet
        queue.enqueue(make_request(model="qwen3:14b"))
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=30.0)):
            result = await sched._handle_swap_dispatch(queue.pick_next(None))

        assert result is False  # dispatch failed, no record_load
        assert sched._brake._last_load_t is not None  # spacing advanced at issue


# ---------------------------------------------------------------------------
# F-4 — fail-LOUD brake observability is wired (gauges, engage counter, audit)
# ---------------------------------------------------------------------------


class _FakeBrake:
    """Minimal brake double exposing only snapshot() (all _update_brake_engage_snapshot reads)."""

    def __init__(self, snap: dict) -> None:
        self._snap = snap

    def snapshot(self) -> dict:
        return dict(self._snap)


def _brake_snap(state: str, **over) -> dict:
    base = {
        "state": state, "reason": state, "cooloff_remaining_s": 0.0,
        "windowed_rate_per_min": 5.0, "backoff_level": 1, "tokens": 0.0,
        "hardware_gate_blind": False, "drain_active": False, "latched": [],
        "force_release_active": False, "force_engage_active": False,
    }
    base.update(over)
    return base


class TestBrakeObservabilityWiring:
    """F-4 — the fail-LOUD brake observability is wired (was zero runtime callers):
    gauges pushed every tick, engage edge counted once, one audit event on engage
    and on release (with duration + swaps-during-brake), latch WARNING heartbeat."""

    def test_engage_release_edges_push_gauges_count_and_audit(
        self, sched_config: BrokerConfig,
    ) -> None:
        sched = _make_sched(sched_config)
        fake = _FakeBrake(_brake_snap("open"))
        sched._brake = fake
        with patch("bastion.scheduler.record_swap_brake_engaged") as rec, \
             patch("bastion.scheduler.update_swap_brake_state") as ust, \
             patch("bastion.scheduler.update_pinned_vram_gb") as upg, \
             patch("bastion.audit.emit") as aem:
            sched._update_brake_engage_snapshot()      # CLOSED→engaged edge
            sched._update_brake_engage_snapshot()      # still engaged (no new edge)
            sched._swaps_during_brake = 2              # pretend two probes succeeded
            fake._snap = _brake_snap("closed")
            sched._update_brake_engage_snapshot()      # engaged→released edge

        assert rec.call_count == 1                     # engage counted exactly once
        assert ust.call_count == 3                     # state gauge pushed each tick
        assert upg.call_count == 3                     # pinned gauge pushed each tick
        sb = [c for c in aem.call_args_list if c.args and c.args[0] == audit.EVENT_SWAP_BRAKE]
        assert [c.args[1]["transition"] for c in sb] == ["engaged", "released"]
        released = sb[1].args[1]
        assert released["swaps_during_brake"] == 2
        assert "duration_seconds" in released

    def test_latched_infeasible_logs_warning_heartbeat(
        self, sched_config: BrokerConfig, caplog,
    ) -> None:
        sched = _make_sched(sched_config)
        sched._brake = _FakeBrake(_brake_snap("open", latched=["big27b"]))
        with patch("bastion.scheduler.record_swap_brake_engaged"), \
             patch("bastion.scheduler.update_swap_brake_state"), \
             patch("bastion.scheduler.update_pinned_vram_gb"), \
             caplog.at_level("WARNING"):
            sched._update_brake_engage_snapshot()
        assert any("big27b" in r.message for r in caplog.records)

    def test_force_active_gauge_pushed_each_tick(
        self, sched_config: BrokerConfig,
    ) -> None:
        """F-5 — bastion_swap_brake_force_active is held high for the override
        window so an operator can see the backstop is disabled (1.0 active, 0.0 not)."""
        sched = _make_sched(sched_config)
        with patch("bastion.scheduler.update_swap_brake_force_active") as gfa, \
             patch("bastion.scheduler.record_swap_brake_engaged"), \
             patch("bastion.scheduler.update_swap_brake_state"), \
             patch("bastion.scheduler.update_pinned_vram_gb"):
            sched._brake = _FakeBrake(_brake_snap("closed", force_release_active=True))
            sched._update_brake_engage_snapshot()
            sched._brake = _FakeBrake(_brake_snap("closed", force_release_active=False))
            sched._update_brake_engage_snapshot()
        assert gfa.call_args_list[0].args[0] == 1.0
        assert gfa.call_args_list[1].args[0] == 0.0


# ---------------------------------------------------------------------------
# F-2 — the F5 cold-swap fail-CLOSED path is reachable from the live swap path
# ---------------------------------------------------------------------------


class TestColdSwapFailClosed:
    """F-2: the scheduler reserves with is_swap=True, so a transient nvidia-smi miss
    on the dangerous cold-swap path fails CLOSED (refuse the swap), and after K
    consecutive misses degrades to blind — handing the floor to the velocity brake
    (set_hw_degraded) instead of converting a sensor outage into a permanent swap
    outage. Before F-2 the scheduler reserved without is_swap, so this entire path
    was inert (steady-state fail-OPEN)."""

    @pytest.mark.asyncio
    async def test_transient_miss_fails_closed_then_degrades_after_k(
        self, sched_config: BrokerConfig,
    ) -> None:
        cfg = sched_config.model_copy(deep=True)
        cfg.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=100.0)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)
        dispatched: list = []

        async def dispatch(request, needs_swap=True) -> None:
            dispatched.append(request)

        sched = Scheduler(cfg, queue, tracker, dispatch, vram_manager=mgr)
        sched._last_swap_time = 0.0
        k = mgr._miss_degrade_after  # default 3

        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)), \
             patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)), \
             patch("bastion.vram.record_hardware_gate_blind") as blind_metric:
            # The first K-1 cold-swap attempts fail CLOSED: blind on the dangerous
            # path = STOP. No dispatch, gate not yet degraded. (The fail-closed reserve
            # returns before _dispatch_for_model dequeues, so the request stays queued.)
            for _ in range(k - 1):
                queue.enqueue(make_request(model="qwen3:14b"))
                result = await sched._handle_swap_dispatch(queue.pick_next(None))
                assert result is False
            assert dispatched == []
            assert mgr.hardware_gate_blind is False

            # The K-th consecutive miss DEGRADES: stop fail-closing, hand the floor
            # to the sensor-independent velocity brake (admit), set blind, fire the
            # metric, and forward the blind signal to the brake.
            queue.enqueue(make_request(model="qwen3:14b"))
            result = await sched._handle_swap_dispatch(queue.pick_next(None))
            assert result is True
            assert dispatched  # the K-th swap was admitted
            assert mgr.hardware_gate_blind is True
            assert blind_metric.called
            assert sched.swap_brake.snapshot()["hardware_gate_blind"] is True

    @pytest.mark.asyncio
    async def test_successful_swap_during_brake_increments_counter(
        self, sched_config: BrokerConfig,
    ) -> None:
        cfg = sched_config.model_copy(deep=True)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        mgr = VRAMManager(tracker, 32 * 1024 * 1024 * 1024, safety_margin_pct=10.0)

        async def ok_dispatch(request, needs_swap=True) -> None:
            pass

        sched = Scheduler(cfg, queue, tracker, ok_dispatch, vram_manager=mgr)
        sched._last_swap_time = 0.0
        b, _clk = _brake_primed_for_probe("qwen3:14b")
        sched._brake = b
        sched._engage_ranking = {}  # mark engaged so the during-brake counter is active
        queue.enqueue(make_request(model="qwen3:14b"))
        with patch.object(tracker, "get_loaded_models", AsyncMock(return_value=[])), \
             patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
             patch.object(tracker, "log_vram_snapshot", AsyncMock()), \
             patch.object(tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)), \
             patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=30.0)):
            result = await sched._handle_swap_dispatch(queue.pick_next(None))

        assert result is True
        assert sched._swaps_during_brake == 1
