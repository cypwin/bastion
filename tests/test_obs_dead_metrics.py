"""Tier-0 dead-metric wiring tests (spec Section 5.4 / 5.1 row 357).

Verifies that three previously-defined-but-uncalled Prometheus helpers are now
driven from their real call sites:

  - ``update_gpu_temperature(celsius)`` — emitted from ``health.check_gpu_safe``
    where the ``GPUStatus`` (with ``temperature_c``) is already in hand on the
    fast cadence; skipped (never set to 0) when the backend returns ``None``.
  - ``record_cooldown_wait()`` — at the scheduler cooldown-enforcement sleep
    site in ``_handle_swap_dispatch``.
  - ``record_model_swap_duration(model, dur)`` — captured around
    ``_dispatch_for_model`` in BOTH the semaphore (``VRAMManager`` present) and
    the no-semaphore (``vram_manager is None``) swap branches.

These assert the call sites exist and fire. When ``prometheus_client`` is
absent the helpers are no-ops, so the tests degrade to a smoke check that the
wiring runs without raising.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    ModelInfo,
    QueuedRequest,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMTracker

# ── GPU temperature gauge (update_gpu_temperature) ─────────────────────────


class TestGpuTemperatureWiring:
    @pytest.mark.asyncio
    async def test_check_gpu_safe_emits_temperature_when_present(self):
        """check_gpu_safe should publish the die temp it already fetched."""
        from bastion import health

        cfg = GPUConfig(max_temperature_c=82)
        status = GPUStatus(temperature_c=72, power_draw_watts=150.0)

        with patch.object(
            health, "query_gpu_status", AsyncMock(return_value=status)
        ), patch.object(health, "update_gpu_temperature") as spy:
            await health.check_gpu_safe(cfg)

        spy.assert_called_once_with(72)

    @pytest.mark.asyncio
    async def test_check_gpu_safe_skips_temperature_when_none(self):
        """None temp (StubBackend / non-NVIDIA) → skip, never emit a 0."""
        from bastion import health

        cfg = GPUConfig(max_temperature_c=82)
        status = GPUStatus(temperature_c=None)

        with patch.object(
            health, "query_gpu_status", AsyncMock(return_value=status)
        ), patch.object(health, "update_gpu_temperature") as spy:
            await health.check_gpu_safe(cfg)

        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_temperature_emitted_even_when_gpu_unsafe(self):
        """Emission happens on value-in-hand, regardless of safety verdict."""
        from bastion import health

        cfg = GPUConfig(max_temperature_c=70)
        status = GPUStatus(temperature_c=95)  # over the ceiling → unsafe

        with patch.object(
            health, "query_gpu_status", AsyncMock(return_value=status)
        ), patch.object(health, "update_gpu_temperature") as spy:
            safe, _reason = await health.check_gpu_safe(cfg)

        assert safe is False
        spy.assert_called_once_with(95)

    def test_gpu_temperature_gauge_records_value(self):
        """Integration: with prometheus present, the gauge reflects the value."""
        from bastion.metrics import PROMETHEUS_AVAILABLE, update_gpu_temperature

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op gauge")

        from bastion.metrics import GPU_TEMPERATURE

        update_gpu_temperature(72.0)
        assert GPU_TEMPERATURE._value.get() == 72.0


# ── Scheduler cooldown + swap-duration wiring ──────────────────────────────


def _sched_config(*, cooldown: float) -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(
            cooldown_seconds=cooldown,
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=32,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
        },
    )


def _make_request(model: str) -> QueuedRequest:
    return QueuedRequest(model=model, endpoint="/api/generate")


class TestCooldownWaitWiring:
    @pytest.mark.asyncio
    async def test_cooldown_sleep_site_records_cooldown_wait(self):
        """The cooldown-wait branch of _handle_swap_dispatch must count once."""
        cfg = _sched_config(cooldown=5.0)  # large so remaining > 0
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        sched = Scheduler(cfg, queue, tracker, AsyncMock())

        # Current model has no queued work, so the swap path must wait for
        # cooldown rather than drain the current model.
        sched._current_model = "mistral-nemo:12b"
        # Real time baseline so cooldown is genuinely in effect.
        import time as _time

        sched._last_swap_time = _time.time()

        candidate = _make_request("qwen3:14b")

        with patch("bastion.scheduler.record_cooldown_wait") as spy, patch(
            "asyncio.sleep", AsyncMock()
        ):
            result = await sched._handle_swap_dispatch(candidate)

        assert result is False
        spy.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_no_cooldown_record_when_cooldown_elapsed(self):
        """When cooldown has elapsed there is no wait → no counter increment."""
        cfg = _sched_config(cooldown=0.0)  # no cooldown
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        sched = Scheduler(cfg, queue, tracker, AsyncMock())
        sched._current_model = None

        import time as _time

        sched._last_swap_time = _time.time() - 100.0  # long ago

        candidate = _make_request("qwen3:14b")

        # Make the swap path bail right after the cooldown check by denying load
        # and eviction, so we only exercise up to (and past) the cooldown branch.
        with patch("bastion.scheduler.record_cooldown_wait") as spy, patch.object(
            tracker, "log_vram_snapshot", AsyncMock()
        ), patch.object(
            tracker, "can_load_model", AsyncMock(return_value=(False, "full"))
        ), patch.object(
            sched, "_evict_for_model", AsyncMock(return_value=False)
        ):
            await sched._handle_swap_dispatch(candidate)

        spy.assert_not_called()


class TestSwapDurationWiring:
    @pytest.mark.asyncio
    async def test_swap_duration_recorded_no_semaphore_branch(self):
        """vram_manager is None → record_model_swap_duration on the else branch."""
        cfg = _sched_config(cooldown=0.0)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        # No vram_manager → the no-semaphore branch.
        sched = Scheduler(cfg, queue, tracker, AsyncMock(), vram_manager=None)
        sched._current_model = None

        import time as _time

        sched._last_swap_time = _time.time() - 100.0  # cooldown elapsed

        candidate = _make_request("qwen3:14b")

        with patch("bastion.scheduler.record_model_swap_duration") as spy, patch(
            "bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))
        ), patch("bastion.scheduler.record_model_swap"), patch.object(
            tracker, "log_vram_snapshot", AsyncMock()
        ), patch.object(
            tracker, "can_load_model", AsyncMock(return_value=(True, "OK"))
        ), patch.object(
            tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)
        ), patch.object(
            tracker, "get_loaded_models", AsyncMock(return_value=[])
        ), patch.object(
            sched, "_dispatch_for_model", AsyncMock(return_value=True)
        ) as dispatch:
            result = await sched._handle_swap_dispatch(candidate)

        assert result is True
        dispatch.assert_awaited()
        spy.assert_called_once()
        # First positional arg is the model name; second is a float duration.
        args, _kwargs = spy.call_args
        assert args[0] == "qwen3:14b"
        assert isinstance(args[1], float)
        assert args[1] >= 0.0

    @pytest.mark.asyncio
    async def test_swap_duration_recorded_semaphore_branch(self):
        """vram_manager present → record_model_swap_duration on the semaphore branch."""
        cfg = _sched_config(cooldown=0.0)
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        # Minimal fake VRAMManager exercising the semaphore branch.
        vram_manager = MagicMock()
        vram_manager.reconcile = AsyncMock()
        vram_manager.reserve = AsyncMock(return_value=MagicMock())
        vram_manager.commit = AsyncMock()
        vram_manager.release = AsyncMock()
        vram_manager.wait_for_vram_convergence = AsyncMock()
        vram_manager._load_semaphore = asyncio.Semaphore(1)

        sched = Scheduler(
            cfg, queue, tracker, AsyncMock(), vram_manager=vram_manager
        )
        sched._current_model = None

        import time as _time

        sched._last_swap_time = _time.time() - 100.0

        candidate = _make_request("qwen3:14b")

        with patch("bastion.scheduler.record_model_swap_duration") as spy, patch(
            "bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))
        ), patch("bastion.scheduler.record_model_swap"), patch.object(
            tracker, "log_vram_snapshot", AsyncMock()
        ), patch.object(
            tracker, "get_loaded_vram_gb", AsyncMock(return_value=0.0)
        ), patch.object(
            tracker, "get_loaded_models", AsyncMock(return_value=[])
        ), patch.object(
            sched, "_dispatch_for_model", AsyncMock(return_value=True)
        ) as dispatch:
            result = await sched._handle_swap_dispatch(candidate)

        assert result is True
        dispatch.assert_awaited()
        spy.assert_called_once()
        args, _kwargs = spy.call_args
        assert args[0] == "qwen3:14b"
        assert isinstance(args[1], float)
        assert args[1] >= 0.0
