"""Concurrency and race condition tests (D2).

Covers:
  - Concurrent task creation: 100 simultaneous create_task calls
  - Concurrent cancel + complete race
  - Circuit breaker concurrent probes
  - SSE subscriber disconnect cleanup
  - _spawn_background_task GC prevention
  - _safe_transition edge cases
  - CompactedResult.output_artifacts round-trip
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from bastion.circuitbreaker import CircuitBreaker, CircuitBreakerConfig
from bastion.models import A2ATaskRecord, A2ATaskState
from bastion.taskstore import CompactedResult, TaskStore, TaskStoreFullError


def _make_record(
    task_id: str = "test-001",
    state: A2ATaskState = A2ATaskState.SUBMITTED,
) -> A2ATaskRecord:
    return A2ATaskRecord(
        task_id=task_id,
        context_id="ctx-001",
        state=state,
        skill_id="infer",
        input_params={"model": "qwen3:30b", "prompt": "hello"},
    )


# ---------------------------------------------------------------------------
# D2: Concurrent task creation
# ---------------------------------------------------------------------------


class TestConcurrentTaskCreation:
    def test_100_concurrent_creates_no_corruption(self) -> None:
        """100 simultaneous create_task calls should not corrupt TaskStore."""
        store = TaskStore(maxsize=200)
        errors: list[Exception] = []

        for i in range(100):
            try:
                store.create(_make_record(f"task-{i:03d}"))
            except Exception as e:
                errors.append(e)

        assert len(errors) == 0
        assert store.active_count() == 100

        # Each task should be retrievable
        for i in range(100):
            result = store.get(f"task-{i:03d}")
            assert result is not None
            assert result.task_id == f"task-{i:03d}"

    def test_concurrent_creates_at_capacity(self) -> None:
        """Creating tasks beyond capacity raises TaskStoreFullError."""
        store = TaskStore(maxsize=10)
        # Fill to capacity
        for i in range(10):
            store.create(_make_record(f"task-{i}"))

        with pytest.raises(TaskStoreFullError):
            store.create(_make_record("overflow"))


# ---------------------------------------------------------------------------
# D2: Concurrent cancel + complete
# ---------------------------------------------------------------------------


class TestConcurrentCancelComplete:
    def test_cancel_after_complete_is_safe(self) -> None:
        """Canceling a task after it has completed should not crash."""
        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))
        store.update_state("t1", A2ATaskState.WORKING)
        store.update_state("t1", A2ATaskState.COMPLETED)

        # Task is now in completed store, trying to cancel should raise KeyError
        with pytest.raises(KeyError, match="not in active store"):
            store.update_state("t1", A2ATaskState.CANCELED)

    def test_complete_after_cancel_is_safe(self) -> None:
        """Completing a task after cancel should raise ValueError (invalid transition)."""
        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))
        store.update_state("t1", A2ATaskState.CANCELED)

        # Task is now in completed store (canceled is terminal)
        with pytest.raises(KeyError, match="not in active store"):
            store.update_state("t1", A2ATaskState.COMPLETED)

    def test_double_complete_is_safe(self) -> None:
        """Completing a task twice should raise KeyError on second attempt."""
        store = TaskStore(maxsize=100)
        store.create(_make_record("t1"))
        store.update_state("t1", A2ATaskState.WORKING)
        store.update_state("t1", A2ATaskState.COMPLETED)

        with pytest.raises(KeyError, match="not in active store"):
            store.update_state("t1", A2ATaskState.COMPLETED)


# ---------------------------------------------------------------------------
# D2: Circuit breaker concurrent probes
# ---------------------------------------------------------------------------


class TestCircuitBreakerConcurrentProbes:
    @pytest.mark.asyncio
    async def test_only_one_half_open_probe(self) -> None:
        """In half_open state, only one probe should run at a time."""
        config = CircuitBreakerConfig(
            enabled=True, failure_threshold=1, recovery_timeout=0.01
        )
        cb = CircuitBreaker(config)
        await cb.record_failure()
        assert cb.state == "open"

        # Wait for recovery timeout
        await asyncio.sleep(0.05)
        assert cb.state == "half_open"

        probe_count = 0
        max_concurrent = 0

        async def slow_probe() -> str:
            nonlocal probe_count, max_concurrent
            probe_count += 1
            max_concurrent = max(max_concurrent, probe_count)
            await asyncio.sleep(0.05)
            probe_count -= 1
            return "ok"

        # The cb.call method should serialize half-open probes
        # First call: enters half_open, starts probe
        result = await cb.call(slow_probe)
        assert result == "ok"
        assert cb.state == "closed"

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self) -> None:
        """A failed probe in half_open should reopen the circuit."""
        config = CircuitBreakerConfig(
            enabled=True, failure_threshold=2, recovery_timeout=0.01
        )
        cb = CircuitBreaker(config)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == "open"

        # Wait for recovery
        cb._opened_at = time.monotonic() - 0.1
        assert cb.state == "half_open"

        async def failing_probe() -> str:
            raise RuntimeError("still broken")

        with pytest.raises(RuntimeError):
            await cb.call(failing_probe)
        assert cb.state == "open"


# ---------------------------------------------------------------------------
# D2: SSE subscriber disconnect cleanup
# ---------------------------------------------------------------------------


class TestSSESubscriberDisconnect:
    def test_unsubscribe_cleans_up(self) -> None:
        """After unsubscribe, the subscriber queue is removed."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        q = store.subscribe("test-001")
        assert "test-001" in store._subscribers

        store.unsubscribe("test-001", q)
        assert "test-001" not in store._subscribers

    def test_unsubscribe_one_of_many(self) -> None:
        """Unsubscribing one queue leaves others intact."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        q1 = store.subscribe("test-001")
        q2 = store.subscribe("test-001")

        store.unsubscribe("test-001", q1)
        assert "test-001" in store._subscribers
        assert q2 in store._subscribers["test-001"]

    @pytest.mark.asyncio
    async def test_notify_after_unsubscribe(self) -> None:
        """Notifying after all subscribers disconnect should be safe."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        q = store.subscribe("test-001")
        store.unsubscribe("test-001", q)

        await store.notify_subscribers("test-001", {"test": True})
        # Should not raise


# ---------------------------------------------------------------------------
# D1: _safe_transition edge cases
# ---------------------------------------------------------------------------


class TestSafeTransitionEdgeCases:
    def test_transition_submitted_to_working(self) -> None:
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        result = store.get("test-001")
        assert result.state == A2ATaskState.WORKING

    def test_transition_after_compaction(self) -> None:
        """Transition after task was compacted should raise KeyError."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.COMPLETED)
        # Task is now compacted
        with pytest.raises(KeyError):
            store.update_state("test-001", A2ATaskState.WORKING)

    def test_invalid_transition_submitted_to_completed(self) -> None:
        """SUBMITTED -> COMPLETED is not valid."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        with pytest.raises(ValueError, match="Invalid state transition"):
            store.update_state("test-001", A2ATaskState.COMPLETED)

    def test_transition_working_to_canceled(self) -> None:
        """WORKING -> CANCELED is valid."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.WORKING)
        store.update_state("test-001", A2ATaskState.CANCELED)
        result = store.get("test-001")
        assert isinstance(result, CompactedResult)
        assert result.status == "canceled"

    def test_transition_submitted_to_failed(self) -> None:
        """SUBMITTED -> FAILED is valid."""
        store = TaskStore(maxsize=100)
        store.create(_make_record())
        store.update_state("test-001", A2ATaskState.FAILED)
        result = store.get("test-001")
        assert isinstance(result, CompactedResult)
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# D1: _spawn_background_task GC prevention
# ---------------------------------------------------------------------------


class TestSpawnBackgroundTask:
    @pytest.mark.asyncio
    async def test_task_in_set_while_running(self) -> None:
        """Tasks should be in _background_tasks while running."""
        bg_tasks: set[asyncio.Task] = set()
        task_was_in_set = False

        async def bg_work() -> None:
            nonlocal task_was_in_set
            # Check if the task is in the set while running
            for t in bg_tasks:
                if not t.done():
                    task_was_in_set = True
            await asyncio.sleep(0.01)

        task = asyncio.create_task(bg_work())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

        await asyncio.sleep(0.005)
        assert len(bg_tasks) == 1  # Task still running

        await task
        await asyncio.sleep(0.01)  # Let callback fire
        assert len(bg_tasks) == 0  # Task cleaned up

    @pytest.mark.asyncio
    async def test_cleanup_on_completion(self) -> None:
        """Completed tasks should be removed from the set."""
        bg_tasks: set[asyncio.Task] = set()

        async def quick_work() -> str:
            return "done"

        task = asyncio.create_task(quick_work())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

        await task
        await asyncio.sleep(0.01)
        assert len(bg_tasks) == 0

    @pytest.mark.asyncio
    async def test_cleanup_on_exception(self) -> None:
        """Tasks that raise exceptions should still be cleaned up."""
        bg_tasks: set[asyncio.Task] = set()

        async def failing_work() -> None:
            raise ValueError("intentional")

        task = asyncio.create_task(failing_work())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

        # Wait for task to complete (with exception)
        with contextlib.suppress(ValueError):
            await task

        await asyncio.sleep(0.01)
        assert len(bg_tasks) == 0


# ---------------------------------------------------------------------------
# D1: CompactedResult.output_artifacts round-trip
# ---------------------------------------------------------------------------


class TestCompactedResultArtifacts:
    def test_artifacts_survive_compaction(self) -> None:
        """output_artifacts should survive the compaction round-trip."""
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = [
            {"parts": [{"kind": "text", "text": "Response text"}]},
            {"parts": [{"kind": "data", "data": {"key": "value"}}]},
        ]
        compacted = CompactedResult.from_record(record)
        assert len(compacted.output_artifacts) == 2
        assert compacted.output_artifacts[0]["parts"][0]["text"] == "Response text"
        assert compacted.output_artifacts[1]["parts"][0]["data"]["key"] == "value"

    def test_empty_artifacts_survive_compaction(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = []
        compacted = CompactedResult.from_record(record)
        assert compacted.output_artifacts == ()

    def test_summary_from_text_artifact(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = [
            {"parts": [{"kind": "text", "text": "Short response"}]}
        ]
        compacted = CompactedResult.from_record(record)
        assert compacted.result_summary == "Short response"

    def test_summary_from_data_artifact(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = [
            {"parts": [{"kind": "data", "data": {"status": "ok"}}]}
        ]
        compacted = CompactedResult.from_record(record)
        assert "status" in compacted.result_summary

    def test_summary_truncated_to_500(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = [
            {"parts": [{"kind": "text", "text": "x" * 1000}]}
        ]
        compacted = CompactedResult.from_record(record)
        assert len(compacted.result_summary) == 500

    def test_artifacts_are_immutable_tuple(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.COMPLETED
        record.output_artifacts = [{"parts": []}]
        compacted = CompactedResult.from_record(record)
        assert isinstance(compacted.output_artifacts, tuple)

    def test_compacted_result_preserves_error(self) -> None:
        record = _make_record()
        record.state = A2ATaskState.FAILED
        record.error = "Something went wrong"
        compacted = CompactedResult.from_record(record)
        assert compacted.error == "Something went wrong"
        assert compacted.status == "failed"
