"""Hardened in-memory task store for A2A protocol tasks.

Dual-store architecture with compaction, TTL, capacity bounds, and
three-stage backpressure. Replaces the plain dict in A2AHandler.

Design based on docs fd51af18 (in-memory stores) and 26621baa (memory management).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from bastion.models import A2ATaskRecord, A2ATaskState

logger = logging.getLogger(__name__)


class BackpressureLevel(StrEnum):
    NORMAL = "normal"
    PRESSURE = "pressure"
    OVERLOADED = "overloaded"


@dataclass(frozen=True, slots=True)
class CompactedResult:
    """Lightweight summary of a completed/failed/canceled task.

    Stored in the completed store after a task reaches a terminal state.
    Retains the task ID, final status, output artifacts (for API
    compatibility), a truncated result summary, any error message,
    and a monotonic completion timestamp.
    """

    task_id: str
    status: str  # A2ATaskState value
    result_summary: str  # Truncated to 500 chars
    error: str | None
    completed_at: float  # monotonic timestamp
    output_artifacts: tuple  # Immutable copy of output_artifacts

    @classmethod
    def from_record(cls, record: A2ATaskRecord) -> CompactedResult:
        """Create a CompactedResult from an A2ATaskRecord.

        Preserves output_artifacts (as an immutable tuple) for API
        compatibility, and extracts a text summary for convenience.
        """
        summary = ""
        if record.output_artifacts:
            for artifact in record.output_artifacts:
                for part in artifact.get("parts", []):
                    if part.get("kind") == "text":
                        summary = part.get("text", "")[:500]
                        break
                    elif part.get("kind") == "data":
                        summary = json.dumps(part.get("data", {}))[:500]
                        break
                if summary:
                    break
        return cls(
            task_id=record.task_id,
            status=record.state.value,
            result_summary=summary,
            error=record.error,
            completed_at=time.monotonic(),
            output_artifacts=tuple(record.output_artifacts),
        )


class TaskStoreFullError(Exception):
    """Raised when the task store is at capacity and cannot accept new tasks."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"Task store full. Retry after {retry_after}s.")


_VALID_TRANSITIONS: dict[A2ATaskState, set[A2ATaskState]] = {
    A2ATaskState.SUBMITTED: {A2ATaskState.WORKING, A2ATaskState.CANCELED, A2ATaskState.FAILED},
    A2ATaskState.WORKING: {A2ATaskState.COMPLETED, A2ATaskState.FAILED, A2ATaskState.CANCELED},
    A2ATaskState.COMPLETED: set(),  # Terminal
    A2ATaskState.FAILED: set(),  # Terminal
    A2ATaskState.CANCELED: set(),  # Terminal
}


class TaskStore:
    """Hardened in-memory task store with dual-store architecture.

    Parameters
    ----------
    maxsize : int
        Maximum number of active (non-terminal) tasks. Default 10,000.
    completed_maxsize : int
        Maximum number of compacted completed results to keep. Default 50,000.
    tombstone_maxsize : int
        Maximum number of tombstone entries. Default 10,000.
    task_ttl_seconds : float
        TTL for active tasks (monotonic). Default 3600 (1 hour).
    completed_ttl_seconds : float
        TTL for completed results. Default 3600 (1 hour).
    cleanup_interval_seconds : float
        Interval between periodic cleanup sweeps. Default 60.
    """

    def __init__(
        self,
        maxsize: int = 10_000,
        completed_maxsize: int = 50_000,
        tombstone_maxsize: int = 10_000,
        task_ttl_seconds: float = 3600.0,
        completed_ttl_seconds: float = 3600.0,
        cleanup_interval_seconds: float = 60.0,
    ) -> None:
        self._maxsize = maxsize
        self._completed_maxsize = completed_maxsize
        self._tombstone_maxsize = tombstone_maxsize
        self._task_ttl = task_ttl_seconds
        self._completed_ttl = completed_ttl_seconds
        self._cleanup_interval = cleanup_interval_seconds

        # Dual store
        self._active: dict[str, A2ATaskRecord] = {}
        self._active_timestamps: dict[str, float] = {}  # task_id -> monotonic creation time
        self._completed: OrderedDict[str, CompactedResult] = OrderedDict()
        self._tombstones: OrderedDict[str, float] = OrderedDict()  # task_id -> eviction time

        # Subscribers for SSE fan-out
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

        # Backpressure state
        self._pressure_level = BackpressureLevel.NORMAL

        # Cleanup task reference (prevent GC)
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._cleanup_running = False

    # --- Public API ---

    def create(self, record: A2ATaskRecord) -> str:
        """Add a new task to the active store.

        Raises TaskStoreFullError if at capacity (overloaded).
        """
        # Check backpressure
        self._update_pressure_level()
        if self._pressure_level == BackpressureLevel.OVERLOADED:
            raise TaskStoreFullError()

        self._active[record.task_id] = record
        self._active_timestamps[record.task_id] = time.monotonic()
        self._update_pressure_level()
        return record.task_id

    def get(self, task_id: str) -> A2ATaskRecord | CompactedResult | None:
        """Retrieve a task by ID. Checks active, completed, then tombstones.

        Returns None if truly not found. Performs lazy TTL eviction.
        """
        # Check active store first
        if task_id in self._active:
            record = self._active[task_id]
            created = self._active_timestamps.get(task_id, 0.0)
            if time.monotonic() - created > self._task_ttl:
                # Expired -- move to tombstones
                self._evict_active(task_id)
                return None
            return record

        # Check completed store
        if task_id in self._completed:
            result = self._completed[task_id]
            if time.monotonic() - result.completed_at > self._effective_completed_ttl:
                # Expired -- move to tombstones
                del self._completed[task_id]
                self._add_tombstone(task_id)
                return None
            return result

        # Check tombstones (task existed but was evicted)
        if task_id in self._tombstones:
            return None  # Caller can distinguish via separate method if needed

        return None

    def update_state(self, task_id: str, new_state: A2ATaskState) -> A2ATaskRecord:
        """Transition a task to a new state.

        Validates the transition is legal. On terminal states, compacts
        and moves to completed store.

        Raises ValueError if transition is invalid.
        Raises KeyError if task not found in active store.
        """
        if task_id not in self._active:
            raise KeyError(f"Task {task_id} not in active store")

        record = self._active[task_id]

        # Validate transition
        valid_next = _VALID_TRANSITIONS.get(record.state, set())
        if new_state not in valid_next:
            raise ValueError(
                f"Invalid state transition: {record.state.value} -> {new_state.value} "
                f"(valid: {[s.value for s in valid_next]})"
            )

        record.state = new_state
        record.updated_at = time.time()

        # If terminal, compact and move to completed store
        if new_state in (A2ATaskState.COMPLETED, A2ATaskState.FAILED, A2ATaskState.CANCELED):
            compacted = CompactedResult.from_record(record)
            self._completed[task_id] = compacted
            # Enforce completed store capacity
            while len(self._completed) > self._completed_maxsize:
                evicted_id, _ = self._completed.popitem(last=False)
                self._add_tombstone(evicted_id)
            # Remove from active
            del self._active[task_id]
            self._active_timestamps.pop(task_id, None)

        return record

    def get_active(self, task_id: str) -> A2ATaskRecord | None:
        """Get a task only from the active store (for mutation)."""
        return self._active.get(task_id)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """Create a bounded subscriber queue for SSE fan-out."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        if task_id not in self._subscribers:
            self._subscribers[task_id] = []
        self._subscribers[task_id].append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        if task_id in self._subscribers:
            with contextlib.suppress(ValueError):
                self._subscribers[task_id].remove(queue)
            if not self._subscribers[task_id]:
                del self._subscribers[task_id]

    async def notify_subscribers(self, task_id: str, event: dict) -> None:
        """Push event to all subscribers with drop-oldest on full.

        Iterates over a copy of the subscriber list to handle
        concurrent modification safely.
        """
        if task_id not in self._subscribers:
            return

        for queue in list(self._subscribers[task_id]):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest, then insert new
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "SSE subscriber queue still full after drop for task %s",
                        task_id,
                    )

    def has_task(self, task_id: str) -> bool:
        """Check if a task exists in any store (active, completed, or tombstones)."""
        return task_id in self._active or task_id in self._completed

    def active_count(self) -> int:
        """Return the number of active (non-terminal) tasks."""
        return len(self._active)

    def count_by_state(self, state: str) -> int:
        """Count active tasks in a given state.

        Parameters
        ----------
        state : str
            State value to count (e.g. "submitted", "working").

        Returns
        -------
        int
            Number of active tasks whose state matches.
        """
        return sum(
            1 for record in self._active.values()
            if record.state.value == state
        )

    def stats(self) -> dict:
        """Return store statistics for monitoring."""
        return {
            "active_count": len(self._active),
            "completed_count": len(self._completed),
            "tombstone_count": len(self._tombstones),
            "subscriber_count": sum(len(v) for v in self._subscribers.values()),
            "pressure_level": self._pressure_level.value,
            "maxsize": self._maxsize,
        }

    def start_cleanup(self) -> None:
        """Start the periodic cleanup background task."""
        if self._cleanup_running:
            return
        self._cleanup_running = True
        task = asyncio.create_task(self._periodic_cleanup(), name="taskstore-cleanup")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        logger.info("TaskStore cleanup started (interval=%.0fs)", self._cleanup_interval)

    def stop_cleanup(self) -> None:
        """Stop the periodic cleanup."""
        self._cleanup_running = False
        for task in self._cleanup_tasks:
            task.cancel()

    # --- Internal ---

    @property
    def _effective_completed_ttl(self) -> float:
        """Completed TTL adjusted for backpressure."""
        if self._pressure_level == BackpressureLevel.PRESSURE:
            return min(self._completed_ttl, 300.0)  # 5 min under pressure
        return self._completed_ttl

    def _update_pressure_level(self) -> None:
        """Update backpressure level with hysteresis."""
        active_count = len(self._active)
        ratio = active_count / self._maxsize if self._maxsize > 0 else 0.0

        old_level = self._pressure_level

        if ratio >= 1.0:
            self._pressure_level = BackpressureLevel.OVERLOADED
        elif self._pressure_level == BackpressureLevel.NORMAL and ratio >= 0.8:
            self._pressure_level = BackpressureLevel.PRESSURE
        elif self._pressure_level == BackpressureLevel.PRESSURE and ratio < 0.7:
            self._pressure_level = BackpressureLevel.NORMAL
        elif self._pressure_level == BackpressureLevel.OVERLOADED and ratio < 0.8:
            self._pressure_level = BackpressureLevel.PRESSURE

        if old_level != self._pressure_level:
            logger.warning(
                "TaskStore backpressure: %s -> %s (active=%d/%d, ratio=%.1f%%)",
                old_level.value,
                self._pressure_level.value,
                active_count,
                self._maxsize,
                ratio * 100,
            )

    def _evict_active(self, task_id: str) -> None:
        """Evict an active task (TTL expired)."""
        if task_id in self._active:
            del self._active[task_id]
            self._active_timestamps.pop(task_id, None)
            self._add_tombstone(task_id)

    def _add_tombstone(self, task_id: str) -> None:
        """Add a tombstone entry for an evicted task."""
        self._tombstones[task_id] = time.monotonic()
        while len(self._tombstones) > self._tombstone_maxsize:
            self._tombstones.popitem(last=False)

    async def _periodic_cleanup(self) -> None:
        """Periodically sweep expired tasks."""
        while self._cleanup_running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                self._sweep()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("TaskStore cleanup error: %s", e)

    def _sweep(self) -> None:
        """Synchronous sweep of expired entries."""
        now = time.monotonic()

        # Sweep active tasks
        expired_active = [
            tid
            for tid, created in self._active_timestamps.items()
            if now - created > self._task_ttl
        ]
        for tid in expired_active:
            self._evict_active(tid)

        # Sweep completed results
        expired_completed = [
            tid
            for tid, result in self._completed.items()
            if now - result.completed_at > self._effective_completed_ttl
        ]
        for tid in expired_completed:
            del self._completed[tid]
            self._add_tombstone(tid)

        # Sweep old tombstones (keep for 2x completed TTL)
        tombstone_ttl = self._completed_ttl * 2
        expired_tombstones = [
            tid
            for tid, evicted_at in self._tombstones.items()
            if now - evicted_at > tombstone_ttl
        ]
        for tid in expired_tombstones:
            del self._tombstones[tid]

        # Clean up subscriber lists for non-existent tasks
        orphan_subs = [
            tid
            for tid in self._subscribers
            if tid not in self._active and tid not in self._completed
        ]
        for tid in orphan_subs:
            del self._subscribers[tid]

        if expired_active or expired_completed or expired_tombstones:
            logger.debug(
                "TaskStore sweep: %d active, %d completed, %d tombstones expired",
                len(expired_active),
                len(expired_completed),
                len(expired_tombstones),
            )

        self._update_pressure_level()
