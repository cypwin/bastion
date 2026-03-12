"""Model-affinity priority queue with aging.

The core scheduling data structure. Groups requests by model, applies
priority aging to prevent starvation, and provides model-affinity bonuses
to minimize GPU model swaps.

Design rationale (from GPU crash investigation):
  - NVIDIA GPUs can crash after ~60 rapid model load/unload cycles
  - Model affinity reduces swaps from O(N*M) to O(M) where M = unique models
  - Priority aging ensures even background batch jobs eventually get served
"""

from __future__ import annotations

import heapq
import logging
import time
import threading
from collections import defaultdict
from typing import Dict, List, Optional

from bastion.models import BrokerConfig, PriorityTier, QueuedRequest, SchedulerConfig

logger = logging.getLogger(__name__)


class AffinityQueue:
    """Model-affinity priority queue with aging.

    Requests are grouped by model. Within each model group, they are
    ordered by effective priority (base_priority + age * aging_rate).

    Parameters
    ----------
    config : SchedulerConfig
        Scheduling parameters (cooldown, aging_rate, affinity_bonus, max_queue_size).
    """

    def __init__(self, config: SchedulerConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model_queues: Dict[str, List[QueuedRequest]] = defaultdict(list)
        self._total_size = 0

    @property
    def total_size(self) -> int:
        """Total number of queued requests across all models."""
        return self._total_size

    @property
    def is_empty(self) -> bool:
        return self._total_size == 0

    def queue_depth_by_model(self) -> Dict[str, int]:
        """Return queue depth per model."""
        with self._lock:
            return {model: len(q) for model, q in self._model_queues.items() if q}

    def enqueue(self, request: QueuedRequest) -> bool:
        """Add a request to the queue.

        Returns False if the queue is full (503 scenario).
        """
        with self._lock:
            if self._total_size >= self.config.max_queue_size:
                logger.warning(
                    "Queue full (%d/%d) — rejecting request for model '%s'",
                    self._total_size, self.config.max_queue_size, request.model,
                )
                return False

            self._model_queues[request.model].append(request)
            self._total_size += 1

            logger.debug(
                "Enqueued request %s for model '%s' (priority=%.1f, queue_depth=%d)",
                request.id, request.model, request.base_priority,
                len(self._model_queues[request.model]),
            )
            return True

    def dequeue_for_model(self, model: str) -> Optional[QueuedRequest]:
        """Dequeue the highest-priority request for a specific model.

        Used when the scheduler decides to drain the current model's queue.
        """
        with self._lock:
            queue = self._model_queues.get(model, [])
            if not queue:
                return None

            # Sort by effective priority (descending) and pick the best
            best_idx = 0
            best_priority = queue[0].effective_priority(self.config.aging_rate)
            for i, req in enumerate(queue[1:], 1):
                p = req.effective_priority(self.config.aging_rate)
                if p > best_priority:
                    best_priority = p
                    best_idx = i

            request = queue.pop(best_idx)
            self._total_size -= 1

            # Clean up empty model queues
            if not queue:
                del self._model_queues[model]

            return request

    def pick_next(
        self,
        current_model: Optional[str] = None,
    ) -> Optional[QueuedRequest]:
        """Pick the best next request considering model affinity.

        If current_model is loaded, requests for that model get an affinity
        bonus to prefer draining the current queue before swapping.

        Returns the request but does NOT remove it from the queue.
        Use dequeue_for_model() after deciding to process it.
        """
        with self._lock:
            if self._total_size == 0:
                return None

            best: Optional[QueuedRequest] = None
            best_priority = -1.0

            for model, queue in self._model_queues.items():
                if not queue:
                    continue

                # Affinity bonus for currently loaded model
                bonus = self.config.model_affinity_bonus if model == current_model else 0.0

                for req in queue:
                    p = req.effective_priority(self.config.aging_rate, bonus)
                    if p > best_priority:
                        best_priority = p
                        best = req

            return best

    def get_models_with_requests(self) -> List[str]:
        """List all models that have pending requests."""
        with self._lock:
            return [model for model, q in self._model_queues.items() if q]

    def model_queue_size(self, model: str) -> int:
        """Number of pending requests for a specific model."""
        with self._lock:
            return len(self._model_queues.get(model, []))

    def cancel(self, request_id: str) -> bool:
        """Cancel a queued request by ID. Returns True if found and removed."""
        with self._lock:
            for model, queue in self._model_queues.items():
                for i, req in enumerate(queue):
                    if req.id == request_id:
                        queue.pop(i)
                        self._total_size -= 1
                        if not queue:
                            del self._model_queues[model]
                        logger.info("Cancelled request %s for model '%s'", request_id, model)
                        return True
            return False

    def sweep_stale(self, max_age_seconds: float) -> List[QueuedRequest]:
        """Remove and return all requests older than max_age_seconds."""
        now = time.time()
        swept: List[QueuedRequest] = []
        with self._lock:
            for model in list(self._model_queues.keys()):
                queue = self._model_queues[model]
                remaining: List[QueuedRequest] = []
                for req in queue:
                    if (now - req.submitted_at) > max_age_seconds:
                        swept.append(req)
                        self._total_size -= 1
                    else:
                        remaining.append(req)
                if remaining:
                    self._model_queues[model] = remaining
                else:
                    del self._model_queues[model]
        if swept:
            logger.warning(
                "Swept %d stale requests (max_age=%.0fs)",
                len(swept), max_age_seconds,
            )
        return swept

    def drain_all(self) -> List[QueuedRequest]:
        """Remove and return all queued requests (for shutdown)."""
        with self._lock:
            all_requests = []
            for queue in self._model_queues.values():
                all_requests.extend(queue)
            self._model_queues.clear()
            self._total_size = 0
            return all_requests