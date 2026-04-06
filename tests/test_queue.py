"""Tests for AffinityQueue — enqueue, dequeue, affinity, aging, limits."""

from __future__ import annotations

import time

from bastion.models import PriorityTier
from bastion.queue import AffinityQueue
from tests.conftest import make_request

# ---------------------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------------------

class TestEnqueueDequeue:
    def test_enqueue_and_size(self, queue):
        r = make_request(model="qwen3:14b")
        assert queue.enqueue(r) is True
        assert queue.total_size == 1
        assert queue.is_empty is False

    def test_dequeue_returns_request(self, queue):
        r = make_request(model="qwen3:14b")
        queue.enqueue(r)
        result = queue.dequeue_for_model("qwen3:14b")
        assert result is not None
        assert result.id == r.id
        assert queue.total_size == 0

    def test_dequeue_empty_returns_none(self, queue):
        assert queue.dequeue_for_model("nonexistent") is None

    def test_dequeue_wrong_model_returns_none(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        assert queue.dequeue_for_model("mistral-nemo:12b") is None
        assert queue.total_size == 1  # Still in queue

    def test_multiple_models(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        queue.enqueue(make_request(model="mistral-nemo:12b"))
        queue.enqueue(make_request(model="qwen3:14b"))
        assert queue.total_size == 3
        depths = queue.queue_depth_by_model()
        assert depths["qwen3:14b"] == 2
        assert depths["mistral-nemo:12b"] == 1


class TestQueueFull:
    def test_reject_when_full(self, small_config):
        q = AffinityQueue(small_config.scheduler)  # max_queue_size=4
        for _i in range(4):
            assert q.enqueue(make_request(model="tiny:1b")) is True
        assert q.enqueue(make_request(model="tiny:1b")) is False
        assert q.total_size == 4


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    def test_highest_priority_dequeued_first(self, queue):
        low = make_request(model="qwen3:14b", tier=PriorityTier.BACKGROUND)
        high = make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE)
        queue.enqueue(low)
        queue.enqueue(high)
        result = queue.dequeue_for_model("qwen3:14b")
        assert result.id == high.id

    def test_aging_promotes_old_requests(self, queue):
        # Old background request (45s old): 10 + 45*2 = 100
        old = make_request(
            model="qwen3:14b", tier=PriorityTier.BACKGROUND,
            submitted_at=time.time() - 45.0,
        )
        # Fresh interactive: 100 + 0*2 = 100
        fresh = make_request(
            model="qwen3:14b", tier=PriorityTier.INTERACTIVE,
        )
        queue.enqueue(old)
        queue.enqueue(fresh)
        result = queue.dequeue_for_model("qwen3:14b")
        # Old background should have aged to >= interactive
        assert result.id == old.id


# ---------------------------------------------------------------------------
# Model affinity (pick_next)
# ---------------------------------------------------------------------------

class TestModelAffinity:
    def test_affinity_prefers_current_model(self, queue):
        # Same priority tier, but current model gets affinity bonus
        other = make_request(model="mistral-nemo:12b", tier=PriorityTier.AGENT)
        current = make_request(model="qwen3:14b", tier=PriorityTier.AGENT)
        queue.enqueue(other)
        queue.enqueue(current)
        best = queue.pick_next(current_model="qwen3:14b")
        assert best.model == "qwen3:14b"

    def test_high_priority_overrides_affinity(self, queue):
        # Interactive for other model beats agent with affinity
        other = make_request(model="mistral-nemo:12b", tier=PriorityTier.INTERACTIVE)
        current = make_request(model="qwen3:14b", tier=PriorityTier.AGENT)
        queue.enqueue(other)
        queue.enqueue(current)
        best = queue.pick_next(current_model="qwen3:14b")
        # Interactive(100) > Agent(50) + affinity(10) = 60
        assert best.model == "mistral-nemo:12b"

    def test_no_current_model(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        best = queue.pick_next(current_model=None)
        assert best is not None
        assert best.model == "qwen3:14b"

    def test_pick_next_does_not_remove(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        queue.pick_next()
        assert queue.total_size == 1  # Still there


# ---------------------------------------------------------------------------
# Cancel and drain
# ---------------------------------------------------------------------------

class TestCancelDrain:
    def test_cancel_existing(self, queue):
        r = make_request(model="qwen3:14b")
        queue.enqueue(r)
        assert queue.cancel(r.id) is True
        assert queue.total_size == 0

    def test_cancel_nonexistent(self, queue):
        assert queue.cancel("doesnotexist") is False

    def test_drain_all(self, queue):
        for _ in range(5):
            queue.enqueue(make_request(model="qwen3:14b"))
        drained = queue.drain_all()
        assert len(drained) == 5
        assert queue.total_size == 0
        assert queue.is_empty is True

    def test_get_models_with_requests(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        queue.enqueue(make_request(model="mistral-nemo:12b"))
        models = queue.get_models_with_requests()
        assert set(models) == {"qwen3:14b", "mistral-nemo:12b"}

    def test_model_queue_size(self, queue):
        queue.enqueue(make_request(model="qwen3:14b"))
        queue.enqueue(make_request(model="qwen3:14b"))
        assert queue.model_queue_size("qwen3:14b") == 2
        assert queue.model_queue_size("nonexistent") == 0
