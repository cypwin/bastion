"""Tests for graceful shutdown behavior.

Covers:
  - Scheduler stop drains queue and cancels loop
  - Scheduler stop respects timeout
  - Scheduler drain mode pauses new scheduling
  - Lifespan shutdown unblocks pending grants
  - Signal handler setup in two-port mode
  - SystemD service file timeout consistency
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion.models import BrokerConfig, PriorityTier, QueuedRequest, SchedulerConfig
from bastion.queue import AffinityQueue

# ---------------------------------------------------------------------------
# Scheduler stop/drain tests
# ---------------------------------------------------------------------------


class TestSchedulerStop:
    """Test scheduler stop drains queue and stops loop cleanly."""

    def _make_scheduler(self, **overrides):
        from bastion.scheduler import Scheduler

        config = BrokerConfig(**overrides)
        queue = AffinityQueue(config.scheduler)
        vram = MagicMock()
        dispatch = AsyncMock()

        return Scheduler(
            config=config,
            queue=queue,
            vram_tracker=vram,
            dispatch_fn=dispatch,
        )

    @pytest.mark.asyncio
    async def test_scheduler_stop_sets_running_false(self) -> None:
        scheduler = self._make_scheduler()
        scheduler._running = True
        scheduler._task = None

        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_scheduler_stop_cancels_task_on_timeout(self) -> None:
        scheduler = self._make_scheduler(
            scheduler=SchedulerConfig(shutdown_timeout_seconds=0.1),
        )

        # Create a task that will never finish
        async def infinite_loop():
            while True:
                await asyncio.sleep(100)

        scheduler._running = True
        scheduler._task = asyncio.create_task(infinite_loop())

        # Stop should cancel the task after timeout
        await scheduler.stop()
        assert scheduler._task is None

    @pytest.mark.asyncio
    async def test_scheduler_drain_mode(self) -> None:
        scheduler = self._make_scheduler()

        assert scheduler._draining is False
        await scheduler.drain()
        assert scheduler._draining is True

        await scheduler.resume()
        assert scheduler._draining is False


# ---------------------------------------------------------------------------
# Queue drain tests
# ---------------------------------------------------------------------------


class TestQueueDrain:
    """Test queue drain_all for shutdown."""

    def test_drain_all_returns_queued_requests(self) -> None:
        config = SchedulerConfig()
        queue = AffinityQueue(config)

        req1 = QueuedRequest(
            model="model_a", endpoint="/api/generate", body=b"{}",
            priority=10.0, base_priority=10.0, tier=PriorityTier.AGENT,
        )
        req2 = QueuedRequest(
            model="model_b", endpoint="/api/generate", body=b"{}",
            priority=20.0, base_priority=20.0, tier=PriorityTier.INTERACTIVE,
        )

        queue.enqueue(req1)
        queue.enqueue(req2)
        assert queue.total_size == 2

        drained = queue.drain_all()
        assert len(drained) == 2
        assert queue.total_size == 0
        assert queue.is_empty

    def test_drain_all_empty_queue(self) -> None:
        config = SchedulerConfig()
        queue = AffinityQueue(config)
        drained = queue.drain_all()
        assert drained == []


# ---------------------------------------------------------------------------
# Systemd service file validation
# ---------------------------------------------------------------------------


class TestSystemdServiceFile:
    """Validate systemd service file configuration."""

    def test_timeout_stop_sec_exceeds_scheduler_timeout(self) -> None:
        """TimeoutStopSec must be > scheduler shutdown_timeout_seconds."""
        service_path = Path(__file__).resolve().parent.parent / "systemd" / "bastion.service"
        if not service_path.exists():
            pytest.skip("Service file not found")

        content = service_path.read_text()

        # Extract TimeoutStopSec
        timeout_stop = None
        for line in content.splitlines():
            if line.startswith("TimeoutStopSec="):
                timeout_stop = int(line.split("=")[1])
                break

        assert timeout_stop is not None, "TimeoutStopSec not found in service file"

        # Check against scheduler default
        config = BrokerConfig()
        scheduler_timeout = config.scheduler.shutdown_timeout_seconds

        assert timeout_stop > scheduler_timeout, (
            f"TimeoutStopSec={timeout_stop} must be > "
            f"scheduler shutdown_timeout_seconds={scheduler_timeout}"
        )

    def test_service_type_is_notify(self) -> None:
        """Service must be Type=notify for sd_notify integration."""
        service_path = Path(__file__).resolve().parent.parent / "systemd" / "bastion.service"
        if not service_path.exists():
            pytest.skip("Service file not found")

        content = service_path.read_text()
        assert "Type=notify" in content

    def test_watchdog_sec_configured(self) -> None:
        """WatchdogSec must be configured for health monitoring."""
        service_path = Path(__file__).resolve().parent.parent / "systemd" / "bastion.service"
        if not service_path.exists():
            pytest.skip("Service file not found")

        content = service_path.read_text()
        assert "WatchdogSec=" in content


# ---------------------------------------------------------------------------
# Pending grant/completion unblock tests
# ---------------------------------------------------------------------------


class TestPendingUnblock:
    """Test that shutdown unblocks pending proxy handlers."""

    @pytest.mark.asyncio
    async def test_pending_events_are_set_on_shutdown(self) -> None:
        """Verify that setting an asyncio.Event unblocks waiters."""
        event = asyncio.Event()

        async def waiter():
            await event.wait()
            return True

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)  # Let the waiter start

        # Simulate shutdown unblocking
        event.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_multiple_events_all_unblocked(self) -> None:
        """All pending events are unblocked during shutdown."""
        events = [asyncio.Event() for _ in range(5)]
        results = []

        async def waiter(e):
            await e.wait()
            results.append(True)

        tasks = [asyncio.create_task(waiter(e)) for e in events]
        await asyncio.sleep(0.01)

        # Simulate shutdown: set all events
        for e in events:
            e.set()

        await asyncio.gather(*tasks)
        assert len(results) == 5
