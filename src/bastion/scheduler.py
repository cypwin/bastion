"""Scheduling loop — picks requests from the affinity queue and manages model transitions.

The scheduler is the brain of BASTION. It runs as an asyncio background task,
continuously checking the queue for work and deciding:
  1. Which request to serve next (highest effective priority with affinity bonus)
  2. Whether a model swap is needed (and safe to perform)
  3. When to enforce cooldown between transitions

Design rationale (from GPU crash investigation):
  - NVIDIA GPUs can crash after ~60 rapid model load/unload cycles in ~7 minutes
  - Cooldown of 2s between swaps reduces cycle rate from ~25/min to ~20/min
  - Model affinity drains same-model requests before swapping, reducing total swaps
  - GPU health gating pauses scheduling when temperature/power are unsafe
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable

from bastion import audit
from bastion.health import check_gpu_safe, query_gpu_status  # noqa: F401
from bastion.metrics import (
    record_model_swap,
    record_queue_wait,
    set_concurrent_requests_active,
)
from bastion.models import BrokerConfig, QueuedRequest
from bastion.queue import AffinityQueue
from bastion.vram import VRAMManager, VRAMTracker
from bastion.watchdog import notify_watchdog

logger = logging.getLogger(__name__)


class Scheduler:
    """Background scheduling loop for the BASTION broker.

    Pulls requests from the AffinityQueue, manages model loading/unloading,
    enforces cooldowns, and dispatches requests to Ollama via a callback.

    Parameters
    ----------
    config : BrokerConfig
        Broker configuration.
    queue : AffinityQueue
        The priority queue to pull requests from.
    vram_tracker : VRAMTracker
        VRAM state tracker for load/unload decisions.
    dispatch_fn : callable
        Async callback to actually forward a request to Ollama.
        Signature: ``async def dispatch(request: QueuedRequest) -> None``
    reservation_check_fn : callable, optional
        Callback to check if a model has an active A2A reservation.
        Signature: ``def check(model: str) -> bool``
        If provided and returns True, model eviction is deferred.
    """

    def __init__(
        self,
        config: BrokerConfig,
        queue: AffinityQueue,
        vram_tracker: VRAMTracker,
        dispatch_fn,
        reservation_check_fn=None,
        has_inflight_fn=None,
        inflight_count_fn=None,
        vram_manager: VRAMManager | None = None,
    ) -> None:
        self.config = config
        self.queue = queue
        self.vram = vram_tracker
        self.vram_manager = vram_manager
        self._dispatch = dispatch_fn
        self._reservation_check_fn = reservation_check_fn
        self._has_inflight_fn = has_inflight_fn or (lambda model: False)
        self._inflight_count_fn = inflight_count_fn or (lambda: 0)

        self._current_model: str | None = None  # Last dispatched model (for affinity bonus)
        self._last_swap_time: float = 0.0
        self._total_swaps: int = 0
        self._total_dispatched: int = 0
        self._running: bool = False
        self._draining: bool = False
        self._task: asyncio.Task | None = None

        # Rolling window of swap timestamps for rate limiting
        self._swap_timestamps: deque[float] = deque()
        self._swap_rate_level: str = "normal"  # normal, warn, critical

        # Event to wake the scheduler when new requests arrive
        self._wake_event = asyncio.Event()

        # Dispatch error cleanup callback (set by server.py at startup)
        self._dispatch_error_fn: Callable[[str], None] | None = None

        # Stall diagnostics (Fix E)
        self._last_stall_reason: str = ""
        self._last_stall_time: float = 0.0

    @property
    def current_model(self) -> str | None:
        """Last dispatched model (used for affinity bonus and admin API).

        S3: This represents the last model we dispatched a request to, which is
        used for model affinity in queue prioritization. For actual VRAM residency,
        use VRAMTracker.is_model_resident() or VRAMTracker.get_resident_models().
        """
        return self._current_model

    @property
    def total_swaps(self) -> int:
        return self._total_swaps

    @property
    def total_dispatched(self) -> int:
        return self._total_dispatched

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_draining(self) -> bool:
        return self._draining

    def notify(self) -> None:
        """Wake the scheduler (call after enqueuing a request)."""
        self._wake_event.set()

    def _get_swap_cooldown(self) -> float:
        """Compute dynamic cooldown based on rolling swap rate.

        Counts swaps in the configured window and escalates cooldown
        when swap velocity approaches crash-inducing rates.

        Returns
        -------
        float
            Cooldown duration in seconds.
        """
        now = time.time()
        window = self.config.scheduler.swap_rate_window_seconds

        # Prune timestamps outside the window
        while self._swap_timestamps and (now - self._swap_timestamps[0]) > window:
            self._swap_timestamps.popleft()

        rate = len(self._swap_timestamps)
        cfg = self.config.scheduler

        if rate >= cfg.swap_rate_critical_threshold:
            new_level = "critical"
            cooldown = cfg.swap_rate_critical_cooldown_seconds
        elif rate >= cfg.swap_rate_warn_threshold:
            new_level = "warn"
            cooldown = cfg.swap_rate_warn_cooldown_seconds
        else:
            new_level = "normal"
            cooldown = cfg.cooldown_seconds

        # Log + audit on level transitions
        if new_level != self._swap_rate_level:
            logger.warning(
                "Swap rate level: %s -> %s (rate=%d/%ds, cooldown=%.1fs)",
                self._swap_rate_level, new_level, rate, int(window), cooldown,
            )
            audit.emit("swap_rate", {
                "level": new_level,
                "previous_level": self._swap_rate_level,
                "swaps_in_window": rate,
                "window_seconds": window,
                "cooldown_seconds": cooldown,
            })
            self._swap_rate_level = new_level

        return cooldown

    async def start(self) -> None:
        """Start the scheduling loop as a background task."""
        if self._running:
            return
        self._running = True
        self._draining = False

        # Sync current model state from Ollama on startup
        await self._sync_current_model()

        self._task = asyncio.create_task(self._loop(), name="bastion-scheduler")
        logger.info(
            "Scheduler started (cooldown=%.1fs, affinity_bonus=%.1f, aging_rate=%.1f)",
            self.config.scheduler.cooldown_seconds,
            self.config.scheduler.model_affinity_bonus,
            self.config.scheduler.aging_rate,
        )

    async def stop(self) -> None:
        """Stop the scheduling loop gracefully."""
        shutdown_timeout = self.config.scheduler.shutdown_timeout_seconds
        self._running = False
        self._wake_event.set()  # Unblock if waiting
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=shutdown_timeout)
            except TimeoutError:
                logger.warning("Scheduler did not stop within %.0fs, cancelling", shutdown_timeout)
                self._task.cancel()
            self._task = None

    async def drain(self) -> None:
        """Enter drain mode: finish current queue, reject new requests."""
        self._draining = True
        self._wake_event.set()
        logger.info("Scheduler entering drain mode (queue depth: %d)", self.queue.total_size)

    async def resume(self) -> None:
        """Exit drain mode and resume normal scheduling."""
        self._draining = False
        logger.info("Scheduler resumed from drain mode")

    # ── Main loop ──────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main scheduling loop. Runs until stop() is called."""
        while self._running:
            try:
                # Wait for work or periodic check
                loop_interval = self.config.scheduler.loop_interval_seconds
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=loop_interval)
                self._wake_event.clear()

                if not self._running:
                    break

                # Process as many requests as possible in this tick
                await self._process_tick()

                # If draining and queue is empty, stop
                if self._draining and self.queue.is_empty:
                    logger.info("Drain complete — queue empty")

                # Systemd watchdog heartbeat (no-op outside systemd)
                notify_watchdog()

            except Exception as e:
                logger.error("Scheduler loop error: %s", e, exc_info=True)
                await asyncio.sleep(self.config.scheduler.error_backoff_seconds)

    async def _process_tick(self) -> bool:
        """Process one scheduling tick — dispatches to multiple co-resident models.

        Concurrent dispatch rules:
        1. Different co-resident models → dispatch concurrently (parallel inference)
        2. Same model → serialize (OLLAMA_NUM_PARALLEL=1)
        3. Model swap needed → serialize (PCIe crash risk)
        4. Model with in-flight request → cannot be evicted
        5. Max concurrent dispatches → configurable cap (default 3)

        Returns True if any request was dispatched.
        """
        if self.queue.is_empty:
            return False

        # Check GPU health before any work
        gpu_safe, reason = await check_gpu_safe(self.config.gpu)
        if not gpu_safe:
            logger.warning("Scheduling paused — GPU unsafe: %s", reason)
            await asyncio.sleep(self.config.scheduler.gpu_unsafe_backoff_seconds)
            return False

        # Phase 1: Dispatch to co-resident models that have queued work
        # and no in-flight requests (concurrent, non-blocking)
        dispatched_any = False
        max_concurrent = self.config.scheduler.max_concurrent_dispatches
        current_inflight = self._inflight_count_fn()
        resident_models = await self.vram.residency_cache.get_resident_models()

        # Reconcile VRAMManager ledger with actual Ollama state.
        # Catches Ollama auto-unloads (keep_alive timeout) and failed loads
        # that left stale allocations blocking the VRAM budget.
        if self.vram_manager is not None:
            await self.vram_manager.reconcile(resident_models)

        dispatch_delay = self.config.scheduler.concurrent_dispatch_delay_seconds

        while current_inflight < max_concurrent:
            # Stagger concurrent dispatches to reduce GPU power transients
            # (460W→80W→460W spikes stress VRMs; 100ms delay staggers ramp-up)
            if dispatched_any and dispatch_delay > 0:
                await asyncio.sleep(dispatch_delay)

            # Re-query models with work each iteration (queue changes as we dispatch)
            models_with_work = self.queue.get_models_with_requests()
            if not models_with_work:
                break

            # Find next eligible co-resident model to dispatch to:
            # - Must be resident
            # - Must not have in-flight request (same-model serialization)
            # Prefer current model (affinity) to drain it first
            models_with_work.sort(
                key=lambda m: (
                    0 if m == self._current_model else 1,
                    -self.queue.model_queue_size(m),
                ),
            )

            dispatched_this_iteration = False
            for model in models_with_work:
                if model not in resident_models:
                    continue  # Not resident — needs swap, handled in phase 2

                if self._has_inflight_fn(model):
                    continue  # Same-model in-flight — serialize

                # Co-resident, no in-flight → dispatch (non-blocking)
                logger.debug("Co-resident dispatch: %s (non-blocking)", model)
                if self._current_model is None:
                    # First dispatch — set swap time baseline for cooldown tracking
                    self._last_swap_time = time.time()
                elif model != self._current_model:
                    logger.debug("Co-resident transition: %s -> %s, skipping cooldown",
                                self._current_model, model)
                self._current_model = model
                result = await self._dispatch_for_model(model, needs_swap=False)
                if result:
                    dispatched_any = True
                    current_inflight += 1
                    dispatched_this_iteration = True
                    break  # Re-evaluate from top (affinity model may still have work)

            if not dispatched_this_iteration:
                break  # No eligible co-resident models

        # Phase 2: If there's still room and a non-resident model needs dispatch,
        # handle the swap case (blocking, serialized)
        if current_inflight < max_concurrent and not self.queue.is_empty:
            candidate = self.queue.pick_next(self._current_model)
            if candidate is not None:
                is_resident = candidate.model in resident_models
                # Re-check residency from cache (may have changed)
                if not is_resident:
                    is_resident = await self.vram.residency_cache.is_model_resident(candidate.model)

                if is_resident:
                    # Resident but has in-flight (same-model serialization)
                    if self._has_inflight_fn(candidate.model):
                        pass  # Skip — will be dispatched when in-flight completes
                    else:
                        # Should have been caught in phase 1, but handle edge case
                        self._current_model = candidate.model
                        result = await self._dispatch_for_model(candidate.model, needs_swap=False)
                        if result:
                            dispatched_any = True
                else:
                    # Non-resident — needs model swap (blocking path)
                    dispatched_any = await self._handle_swap_dispatch(candidate) or dispatched_any

        # Stall diagnostics: when queue has work but nothing dispatched
        if not dispatched_any and not self.queue.is_empty:
            await self._diagnose_stall()
        elif dispatched_any and self._last_stall_reason:
            # Clear stall reason on successful dispatch
            self._last_stall_reason = ""

        # Vision C schema-frozen metric: bastion_concurrent_requests_active
        # Update after each dispatch decision so the Grafana gauge reflects
        # real-time in-flight counts without an extra polling loop.
        set_concurrent_requests_active(self._inflight_count_fn())

        return dispatched_any

    async def _diagnose_stall(self) -> None:
        """Determine why the scheduler can't dispatch and log the reason.

        Called at end of _process_tick when dispatched_any=False and queue is
        non-empty. Only logs when the reason changes to avoid spam.
        """
        max_concurrent = self.config.scheduler.max_concurrent_dispatches
        current_inflight = self._inflight_count_fn()
        resident_models = await self.vram.residency_cache.get_resident_models()
        models_with_work = self.queue.get_models_with_requests()

        reason = "unknown"
        detail = ""

        if current_inflight >= max_concurrent:
            reason = "at_max_concurrent"
            detail = f"inflight={current_inflight}/{max_concurrent}"

        elif all(self._has_inflight_fn(m) for m in models_with_work if m in resident_models):
            # Every resident model with queued work has in-flight request
            inflight_resident = [m for m in models_with_work if m in resident_models]
            if inflight_resident:
                reason = "all_models_inflight"
                detail = f"models={inflight_resident}"
            else:
                # No resident models have queued work -> all need swap
                non_resident = [m for m in models_with_work if m not in resident_models]
                if non_resident:
                    swap_cooldown = self._get_swap_cooldown()
                    elapsed = time.time() - self._last_swap_time
                    remaining = swap_cooldown - elapsed
                    if remaining > 0:
                        reason = "swap_cooldown"
                        detail = f"{remaining:.1f}s remaining"
                    else:
                        reason = "non_resident_models"
                        detail = f"models={non_resident}"

        else:
            # Some models need swap, check cooldown
            non_resident_with_work = [m for m in models_with_work if m not in resident_models]
            if non_resident_with_work:
                swap_cooldown = self._get_swap_cooldown()
                elapsed = time.time() - self._last_swap_time
                remaining = swap_cooldown - elapsed
                if remaining > 0:
                    reason = "swap_cooldown"
                    detail = f"{remaining:.1f}s remaining"
                else:
                    reason = "non_resident_models"
                    detail = f"models={non_resident_with_work}"

        # Only log on reason change to avoid spam
        if reason != self._last_stall_reason:
            self._last_stall_reason = reason
            self._last_stall_time = time.time()
            logger.info(
                "Scheduler stall: %s (%s) — queue_depth=%d",
                reason, detail, self.queue.total_size,
            )
            audit.emit("scheduler_stall", {
                "reason": reason,
                "detail": detail,
                "queue_depth": self.queue.total_size,
                "inflight": current_inflight,
            })

    @property
    def stall_reason(self) -> str:
        """Current stall reason (empty string if not stalled)."""
        return self._last_stall_reason

    @property
    def stall_time(self) -> float:
        """Timestamp when current stall reason was first detected."""
        return self._last_stall_time

    async def _handle_swap_dispatch(self, candidate: QueuedRequest) -> bool:
        """Handle dispatch for a non-resident model that requires a swap.

        This is the serialized swap path extracted from _process_tick.
        When a VRAMManager is available, uses the assume/confirm/forget pattern
        to atomically reserve VRAM before loading, eliminating TOCTOU races.
        """
        # Check cooldown (dynamic based on swap rate)
        swap_cooldown = self._get_swap_cooldown()
        elapsed = time.time() - self._last_swap_time
        remaining = swap_cooldown - elapsed
        if remaining > 0:
            # Can we serve a request for the current model instead?
            current_depth = self.queue.model_queue_size(self._current_model)
            if current_depth > 0 and not self._has_inflight_fn(self._current_model):
                # Drain current model's queue while waiting for cooldown
                return await self._dispatch_for_model(self._current_model, needs_swap=False)
            else:
                # Wait for cooldown
                logger.debug("Cooldown: %.1fs remaining before model swap", remaining)
                await asyncio.sleep(min(remaining, 0.5))
                return False

        # Log VRAM state before swap decision
        await self.vram.log_vram_snapshot("pre_swap", {
            "from_model": self._current_model or "none",
            "to_model": candidate.model,
            "queue_depth": self.queue.model_queue_size(candidate.model),
        })

        # --- VRAM reservation (assume/confirm/forget pattern) ---
        reservation = None
        if self.vram_manager is not None:
            # Look up model VRAM from config, fall back to default estimate
            known = self.config.models.get(candidate.model)
            vram_gb = known.vram_gb if known else self.config.gpu.default_vram_estimate_gb
            vram_bytes = int(vram_gb * 1024 * 1024 * 1024)

            try:
                reservation = await self.vram_manager.reserve(candidate.model, vram_bytes)
            except ValueError as exc:
                logger.warning(
                    "VRAM reservation failed for '%s': %s — trying to free VRAM",
                    candidate.model, exc,
                )
                # Evict models to make space, then retry reservation
                freed = await self._evict_for_model(candidate)
                if freed:
                    try:
                        reservation = await self.vram_manager.reserve(candidate.model, vram_bytes)
                    except ValueError as exc2:
                        logger.error(
                            "Cannot reserve VRAM for '%s' after eviction: %s",
                            candidate.model, exc2,
                        )
                        return False
                else:
                    return False
        else:
            # Fallback: original can_load_model check (no VRAMManager)
            can_load, vram_reason = await self.vram.can_load_model(candidate.model)
            if not can_load:
                logger.warning(
                    "Cannot load model '%s': %s — trying to free VRAM",
                    candidate.model, vram_reason,
                )
                freed = await self._evict_for_model(candidate)
                if not freed:
                    return False

        # Perform the swap (serialized through load semaphore if VRAMManager available)
        logger.info(
            "Model swap: %s -> %s (queue depth for new: %d)",
            self._current_model, candidate.model,
            self.queue.model_queue_size(candidate.model),
        )

        # Audit: model swap event
        from_model = self._current_model or "none"
        loaded_before = await self.vram.get_loaded_vram_gb()
        audit.emit(audit.EVENT_SWAP, {
            "from_model": from_model,
            "to_model": candidate.model,
            "queue_depth": self.queue.model_queue_size(candidate.model),
            "vram_before_gb": round(loaded_before, 2),
        })

        # Vision C schema-frozen metric: bastion_model_swap_total
        # reason="scheduler_pick" for the normal swap path (queue advanced to a
        # non-resident model). The eviction path uses reason="eviction" inside
        # _unload_model (see that helper).
        record_model_swap(
            from_model=self._current_model,
            to_model=candidate.model,
            reason="scheduler_pick",
        )

        self._current_model = candidate.model
        self._last_swap_time = time.time()
        self._swap_timestamps.append(self._last_swap_time)
        self._total_swaps += 1

        # Proactive eviction: if resident count exceeds max_loaded_models,
        # evict the least-useful excess model
        max_loaded = self.config.scheduler.ollama_max_loaded_models
        resident_after = await self.vram.get_loaded_models()
        if len(resident_after) > max_loaded:
            excess = [
                m for m in resident_after
                if m.name != candidate.model
                and not (
                    self.config.models.get(m.name)
                    and self.config.models[m.name].always_allowed
                )
                and not (
                    self._reservation_check_fn
                    and self._reservation_check_fn(m.name)
                )
                and not self._has_inflight_fn(m.name)
            ]
            excess.sort(key=lambda m: (self.queue.model_queue_size(m.name), m.vram_gb))
            evict_count = len(resident_after) - max_loaded
            for m in excess[:evict_count]:
                logger.info(
                    "Proactive eviction: unloading '%s' (resident=%d > max=%d)",
                    m.name, len(resident_after), max_loaded,
                )
                await self._unload_model(m.name)

        # Dispatch the request (blocking -- swap path)
        # Use load semaphore if VRAMManager is available to serialize GPU I/O
        if self.vram_manager is not None and reservation is not None:
            async with self.vram_manager._load_semaphore:
                try:
                    result = await self._dispatch_for_model(candidate.model, needs_swap=True)
                    if result:
                        await self.vram_manager.commit(reservation)
                    else:
                        await self.vram_manager.release(reservation)
                    return result
                except Exception:
                    await self.vram_manager.release(reservation)
                    raise
        else:
            return await self._dispatch_for_model(candidate.model, needs_swap=True)

    async def _evict_for_model(self, candidate: QueuedRequest) -> bool:
        """Evict resident models to make VRAM space for a candidate model.

        Queries all resident models, evicts strategically (preferring models with
        no queued requests, then smallest VRAM first), and waits for VRAM
        convergence after each eviction if VRAMManager is available.

        Returns True if enough VRAM was freed, False otherwise.
        """
        resident = await self.vram.get_loaded_models()
        evictable = [
            m for m in resident
            if m.name != candidate.model
            and not (self.config.models.get(m.name) and self.config.models[m.name].always_allowed)
            and not (self._reservation_check_fn and self._reservation_check_fn(m.name))
            and not self._has_inflight_fn(m.name)  # Never evict in-flight models
        ]
        # Sort: prefer evicting models with no queued requests, then smallest VRAM first
        evictable.sort(key=lambda m: (self.queue.model_queue_size(m.name), m.vram_gb))

        evicted_count = 0
        for model_to_evict in evictable:
            await self._unload_model(model_to_evict.name)
            evicted_count += 1

            # Wait for VRAM to stabilize after unload
            if self.vram_manager is not None:
                await self.vram_manager.wait_for_vram_convergence()
            else:
                await asyncio.sleep(0.5)  # Brief pause for VRAM to free

            can_load, vram_reason = await self.vram.can_load_model(candidate.model)
            if can_load:
                return True

        logger.error(
            "Cannot load '%s' after evicting %d models",
            candidate.model, evicted_count,
        )
        return False

    async def _dispatch_for_model(self, model: str, needs_swap: bool = True) -> bool:
        """Dequeue and dispatch the highest-priority request for a model.

        Parameters
        ----------
        model : str
            Model to dispatch for.
        needs_swap : bool
            Whether a model swap is needed. Passed through to _dispatch_request
            to determine blocking vs non-blocking dispatch.
        """
        request = self.queue.dequeue_for_model(model)
        if request is None:
            return False

        # Vision C schema-frozen metric: bastion_request_queue_wait_seconds
        # Record the time the request spent in the affinity queue. submitted_at
        # is the enqueue timestamp (Pydantic default at construction).
        wait_seconds = max(0.0, time.time() - request.submitted_at)
        record_queue_wait(
            model=request.model,
            priority=request.tier.value,
            wait_seconds=wait_seconds,
        )

        try:
            logger.debug(
                "Dispatching %s -> %s (age=%.1fs, priority=%.1f, blocking=%s)",
                request.id, request.model, request.age_seconds,
                request.effective_priority(self.config.scheduler.aging_rate),
                needs_swap,
            )
            await self._dispatch(request, needs_swap=needs_swap)
            self._total_dispatched += 1
            return True
        except Exception as e:
            logger.error("Dispatch failed for request %s: %s", request.id, e)
            # Clean up tracking state so request doesn't become a ghost
            if self._dispatch_error_fn:
                self._dispatch_error_fn(request.id)
            return False

    # ── Model management helpers ───────────────────────────────────

    async def _unload_model(self, model: str) -> None:
        """Unload a model from VRAM with logging.

        Checks for active A2A reservations and in-flight requests before unloading.
        Releases VRAMManager allocation so the ledger stays in sync with reality.
        """
        # Check if model has an active reservation (A2A integration)
        if self._reservation_check_fn and self._reservation_check_fn(model):
            logger.info("Deferring eviction of '%s' — active A2A reservation", model)
            return

        # Check if model has in-flight inference requests
        if self._has_inflight_fn(model):
            logger.info("Deferring eviction of '%s' — in-flight inference request", model)
            return

        logger.info("Unloading model '%s' to free VRAM", model)
        success = await self.vram.unload_model(model)
        if success:
            # Vision C: count the eviction as a model transition with
            # reason="eviction" (no to_model in the strict sense; we use
            # "_none" to signal the unloaded slot, mirroring the from_model
            # convention for idle-load).
            record_model_swap(
                from_model=model,
                to_model="_none",
                reason="eviction",
            )
            if self._current_model == model:
                self._current_model = None
            # Release VRAMManager allocation so the ledger reflects the unload
            if self.vram_manager is not None:
                await self.vram_manager.release_model(model)
                await self.vram_manager.wait_for_vram_convergence()
        else:
            logger.warning("Failed to unload model '%s'", model)

    async def _sync_current_model(self) -> None:
        """Detect which model is currently loaded in Ollama.

        Called on startup to sync scheduler state with Ollama's actual state.
        S3: With residency-aware scheduling, we pick the largest loaded model
        as the initial "current" model for affinity bonus purposes. The actual
        residency tracking is handled by VRAMTracker's residency cache.
        """
        try:
            loaded = await self.vram.get_loaded_models()
            if loaded:
                # Pick the largest loaded model as initial "current" for affinity
                largest = max(loaded, key=lambda m: m.vram_gb)
                self._current_model = largest.name
                # Set swap time baseline so first non-resident swap respects cooldown
                self._last_swap_time = time.time()
                logger.info(
                    "Synced with Ollama: %d models loaded, current='%s' (%.1f GB)",
                    len(loaded), largest.name, largest.vram_gb,
                )
            else:
                self._current_model = None
                logger.info("Synced with Ollama: no models loaded")
        except Exception as e:
            logger.warning("Failed to sync model state from Ollama: %s", e)
            self._current_model = None
