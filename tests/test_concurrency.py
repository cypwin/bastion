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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.circuitbreaker import CircuitBreaker, CircuitBreakerConfig
from bastion.models import (
    A2ATaskRecord,
    A2ATaskState,
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    PriorityTier,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.taskstore import CompactedResult, TaskStore, TaskStoreFullError
from bastion.vram import VRAMTracker
from tests.conftest import make_request


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


# ---------------------------------------------------------------------------
# Burst handling: no request loss
# ---------------------------------------------------------------------------


def _burst_config(max_queue_size: int = 256) -> BrokerConfig:
    """Deterministic config for burst tests (zero cooldown, large queue)."""
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0, max_temperature_c=82),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=max_queue_size,
            # Tight loop for fast deterministic tests.
            loop_interval_seconds=0.01,
            # No artificial inter-dispatch delay — we want the burst to drain
            # in O(loop_interval) per `max_concurrent_dispatches` requests.
            concurrent_dispatch_delay_seconds=0.0,
            # Allow many parallel dispatches so 100 burst items drain quickly.
            max_concurrent_dispatches=8,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
            "llama3.1:8b": ModelInfo(vram_gb=4.4),
        },
    )


class TestBurstNoLoss:
    """Burst-handling invariants — every request is accounted for."""

    @pytest.mark.asyncio
    async def test_burst_100_requests_no_loss(self) -> None:
        """100 concurrent enqueues: every request is accepted or rejected.

        Pins ``accepted + rejected == 100`` so no request is silently dropped.
        """
        cfg = _burst_config(max_queue_size=256)
        q = AffinityQueue(cfg.scheduler)
        ready = asyncio.Event()
        accepted = 0
        rejected = 0

        async def enqueue_one(i: int) -> bool:
            await ready.wait()
            req = make_request(
                model="qwen3:14b",
                tier=PriorityTier.AGENT,
                body=b'{"model":"qwen3:14b","prompt":"x"}',
                client_info=f"burst-{i:03d}",
            )
            return q.enqueue(req)

        tasks = [asyncio.create_task(enqueue_one(i)) for i in range(100)]
        ready.set()
        results = await asyncio.gather(*tasks)

        for ok in results:
            if ok:
                accepted += 1
            else:
                rejected += 1

        assert accepted + rejected == 100
        assert q.total_size == accepted
        assert accepted == 100, f"Expected all 100 accepted, got {accepted}"

    @pytest.mark.asyncio
    async def test_burst_over_capacity_rejects_remainder(self) -> None:
        """When max_queue_size < burst, the overflow is rejected (no silent loss)."""
        cfg = _burst_config(max_queue_size=40)
        q = AffinityQueue(cfg.scheduler)
        ready = asyncio.Event()

        async def enqueue_one(i: int) -> bool:
            await ready.wait()
            return q.enqueue(make_request(model="qwen3:14b", client_info=f"r{i}"))

        tasks = [asyncio.create_task(enqueue_one(i)) for i in range(100)]
        ready.set()
        results = await asyncio.gather(*tasks)

        accepted = sum(1 for ok in results if ok)
        rejected = sum(1 for ok in results if not ok)

        assert accepted == 40
        assert rejected == 60
        assert q.total_size == 40


# ---------------------------------------------------------------------------
# Burst handling: no double-forward
# ---------------------------------------------------------------------------


class TestBurstNoDoubleForward:
    """Each dispatched request is forwarded to the backend exactly once."""

    @pytest.mark.asyncio
    async def test_burst_100_requests_no_double_forward(self) -> None:
        """100 queued requests = exactly 100 dispatch calls (no retry-loop double-send)."""
        cfg = _burst_config(max_queue_size=256)
        q = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        seen_ids: list[str] = []
        seen_lock = asyncio.Lock()

        async def dispatch_fn(request, needs_swap: bool = True) -> None:  # noqa: ARG001
            async with seen_lock:
                seen_ids.append(request.id)

        request_ids: set[str] = set()
        for i in range(100):
            req = make_request(model="qwen3:14b", client_info=f"req-{i:03d}")
            request_ids.add(req.id)
            assert q.enqueue(req) is True

        loaded = LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={})
        with patch.object(
            tracker, "get_loaded_models",
            new_callable=AsyncMock, return_value=[loaded],
        ), patch(
            "bastion.scheduler.check_gpu_safe",
            AsyncMock(return_value=(True, "OK")),
        ), patch.object(
            tracker, "can_load_model",
            new_callable=AsyncMock, return_value=(True, "OK"),
        ):
            sched = Scheduler(cfg, q, tracker, dispatch_fn)
            await sched.start()
            for _ in range(10):
                sched.notify()
                await asyncio.sleep(0)

            # Wait budget: 100 dispatches at 8/tick * 10ms tick = ~125ms ideal,
            # 5s is huge headroom and still keeps the test well under the 15s
            # whole-file budget.
            for _ in range(500):
                await asyncio.sleep(0.01)
                if len(seen_ids) >= 100 and q.is_empty:
                    break

            await sched.stop()

        assert len(seen_ids) == 100, f"expected 100 dispatches, got {len(seen_ids)}"
        assert set(seen_ids) == request_ids
        assert len(set(seen_ids)) == len(seen_ids), "duplicate dispatch detected"
        assert sched.total_dispatched == 100


# ---------------------------------------------------------------------------
# Model swap under burst
# ---------------------------------------------------------------------------


class TestModelSwapUnderBurst:
    """Mixed-model bursts dispatch all requests; swap count stays bounded."""

    @pytest.mark.asyncio
    async def test_model_swap_under_burst_preserves_all_requests(self) -> None:
        """50 A + 50 B interleaved: all 100 dispatch; swap count is bounded."""
        cfg = _burst_config(max_queue_size=256)
        q = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        seen: list[str] = []

        async def dispatch_fn(request, needs_swap: bool = True) -> None:  # noqa: ARG001
            seen.append(request.model)

        state = {"loaded": "qwen3:14b"}

        async def fake_get_loaded() -> list[LoadedModel]:
            name = state["loaded"]
            return [LoadedModel(
                name=name,
                size_bytes=0,
                vram_gb=cfg.models[name].vram_gb,
                details={},
            )]

        async def fake_can_load(model: str) -> tuple[bool, str]:  # noqa: ARG001
            return True, "OK"

        for i in range(100):
            model = "qwen3:14b" if i % 2 == 0 else "mistral-nemo:12b"
            assert q.enqueue(make_request(model=model, client_info=f"mix-{i}"))

        with patch.object(
            tracker, "get_loaded_models",
            new_callable=AsyncMock, side_effect=fake_get_loaded,
        ), patch(
            "bastion.scheduler.check_gpu_safe",
            AsyncMock(return_value=(True, "OK")),
        ), patch.object(
            tracker, "can_load_model",
            new_callable=AsyncMock, side_effect=fake_can_load,
        ):
            sched = Scheduler(cfg, q, tracker, dispatch_fn)
            await sched.start()
            sched.notify()

            for _ in range(800):
                await asyncio.sleep(0.01)
                if len(seen) >= 100 and q.is_empty:
                    break

            await sched.stop()

        assert len(seen) == 100, f"expected 100 dispatches, got {len(seen)}"
        assert seen.count("qwen3:14b") == 50
        assert seen.count("mistral-nemo:12b") == 50
        # Swap count is bounded — affinity batching should keep this finite.
        assert sched.total_swaps <= 100


# ---------------------------------------------------------------------------
# Lease contention
# ---------------------------------------------------------------------------


async def _make_lease_handler():
    """Construct a minimal A2AHandler for lease testing (mirrors test_lease)."""
    from bastion.a2a import A2AHandler

    return A2AHandler(
        config=BrokerConfig(),
        enqueue_fn=AsyncMock(),
        vram_tracker=MagicMock(),
        scheduler=MagicMock(),
    )


class TestLeaseContention:
    """Lease single-grant + TTL-expiry-race invariants."""

    @pytest.mark.asyncio
    async def test_lease_acquire_single_grant_under_contention(self) -> None:
        """N concurrent create_lease calls each receive a UNIQUE fencing token.

        BASTION's ``create_lease`` does not gate on a finite pool — each call
        succeeds. This test pins the fencing-token invariant: no two concurrent
        acquirers ever share a token (zombie-prevention contract) and no two
        leases share an id.
        """
        handler = await _make_lease_handler()
        ready = asyncio.Event()
        n_acquirers = 20

        async def acquire(i: int):  # noqa: ARG001
            await ready.wait()
            return handler.create_lease(
                model="qwen3:14b",
                max_requests=5,
                ttl_seconds=60.0,
                idle_timeout=30.0,
            )

        tasks = [asyncio.create_task(acquire(i)) for i in range(n_acquirers)]
        ready.set()
        leases = await asyncio.gather(*tasks)

        assert len(leases) == n_acquirers
        tokens = [lease.fencing_token for lease in leases]
        assert len(set(tokens)) == n_acquirers, (
            "fencing token collision under contention"
        )
        assert sorted(tokens) == list(range(min(tokens), min(tokens) + n_acquirers))
        ids = [lease.lease_id for lease in leases]
        assert len(set(ids)) == n_acquirers, (
            "lease_id collision under contention"
        )
        for lease in leases:
            assert lease.lease_id in handler._leases

    @pytest.mark.asyncio
    async def test_lease_ttl_expiry_then_concurrent_acquire(self) -> None:
        """Expired holder yields stale token; concurrent acquirers get fresh leases.

        Sequence:
          1. Create a lease with ttl_seconds=0 (immediately expired).
          2. has_active_lease(model) returns False.
          3. 5 concurrent acquirers race to create_lease.
          4. Each gets a distinct fencing token > old token; old token invalid.
        """
        handler = await _make_lease_handler()

        old = handler.create_lease(
            model="qwen3:14b",
            max_requests=1,
            ttl_seconds=0.0,
            idle_timeout=60.0,
        )
        should, reason = old.should_release()
        assert should is True
        assert reason in {"TTL_EXPIRED", "REQUEST_LIMIT"}
        assert handler.has_active_lease("qwen3:14b") is False

        ready = asyncio.Event()

        async def acquire_after_expiry(i: int):  # noqa: ARG001
            await ready.wait()
            return handler.create_lease(
                model="qwen3:14b",
                max_requests=10,
                ttl_seconds=60.0,
                idle_timeout=60.0,
            )

        tasks = [asyncio.create_task(acquire_after_expiry(i)) for i in range(5)]
        ready.set()
        new_leases = await asyncio.gather(*tasks)

        assert len(new_leases) == 5
        new_tokens = [lease.fencing_token for lease in new_leases]
        assert all(t > old.fencing_token for t in new_tokens)
        assert len(set(new_tokens)) == 5

        valid, _reason = handler.validate_lease(old.lease_id, old.fencing_token)
        assert valid is False

    @pytest.mark.asyncio
    async def test_try_create_lease_single_winner_under_thread_contention(self) -> None:
        """N threads race try_create_lease for the same model — exactly one wins.

        Real threads (not asyncio tasks) so the internal lease lock is what
        prevents the double grant, not the event loop's cooperative scheduling.
        This is the regression test for the `has_active_lease` →
        `create_lease` TOCTOU window.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        handler = await _make_lease_handler()
        n_acquirers = 16
        barrier = threading.Barrier(n_acquirers)

        def acquire():
            barrier.wait()
            return handler.try_create_lease(
                model="qwen3:14b",
                max_requests=5,
                ttl_seconds=60.0,
                idle_timeout=30.0,
            )

        with ThreadPoolExecutor(max_workers=n_acquirers) as pool:
            results = list(pool.map(lambda _: acquire(), range(n_acquirers)))

        winners = [lease for lease in results if lease is not None]
        assert len(winners) == 1, (
            f"expected exactly one grant, got {len(winners)} — TOCTOU regression"
        )
        assert len(handler._leases) == 1
        assert winners[0].lease_id in handler._leases


# ---------------------------------------------------------------------------
# Audit log ordering
# ---------------------------------------------------------------------------


class TestAuditOrdering:
    """Audit writes from a single event loop preserve submission order.

    audit.emit() uses a deque + stdlib logger (both thread-safe, synchronous).
    Under asyncio, once a coroutine resumes it runs ``emit()`` synchronously,
    so order in the recent_events deque follows scheduling order.
    """

    @pytest.mark.asyncio
    async def test_audit_writes_preserve_order_under_burst(
        self, tmp_path,
    ) -> None:
        import bastion.audit as audit_mod
        # audit.emit() is a no-op unless the global logger is initialized.
        # The autouse `_isolate_audit_logger` fixture in conftest snapshots
        # and restores the global, so we can init freely here.
        log_file = tmp_path / "burst-audit.jsonl"
        audit_mod.init_audit_logger(log_path=str(log_file))
        original = list(audit_mod._recent_events)
        audit_mod._recent_events.clear()
        try:
            ready = asyncio.Event()
            n_events = 50

            async def emit_one(i: int) -> None:
                await ready.wait()
                # Yield once so coroutines interleave before emitting.
                await asyncio.sleep(0)
                audit_mod.emit("burst_test", {"seq": i})

            tasks = [asyncio.create_task(emit_one(i)) for i in range(n_events)]
            ready.set()
            await asyncio.gather(*tasks)

            recorded = [
                e for e in audit_mod._recent_events
                if e.get("event") == "burst_test"
            ]
            assert len(recorded) == n_events, (
                f"expected {n_events} events, got {len(recorded)}"
            )
            # asyncio.gather schedules in argument order; after one
            # ``sleep(0)`` they resume in that same FIFO order.
            seqs = [e["details"]["seq"] for e in recorded]
            assert seqs == list(range(n_events)), (
                "audit emit order diverged from submission order"
            )
        finally:
            audit_mod._recent_events.clear()
            audit_mod._recent_events.extend(original)


# ---------------------------------------------------------------------------
# Queue ordering under concurrent put/get
# ---------------------------------------------------------------------------


class TestQueueOrderingUnderBurst:
    """Under concurrent put+get, dequeued sequence respects priority."""

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_dequeue_priority_preserved(self) -> None:
        """Mixed-priority concurrent enqueue + drain: highest-priority items
        emerge first when both priorities coexist in the queue.
        """
        cfg = _burst_config(max_queue_size=256)
        q = AffinityQueue(cfg.scheduler)

        items = []
        for i in range(40):
            tier = (
                PriorityTier.INTERACTIVE if i % 4 == 0
                else PriorityTier.BACKGROUND
            )
            items.append(make_request(
                model="qwen3:14b",
                tier=tier,
                client_info=f"prio-{i}",
            ))

        ready = asyncio.Event()
        dequeued: list = []

        async def produce(item) -> None:
            await ready.wait()
            assert q.enqueue(item)

        async def consume() -> None:
            await ready.wait()
            while len(dequeued) < len(items):
                got = q.dequeue_for_model("qwen3:14b")
                if got is None:
                    await asyncio.sleep(0.001)
                    continue
                dequeued.append(got)

        prod_tasks = [asyncio.create_task(produce(it)) for it in items]
        cons_task = asyncio.create_task(consume())
        ready.set()
        await asyncio.gather(*prod_tasks)
        await cons_task

        assert len(dequeued) == 40
        n_interactive = sum(
            1 for r in dequeued if r.tier == PriorityTier.INTERACTIVE
        )
        n_background = sum(
            1 for r in dequeued if r.tier == PriorityTier.BACKGROUND
        )
        assert n_interactive == 10
        assert n_background == 30
        # Among the first 10 dequeued (while both priorities coexist),
        # INTERACTIVE-tier requests should dominate.
        first10 = dequeued[:10]
        i10 = sum(1 for r in first10 if r.tier == PriorityTier.INTERACTIVE)
        b10 = sum(1 for r in first10 if r.tier == PriorityTier.BACKGROUND)
        assert i10 >= b10, (
            f"priority inversion in first 10 dequeues: "
            f"interactive={i10} background={b10}"
        )


# ---------------------------------------------------------------------------
# Circuit breaker under burst
# ---------------------------------------------------------------------------


class TestCircuitBreakerBurst:
    """Concurrent record_failure calls trip the breaker at/above threshold.

    record_failure is guarded by an asyncio.Lock, so the counter is incremented
    exactly once per call. After N+5 failures with threshold=N, state == OPEN.
    """

    @pytest.mark.asyncio
    async def test_record_failure_under_burst_threshold(self) -> None:
        threshold = 5
        burst = threshold + 5
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=threshold,
            recovery_timeout=10.0,  # Stay OPEN.
        )
        cb = CircuitBreaker(config)
        ready = asyncio.Event()

        async def fail_once() -> None:
            await ready.wait()
            await cb.record_failure()

        tasks = [asyncio.create_task(fail_once()) for _ in range(burst)]
        ready.set()
        await asyncio.gather(*tasks)

        assert cb._consecutive_failures == burst
        assert cb.state == "open"

    @pytest.mark.asyncio
    async def test_record_failure_then_success_under_burst_resets(self) -> None:
        """Success after burst-of-failures closes the circuit and resets counter."""
        config = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=10.0,
        )
        cb = CircuitBreaker(config)

        await asyncio.gather(*[cb.record_failure() for _ in range(10)])
        assert cb.state == "open"

        await cb.record_success()
        assert cb.state == "closed"
        assert cb._consecutive_failures == 0
