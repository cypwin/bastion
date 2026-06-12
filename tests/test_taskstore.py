"""Tests for the hardened TaskStore (dual-store, TTL, backpressure)."""

from __future__ import annotations

import asyncio
import time

import pytest

from bastion.models import A2ATaskRecord, A2ATaskState
from bastion.taskstore import (
    _VALID_TRANSITIONS,
    BackpressureLevel,
    CompactedResult,
    TaskStore,
    TaskStoreFullError,
)


def _make_record(
    task_id: str = "test-001",
    state: A2ATaskState = A2ATaskState.SUBMITTED,
    skill_id: str = "infer",
) -> A2ATaskRecord:
    """Create a minimal A2ATaskRecord for testing."""
    return A2ATaskRecord(
        task_id=task_id,
        context_id="ctx-001",
        state=state,
        skill_id=skill_id,
        input_params={"model": "qwen3:30b", "prompt": "hello"},
    )


class TestTaskStoreBasics:
    """Basic create/get/has_task operations."""

    def test_create_and_get(self) -> None:
        store = TaskStore(maxsize=100)
        record = _make_record()
        store.create(record)
        result = store.get("test-001")
        assert result is not None
        assert isinstance(result, A2ATaskRecord)
        assert result.task_id == "test-001"

    def test_get_nonexistent_returns_none(self) -> None:
        store = TaskStore(maxsize=100)
        assert store.get("nonexistent") is None

    def test_has_task_active(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        assert store.has_task("test-001") is True
        assert store.has_task("nonexistent") is False

    def test_get_active(self) -> None:
        store = TaskStore(maxsize=100)
        record = _make_record()
        store.create(record)
        active = store.get_active("test-001")
        assert active is record

    def test_get_active_nonexistent(self) -> None:
        store = TaskStore(maxsize=100)
        assert store.get_active("nonexistent") is None

    def test_active_count(self) -> None:
        store = TaskStore(maxsize=100)
        assert store.active_count() == 0
        store.create(_make_record("t1"))
        assert store.active_count() == 1
        store.create(_make_record("t2"))
        assert store.active_count() == 2


class TestThreadAffinityContract:
    """TaskStore is asyncio-single-loop only — that contract must fail loud.

    `create` writes `_active`/`_active_timestamps` without a lock, which is
    safe only because all callers share one event-loop thread. A threaded
    caller (anyio thread pool, executor offload) would race silently; the
    affinity guard turns that into an immediate RuntimeError.
    """

    def test_create_from_foreign_thread_raises(self) -> None:
        import threading

        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))  # binds owner thread

        result: list[BaseException | str] = []

        def create_from_thread() -> None:
            try:
                store.create(_make_record("t2"))
                result.append("no error")
            except RuntimeError as e:
                result.append(e)

        t = threading.Thread(target=create_from_thread)
        t.start()
        t.join()

        assert isinstance(result[0], RuntimeError), (
            "create() from a foreign thread must raise RuntimeError, "
            f"got: {result[0]!r}"
        )
        assert "thread" in str(result[0]).lower()
        assert not store.has_task("t2")

    def test_create_from_owner_thread_repeatedly_ok(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))
        store.create(_make_record("t2"))
        assert store.active_count() == 2


class TestStateTransitions:
    """State machine enforcement."""

    def test_valid_transition_submitted_to_working(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        result = store.update_state("test-001", A2ATaskState.WORKING)
        assert result.state == A2ATaskState.WORKING

    def test_valid_transition_working_to_completed(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        result = store.update_state("test-001", A2ATaskState.COMPLETED)
        assert result.state == A2ATaskState.COMPLETED

    def test_valid_transition_submitted_to_canceled(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        result = store.update_state("test-001", A2ATaskState.CANCELED)
        assert result.state == A2ATaskState.CANCELED

    def test_valid_transition_working_to_failed(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        result = store.update_state("test-001", A2ATaskState.FAILED)
        assert result.state == A2ATaskState.FAILED

    def test_invalid_transition_raises_valueerror(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        with pytest.raises(ValueError, match="Invalid state transition"):
            store.update_state("test-001", A2ATaskState.COMPLETED)

    def test_invalid_transition_from_terminal_raises(self) -> None:
        store = TaskStore(maxsize=100)
        record = _make_record()
        store.create(record)
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        # Task is now in completed store, not active
        with pytest.raises(KeyError, match="not in active store"):
            store.update_state("test-001", A2ATaskState.WORKING)

    def test_update_nonexistent_task_raises_keyerror(self) -> None:
        store = TaskStore(maxsize=100)
        with pytest.raises(KeyError, match="not in active store"):
            store.update_state("nonexistent", A2ATaskState.WORKING)

    def test_all_valid_transitions_defined(self) -> None:
        """All A2ATaskState values have transition entries."""
        for state in A2ATaskState:
            assert state in _VALID_TRANSITIONS


class TestDualStore:
    """Active → completed → tombstone lifecycle."""

    def test_terminal_state_moves_to_completed(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        # No longer in active store
        assert store.get_active("test-001") is None
        assert "test-001" not in store._active
        # In completed store
        assert "test-001" in store._completed
        result = store.get("test-001")
        assert isinstance(result, CompactedResult)
        assert result.status == "completed"

    def test_compacted_result_from_record(self) -> None:
        record = _make_record()
        record.output_artifacts = [
            {"parts": [{"kind": "text", "text": "Hello world response"}]}
        ]
        record.state = A2ATaskState.COMPLETED
        compacted = CompactedResult.from_record(record)
        assert compacted.task_id == "test-001"
        assert compacted.status == "completed"
        assert compacted.result_summary == "Hello world response"

    def test_compacted_result_truncates_long_summary(self) -> None:
        record = _make_record()
        record.output_artifacts = [
            {"parts": [{"kind": "text", "text": "x" * 1000}]}
        ]
        record.state = A2ATaskState.COMPLETED
        compacted = CompactedResult.from_record(record)
        assert len(compacted.result_summary) == 500

    def test_has_task_includes_completed(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        assert store.has_task("test-001") is True

    def test_completed_store_capacity_eviction(self) -> None:
        store = TaskStore(maxsize=100, completed_maxsize=3)
        # Create and complete 5 tasks
        for i in range(5):
            tid = f"t-{i}"
            store.create(_make_record(tid))
            store.update_state(tid, A2ATaskState.WORKING)
            store.update_state(tid, A2ATaskState.COMPLETED)
        # Only 3 most recent should remain in completed
        assert len(store._completed) == 3
        # Oldest should be in tombstones
        assert "t-0" in store._tombstones
        assert "t-1" in store._tombstones

    def test_tombstone_capacity_limit(self) -> None:
        store = TaskStore(maxsize=100, completed_maxsize=1, tombstone_maxsize=2)
        for i in range(5):
            tid = f"t-{i}"
            store.create(_make_record(tid))
            store.update_state(tid, A2ATaskState.WORKING)
            store.update_state(tid, A2ATaskState.COMPLETED)
        # Only 2 tombstones should be kept
        assert len(store._tombstones) <= 2


class TestTTLEviction:
    """TTL enforcement via lazy and periodic sweep."""

    def test_lazy_eviction_on_get(self) -> None:
        store = TaskStore(maxsize=100, task_ttl_seconds=10.0)
        store.create(_make_record())
        # Fake the timestamp to be old
        store._active_timestamps["test-001"] = time.monotonic() - 20.0
        result = store.get("test-001")
        assert result is None
        assert "test-001" not in store._active
        assert "test-001" in store._tombstones

    def test_completed_ttl_eviction(self) -> None:
        store = TaskStore(maxsize=100, completed_ttl_seconds=10.0)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        # Fake completion time to be old
        old_result = store._completed["test-001"]
        store._completed["test-001"] = CompactedResult(
            task_id=old_result.task_id,
            status=old_result.status,
            result_summary=old_result.result_summary,
            error=old_result.error,
            completed_at=time.monotonic() - 20.0,
            output_artifacts=old_result.output_artifacts,
        )
        result = store.get("test-001")
        assert result is None
        assert "test-001" in store._tombstones

    def test_sweep_removes_expired_active(self) -> None:
        store = TaskStore(maxsize=100, task_ttl_seconds=10.0)
        store.create(_make_record("t1"))
        store.create(_make_record("t2"))
        # Make t1 expired
        store._active_timestamps["t1"] = time.monotonic() - 20.0
        store._sweep()
        assert "t1" not in store._active
        assert "t2" in store._active

    def test_sweep_removes_expired_completed(self) -> None:
        store = TaskStore(maxsize=100, completed_ttl_seconds=10.0)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        # Fake completion time
        old_result = store._completed["test-001"]
        store._completed["test-001"] = CompactedResult(
            task_id=old_result.task_id,
            status=old_result.status,
            result_summary=old_result.result_summary,
            error=old_result.error,
            completed_at=time.monotonic() - 20.0,
            output_artifacts=old_result.output_artifacts,
        )
        store._sweep()
        assert "test-001" not in store._completed
        assert "test-001" in store._tombstones


class TestBackpressure:
    """Three-stage backpressure with hysteresis."""

    def test_normal_level_initially(self) -> None:
        store = TaskStore(maxsize=100)
        assert store._pressure_level == BackpressureLevel.NORMAL

    def test_pressure_at_80_percent(self) -> None:
        store = TaskStore(maxsize=10)
        for i in range(8):
            store.create(_make_record(f"t-{i}"))
        assert store._pressure_level == BackpressureLevel.PRESSURE

    def test_overloaded_at_100_percent(self) -> None:
        store = TaskStore(maxsize=10)
        for i in range(10):
            store.create(_make_record(f"t-{i}"))
        assert store._pressure_level == BackpressureLevel.OVERLOADED

    def test_overloaded_rejects_new_tasks(self) -> None:
        store = TaskStore(maxsize=10)
        for i in range(10):
            store.create(_make_record(f"t-{i}"))
        with pytest.raises(TaskStoreFullError):
            store.create(_make_record("overflow"))

    def test_taskstore_full_error_has_retry_after(self) -> None:
        store = TaskStore(maxsize=1)
        store.create(_make_record("t-0"))
        try:
            store.create(_make_record("t-1"))
            raise AssertionError("Should have raised")
        except TaskStoreFullError as e:
            assert e.retry_after == 60

    def test_hysteresis_pressure_to_normal_at_70(self) -> None:
        store = TaskStore(maxsize=10)
        # Fill to 80% to enter PRESSURE
        for i in range(8):
            store.create(_make_record(f"t-{i}"))
        assert store._pressure_level == BackpressureLevel.PRESSURE
        # Remove tasks to get to 69% (7 tasks -> 70%)
        # Need to remove 2 to get to 60%
        del store._active["t-0"]
        store._active_timestamps.pop("t-0", None)
        del store._active["t-1"]
        store._active_timestamps.pop("t-1", None)
        del store._active["t-2"]
        store._active_timestamps.pop("t-2", None)
        # Now at 5/10 = 50%, should be NORMAL
        store._update_pressure_level()
        assert store._pressure_level == BackpressureLevel.NORMAL

    def test_hysteresis_stays_pressure_at_75(self) -> None:
        store = TaskStore(maxsize=100)
        # Manually set to PRESSURE
        store._pressure_level = BackpressureLevel.PRESSURE
        # Add 75 tasks (75%)
        for i in range(75):
            store._active[f"t-{i}"] = _make_record(f"t-{i}")
            store._active_timestamps[f"t-{i}"] = time.monotonic()
        store._update_pressure_level()
        # Still PRESSURE (above 70% threshold for leaving)
        assert store._pressure_level == BackpressureLevel.PRESSURE

    def test_pressure_reduces_completed_ttl(self) -> None:
        store = TaskStore(maxsize=10, completed_ttl_seconds=3600.0)
        # Enter pressure
        for i in range(8):
            store.create(_make_record(f"t-{i}"))
        assert store._effective_completed_ttl == 300.0


class TestSubscribers:
    """Subscriber management and fan-out."""

    def test_subscribe_creates_queue(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        queue = store.subscribe("test-001")
        assert isinstance(queue, asyncio.Queue)
        assert queue.maxsize == 100
        assert "test-001" in store._subscribers
        assert queue in store._subscribers["test-001"]

    def test_unsubscribe_removes_queue(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        queue = store.subscribe("test-001")
        store.unsubscribe("test-001", queue)
        assert "test-001" not in store._subscribers

    def test_unsubscribe_nonexistent_is_safe(self) -> None:
        store = TaskStore(maxsize=100)
        queue = asyncio.Queue()
        store.unsubscribe("nonexistent", queue)  # Should not raise

    @pytest.mark.asyncio
    async def test_notify_sends_to_all_subscribers(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        q1 = store.subscribe("test-001")
        q2 = store.subscribe("test-001")
        event = {"statusUpdate": {"state": "working"}}
        await store.notify_subscribers("test-001", event)
        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    @pytest.mark.asyncio
    async def test_notify_drop_oldest_on_full(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        queue = store.subscribe("test-001")
        # Fill the queue
        for i in range(100):
            queue.put_nowait({"i": i})
        # Now notify — should drop oldest and insert new
        new_event = {"statusUpdate": {"state": "completed"}}
        await store.notify_subscribers("test-001", new_event)
        # Queue should still be at capacity
        assert queue.qsize() == 100
        # Drain and check last item is the new event
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert items[-1] == new_event

    @pytest.mark.asyncio
    async def test_notify_no_subscribers_is_safe(self) -> None:
        store = TaskStore(maxsize=100)
        await store.notify_subscribers("nonexistent", {"test": True})


class TestStats:
    """Store statistics reporting."""

    def test_stats_empty_store(self) -> None:
        store = TaskStore(maxsize=100)
        stats = store.stats()
        assert stats["active_count"] == 0
        assert stats["completed_count"] == 0
        assert stats["tombstone_count"] == 0
        assert stats["subscriber_count"] == 0
        assert stats["pressure_level"] == "normal"
        assert stats["maxsize"] == 100

    def test_stats_reflects_state(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))
        store.create(_make_record("t2"))
        store.update_state("t1", A2ATaskState.WORKING)
        store.update_state("t1", A2ATaskState.COMPLETED)
        store.subscribe("t2")
        stats = store.stats()
        assert stats["active_count"] == 1
        assert stats["completed_count"] == 1
        assert stats["subscriber_count"] == 1


class TestCleanup:
    """Background cleanup task lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop_cleanup(self) -> None:
        store = TaskStore(maxsize=100, cleanup_interval_seconds=0.1)
        store.start_cleanup()
        assert store._cleanup_running is True
        assert len(store._cleanup_tasks) == 1
        # Let it run one cycle
        await asyncio.sleep(0.2)
        store.stop_cleanup()
        assert store._cleanup_running is False
        # Give cancellation a moment
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_start_cleanup_idempotent(self) -> None:
        store = TaskStore(maxsize=100, cleanup_interval_seconds=60.0)
        store.start_cleanup()
        store.start_cleanup()  # Second call should be no-op
        assert len(store._cleanup_tasks) == 1
        store.stop_cleanup()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_periodic_cleanup_sweeps_expired(self) -> None:
        store = TaskStore(
            maxsize=100,
            task_ttl_seconds=0.1,
            cleanup_interval_seconds=0.2,
        )
        store.create(_make_record())
        store.start_cleanup()
        # Wait for TTL expiry + one cleanup cycle
        await asyncio.sleep(0.5)
        store.stop_cleanup()
        assert "test-001" not in store._active
        await asyncio.sleep(0.1)
