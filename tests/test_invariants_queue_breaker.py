"""Property-based invariants for AffinityQueue and CircuitBreaker.

Complements the example-based tests in ``test_queue.py`` and
``test_circuitbreaker.py``.  Hypothesis explores arbitrary submission
sequences and event interleavings that hand-written examples miss.

Invariants pinned (see module docstring per test):

AffinityQueue
    INV1  priority ordering at equal age
    INV2  aging fairness — older low-priority eventually overtakes new high
    INV3  within a model group, dequeue order is non-increasing age (no
          out-of-order at identical base_priority)
    INV4  no-loss — every enqueued request is later dequeued or counted
    INV5  size cap — len <= max_queue_size, overflow rejected

CircuitBreaker
    INV6  state is always one of {closed, open, half_open}
    INV7  CLOSED -> OPEN after exactly N consecutive failures
    INV8  OPEN -> HALF_OPEN once recovery_timeout elapses
    INV9  HALF_OPEN -> CLOSED on successful probe
    INV10 HALF_OPEN -> OPEN on failed probe
    INV11 success in CLOSED never changes state
    INV12 consecutive-failure counter resets on transition to CLOSED

INV13 (backoff bounded) is intentionally omitted — the current
``CircuitBreaker`` implementation has no exponential backoff and no
``max_recovery_timeout``; ``recovery_timeout`` is a fixed scalar.
"""

from __future__ import annotations

import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bastion.circuitbreaker import CircuitBreaker
from bastion.models import (
    CircuitBreakerConfig,
    ModelInfo,
    PriorityTier,
    QueuedRequest,
    SchedulerConfig,
)
from bastion.queue import AffinityQueue
from tests.conftest import make_request

# ---------------------------------------------------------------------------
# Shared strategies and helpers
# ---------------------------------------------------------------------------

_MODELS = ["qwen3:14b", "llama3.1:8b", "mistral-nemo:12b"]
_TIERS = [
    PriorityTier.INTERACTIVE,
    PriorityTier.AGENT,
    PriorityTier.PIPELINE,
    PriorityTier.BACKGROUND,
]

# (model, base_priority, age_seconds)
_submission_strategy = st.tuples(
    st.sampled_from(_MODELS),
    st.floats(min_value=10.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
)


def _make_queue(max_size: int = 1024, aging_rate: float = 2.0) -> AffinityQueue:
    """Build a fresh AffinityQueue with explicit knobs.

    Kept inline (no conftest changes) per task constraints.
    """
    cfg = SchedulerConfig(
        cooldown_seconds=0.0,
        model_affinity_bonus=10.0,
        aging_rate=aging_rate,
        max_queue_size=max_size,
    )
    return AffinityQueue(cfg)


def _enqueue_submission(
    queue: AffinityQueue,
    model: str,
    base_priority: float,
    age_seconds: float,
    now: float | None = None,
) -> QueuedRequest:
    """Build and enqueue a request with controlled submitted_at."""
    submitted_at = (now if now is not None else time.time()) - age_seconds
    req = make_request(
        model=model,
        tier=PriorityTier.AGENT,
        base_priority=base_priority,
        submitted_at=submitted_at,
    )
    queue.enqueue(req)
    return req


# ===========================================================================
# AffinityQueue invariants
# ===========================================================================


class TestAffinityQueueInvariants:
    """Property tests for AffinityQueue."""

    # ------------------------------------------------------------------ INV1
    @given(
        priorities=st.lists(
            st.floats(min_value=1.0, max_value=200.0, allow_nan=False, allow_infinity=False),
            min_size=2,
            max_size=20,
            unique=True,
        ),
    )
    @settings(max_examples=50, deadline=2000)
    def test_inv1_priority_ordering_at_equal_age(self, priorities: list[float]) -> None:
        """INV1: with identical submission times, the highest base_priority
        across all model groups is picked first.
        """
        queue = _make_queue()
        now = time.time()
        for i, p in enumerate(priorities):
            # Distribute across models to exercise cross-group selection.
            model = _MODELS[i % len(_MODELS)]
            _enqueue_submission(queue, model, p, age_seconds=0.0, now=now)

        best = queue.pick_next(current_model=None)
        assert best is not None
        assert best.base_priority == max(priorities)

    # ------------------------------------------------------------------ INV2
    @given(
        old_base=st.floats(min_value=1.0, max_value=20.0, allow_nan=False, allow_infinity=False),
        new_base=st.floats(min_value=50.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        aging_rate=st.floats(min_value=0.5, max_value=10.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=50, deadline=2000)
    def test_inv2_aging_overtakes_given_enough_age(
        self,
        old_base: float,
        new_base: float,
        aging_rate: float,
    ) -> None:
        """INV2: an older low-priority request overtakes a younger high one
        once age * aging_rate exceeds the base gap.
        """
        # Choose age so that old's effective priority strictly exceeds new's.
        gap = new_base - old_base
        # Need age * aging_rate > gap  ->  age > gap / aging_rate.
        age = (gap / aging_rate) + 5.0  # comfortable margin

        queue = _make_queue(aging_rate=aging_rate)
        now = time.time()
        old_req = _enqueue_submission(queue, "qwen3:14b", old_base, age, now=now)
        _enqueue_submission(queue, "qwen3:14b", new_base, 0.0, now=now)

        picked = queue.dequeue_for_model("qwen3:14b")
        assert picked is not None
        assert picked.id == old_req.id, (
            f"old(age={age:.1f}s base={old_base}) should overtake "
            f"new(base={new_base}) at aging_rate={aging_rate}"
        )

    # ------------------------------------------------------------------ INV3
    @given(
        # Integer seconds — guaranteed to produce distinguishable
        # submitted_at floats. Fractional ages at time.time() scale (~1.7e9)
        # can collapse to identical submitted_at due to float64 ulp
        # (~2.4e-7s), which is a precision artefact of the timestamp
        # source, not a queue ordering bug.
        ages=st.lists(
            st.integers(min_value=1, max_value=300).map(float),
            min_size=2,
            max_size=15,
            unique=True,
        ),
    )
    @settings(max_examples=50, deadline=2000)
    def test_inv3_within_model_order_by_age(self, ages: list[float]) -> None:
        """INV3: with equal base_priority, requests within a model dequeue
        in descending age (oldest first) — the queue's age-based effective
        priority gives a deterministic order within a group.
        """
        queue = _make_queue()
        now = time.time()
        ids_by_age: dict[float, str] = {}
        for age in ages:
            req = _enqueue_submission(
                queue, "qwen3:14b", base_priority=50.0, age_seconds=age, now=now,
            )
            ids_by_age[age] = req.id

        # Dequeue everything from the single model group.
        dequeued: list[QueuedRequest] = []
        while True:
            r = queue.dequeue_for_model("qwen3:14b")
            if r is None:
                break
            dequeued.append(r)

        assert len(dequeued) == len(ages)
        expected_order = sorted(ages, reverse=True)  # oldest first
        # Walk both lists in lockstep: each position must hold the request
        # whose age matches the expected (descending) ordering.
        for expected_age, got in zip(expected_order, dequeued, strict=True):
            assert got.id == ids_by_age[expected_age], (
                f"expected oldest-first dequeue: at position with age={expected_age:.2f} "
                f"got id={got.id}, expected id={ids_by_age[expected_age]}"
            )

    # ------------------------------------------------------------------ INV4
    @given(
        submissions=st.lists(_submission_strategy, min_size=1, max_size=40),
        dequeue_count=st.integers(min_value=0, max_value=40),
    )
    @settings(max_examples=80, deadline=3000)
    def test_inv4_no_loss_across_arbitrary_ops(
        self,
        submissions: list[tuple[str, float, float]],
        dequeue_count: int,
    ) -> None:
        """INV4: total_size always equals (enqueued_accepted - dequeued).

        For any submission sequence followed by an arbitrary number of
        dequeues, no request silently disappears.
        """
        max_size = 1024
        queue = _make_queue(max_size=max_size)
        accepted = 0
        now = time.time()
        for model, prio, age in submissions:
            req = make_request(
                model=model,
                tier=PriorityTier.AGENT,
                base_priority=prio,
                submitted_at=now - age,
            )
            if queue.enqueue(req):
                accepted += 1

        assert queue.total_size == accepted

        actually_dequeued = 0
        for _ in range(dequeue_count):
            # pick_next then dequeue, exactly like the scheduler does.
            best = queue.pick_next()
            if best is None:
                break
            got = queue.dequeue_for_model(best.model)
            assert got is not None
            actually_dequeued += 1

        assert queue.total_size == accepted - actually_dequeued
        # Drain the rest and confirm conservation.
        drained = queue.drain_all()
        assert len(drained) + actually_dequeued == accepted

    # ------------------------------------------------------------------ INV5
    @given(
        max_size=st.integers(min_value=1, max_value=8),
        extra=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=40, deadline=2000)
    def test_inv5_size_cap_enforced(self, max_size: int, extra: int) -> None:
        """INV5: enqueue beyond max_queue_size returns False and the
        recorded length never exceeds the cap.
        """
        queue = _make_queue(max_size=max_size)
        attempts = max_size + extra
        accepted = 0
        rejected = 0
        for _ in range(attempts):
            ok = queue.enqueue(
                make_request(model="qwen3:14b", tier=PriorityTier.AGENT),
            )
            if ok:
                accepted += 1
            else:
                rejected += 1
            assert queue.total_size <= max_size

        assert accepted == max_size
        assert rejected == extra


# ===========================================================================
# CircuitBreaker invariants
# ===========================================================================


def _cb(failure_threshold: int = 3, recovery_timeout: float = 1.0) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitBreakerConfig(
            enabled=True,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        ),
    )


def _expire_open(cb: CircuitBreaker) -> None:
    """Force the breaker past its recovery_timeout deterministically."""
    cb._opened_at = time.monotonic() - cb._config.recovery_timeout - 1.0


# Event alphabet for the state-machine walk.
_CB_EVENTS = ["success", "failure", "advance_time"]


class TestCircuitBreakerInvariants:
    """Property tests for the three-state CircuitBreaker."""

    # ------------------------------------------------------------------ INV6
    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(events=st.lists(st.sampled_from(_CB_EVENTS), min_size=1, max_size=50))
    @pytest.mark.asyncio
    async def test_inv6_state_always_valid(self, events: list[str]) -> None:
        """INV6: for any event sequence, ``state`` is always one of the
        three documented values.
        """
        cb = _cb(failure_threshold=3, recovery_timeout=0.5)
        for ev in events:
            if ev == "success":
                await cb.record_success()
            elif ev == "failure":
                await cb.record_failure()
            elif ev == "advance_time":
                _expire_open(cb)
            assert cb.state in {"closed", "open", "half_open"}

    # ------------------------------------------------------------------ INV7
    @settings(
        max_examples=40,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        threshold=st.integers(min_value=1, max_value=10),
        non_trip_failures=st.integers(min_value=0, max_value=9),
    )
    @pytest.mark.asyncio
    async def test_inv7_trips_at_exact_threshold(
        self,
        threshold: int,
        non_trip_failures: int,
    ) -> None:
        """INV7: from CLOSED, the breaker is still CLOSED after
        ``threshold - 1`` failures and OPEN after exactly ``threshold``.
        """
        cb = _cb(failure_threshold=threshold, recovery_timeout=10.0)
        below = min(non_trip_failures, threshold - 1)
        for _ in range(below):
            await cb.record_failure()
        assert cb.state == "closed", (
            f"expected closed after {below} of {threshold} failures, "
            f"got {cb.state}"
        )
        # Now reach the threshold exactly.
        for _ in range(threshold - below):
            await cb.record_failure()
        assert cb.state == "open"

    # ------------------------------------------------------------------ INV8
    @settings(
        max_examples=30,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(threshold=st.integers(min_value=1, max_value=5))
    @pytest.mark.asyncio
    async def test_inv8_open_to_half_open_after_timeout(self, threshold: int) -> None:
        """INV8: once recovery_timeout has elapsed, an OPEN breaker reports
        ``half_open`` (the auto-promotion in the ``state`` property).
        """
        cb = _cb(failure_threshold=threshold, recovery_timeout=1.0)
        for _ in range(threshold):
            await cb.record_failure()
        assert cb.state == "open"
        _expire_open(cb)
        assert cb.state == "half_open"

    # ----------------------------------------------------------------- INV9
    @settings(
        max_examples=30,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(threshold=st.integers(min_value=1, max_value=5))
    @pytest.mark.asyncio
    async def test_inv9_half_open_success_closes(self, threshold: int) -> None:
        """INV9: a successful probe from HALF_OPEN restores CLOSED."""
        cb = _cb(failure_threshold=threshold, recovery_timeout=1.0)
        for _ in range(threshold):
            await cb.record_failure()
        _expire_open(cb)
        assert cb.state == "half_open"

        async def ok() -> str:
            return "ok"

        result = await cb.call(ok)
        assert result == "ok"
        assert cb.state == "closed"

    # ----------------------------------------------------------------- INV10
    @settings(
        max_examples=30,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(threshold=st.integers(min_value=1, max_value=5))
    @pytest.mark.asyncio
    async def test_inv10_half_open_failure_reopens(self, threshold: int) -> None:
        """INV10: a failed probe from HALF_OPEN immediately reopens."""
        cb = _cb(failure_threshold=threshold, recovery_timeout=1.0)
        for _ in range(threshold):
            await cb.record_failure()
        _expire_open(cb)
        assert cb.state == "half_open"

        async def boom() -> None:
            raise RuntimeError("still broken")

        with pytest.raises(RuntimeError):
            await cb.call(boom)
        assert cb.state == "open"

    # ----------------------------------------------------------------- INV11
    @settings(
        max_examples=80,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(n_successes=st.integers(min_value=1, max_value=50))
    @pytest.mark.asyncio
    async def test_inv11_successes_keep_closed(self, n_successes: int) -> None:
        """INV11: from CLOSED, any number of successes leaves CLOSED."""
        cb = _cb(failure_threshold=3, recovery_timeout=10.0)
        for _ in range(n_successes):
            await cb.record_success()
        assert cb.state == "closed"

    # ----------------------------------------------------------------- INV12
    @settings(
        max_examples=40,
        deadline=2000,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        threshold=st.integers(min_value=2, max_value=6),
        pre_failures=st.integers(min_value=0, max_value=5),
    )
    @pytest.mark.asyncio
    async def test_inv12_success_resets_counter(
        self,
        threshold: int,
        pre_failures: int,
    ) -> None:
        """INV12: a success in CLOSED zeroes the consecutive-failure
        counter — subsequent ``threshold - 1`` failures must NOT trip.
        """
        cb = _cb(failure_threshold=threshold, recovery_timeout=10.0)
        # Stay strictly below the trip line.
        pre = min(pre_failures, threshold - 1)
        for _ in range(pre):
            await cb.record_failure()
        await cb.record_success()
        # Counter should be reset — threshold-1 more failures still CLOSED.
        for _ in range(threshold - 1):
            await cb.record_failure()
        assert cb.state == "closed"
        # One more failure trips it (sanity).
        await cb.record_failure()
        assert cb.state == "open"


# ---------------------------------------------------------------------------
# Hypothesis configuration sanity — ensure the strategies and types resolve.
# ---------------------------------------------------------------------------

def test_strategies_resolve() -> None:
    """Smoke check: strategy module imports and basic types remain stable."""
    assert isinstance(_MODELS, list)
    assert PriorityTier.AGENT in _TIERS
    assert isinstance(ModelInfo(vram_gb=1.0).vram_gb, float)
