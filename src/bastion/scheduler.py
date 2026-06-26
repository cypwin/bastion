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
from typing import Any

from bastion import audit
from bastion.health import check_gpu_safe, query_gpu_status  # noqa: F401
from bastion.metrics import (
    record_cooldown_wait,
    record_model_swap,
    record_model_swap_duration,
    record_queue_wait,
    set_concurrent_requests_active,
    update_swap_rate_per_min,
)
from bastion.models import BrokerConfig, QueuedRequest
from bastion.queue import AffinityQueue
from bastion.swapbrake import SwapBrake
from bastion.vram import (
    VRAM_STATE_UNKNOWN_REASON,
    VRAMManager,
    VRAMTracker,
    registry_lookup,
)
from bastion.watchdog import notify_watchdog

logger = logging.getLogger(__name__)

# S4 — queued-work tiering ceilings (scheduler-local; distinct from the proxy's
# queue_timeout_seconds 504 bound). Conservative floors for an unknown card.
_SWAP_STARVATION_CEILING_SECONDS = 60.0  # a starved swap earns the next freed slot
_BRAKE_BACKLOG_CEILING = 256             # shed swap attempts past this under a long brake


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
        queue: AffinityQueue | Any,  # accepts PersistentQueue wrapper at runtime
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

        # F1/F2 — sensor-independent swap-velocity circuit breaker. Counts
        # BASTION's OWN residency transitions on a monotonic clock, so it stays
        # armed when every nvidia-smi / /api/ps sensor is dark.
        self._brake = SwapBrake(config.scheduler.swap_brake, clock=time.monotonic)
        # THE single chokepoint for residency-increasing loads. Must exist on
        # every branch (R2-1): reuse the VRAMManager semaphore when present, else
        # a private one — so the no-VRAMManager path is braked too and the brake
        # can never be bypassed on an uncalibrated / non-NVIDIA host.
        self._load_serializer: asyncio.Semaphore = (
            vram_manager._load_semaphore if vram_manager is not None else asyncio.Semaphore(1)
        )

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

        # T3.2: per-model consecutive eviction-stuck counter.  Increments each
        # time _evict_for_model returns False for a given candidate; cleared on
        # success.  Used to suppress the ~10/sec "Cannot load X after evicting
        # 0 models" log spam when all resident models are in-flight (system
        # genuinely stuck — operator should know once, not every tick).
        self._eviction_stuck_streak: dict[str, int] = {}

        # F4 — behavioral evict↔reload oscillation detector (PRIMARY infeasible
        # signal; version-independent). Fed when _unload_model succeeds and the
        # model REAPPEARS resident on a later tick — the fingerprint of an
        # externally pinned working set BASTION keeps fighting (last-writer-wins
        # against the caller's keep_alive=-1). Promotes _eviction_stuck_streak.
        self._evict_reload_history: dict[str, deque[float]] = {}
        self._recently_unloaded: dict[str, float] = {}  # model -> monotonic unload time

        # S4 — priority-aging snapshot captured on the CLOSED→engaged edge so the
        # single swap granted at release does NOT load a background model that only
        # age-inflated past a foreground one DURING the brake. None ⇒ not engaged.
        self._engage_ranking: dict[str, float] | None = None
        # S4 — backlog ceiling: shed swap attempts past a bound under a long brake.
        self._brake_backlog_count: int = 0
        self._brake_backlog_ceiling: int = _BRAKE_BACKLOG_CEILING
        # S4 — swap-starvation ceiling (distinct from queue_timeout): a swap-needing
        # request that starves behind ungated Phase-1 traffic eventually earns the
        # next freed in-flight slot to evict for it.
        self._swap_starve_since: dict[str, float] = {}
        self._swap_starvation_ceiling: float = _SWAP_STARVATION_CEILING_SECONDS

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

    @property
    def swap_brake(self) -> SwapBrake:
        """The swap-velocity circuit breaker (for /broker/status + admin override)."""
        return self._brake

    @property
    def load_serializer(self) -> asyncio.Semaphore:
        """THE residency-increasing-load serialization point.

        Every direct-load path (scheduler swap, /broker/preload) MUST hold this
        and run the brake's authoritative ``acquire()``+``record_load()`` inside
        it — see the funnel regression test (REG).
        """
        return self._load_serializer

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
        # Swap-timing clock is MONOTONIC (F1): a wall-clock backward NTP step /
        # suspend-resume would otherwise read the trailing window as ~0 swaps and
        # silently disarm the rate throttle. Stall-DISPLAY stamps stay wall-clock.
        now = time.monotonic()
        window = self.config.scheduler.swap_rate_window_seconds

        # Prune timestamps outside the window
        while self._swap_timestamps and (now - self._swap_timestamps[0]) > window:
            self._swap_timestamps.popleft()

        rate = len(self._swap_timestamps)
        # Vision C gauge: surface the live per-minute swap rate so a storm is
        # visible forming, independent of whether the brake has engaged.
        update_swap_rate_per_min(float(rate))
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
        self._brake.set_drain(True)  # hold brake state — drain-induced zero rate != "storm over"
        self._wake_event.set()
        logger.info("Scheduler entering drain mode (queue depth: %d)", self.queue.total_size)

    async def resume(self) -> None:
        """Exit drain mode and resume normal scheduling."""
        self._draining = False
        self._brake.set_drain(False)
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

        # State unknown (Ollama unreachable). Reconcile above was a no-op so
        # the ledger is preserved. Bail out of this tick rather than make
        # dispatch decisions on missing residency data; scheduler retries
        # on the next 100ms loop.
        if resident_models is None:
            if self._last_stall_reason != "tracker_state_unknown":
                logger.info(
                    "Scheduler tick skipped: VRAM tracker state unknown "
                    "(Ollama /api/ps unreachable); ledger preserved",
                )
                self._last_stall_reason = "tracker_state_unknown"
                self._last_stall_time = time.time()
            return False

        # F4 — clear any infeasible latch on a real residency delta (each tick;
        # never on a pure time advance), feed the behavioral evict↔reload detector,
        # and maintain the S4 priority-aging snapshot across brake engage/release.
        if self._brake is not None:
            self._brake.clear_on_residency_delta(resident_models)
        self._detect_evict_reload_oscillation(resident_models)
        self._update_brake_engage_snapshot()

        dispatch_delay = self.config.scheduler.concurrent_dispatch_delay_seconds

        while current_inflight < max_concurrent:
            # Stagger concurrent dispatches to reduce GPU power transients
            # (large cold-load power swings stress VRMs; a short delay staggers ramp-up)
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
                    self._last_swap_time = time.monotonic()
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
            candidate = await self._select_swap_candidate(resident_models)
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

        # _process_tick bails before invoking this when state is unknown,
        # but guard defensively in case future callers reuse this helper.
        if resident_models is None:
            resident_models = set()

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
                    elapsed = max(0.0, time.monotonic() - self._last_swap_time)
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
                elapsed = max(0.0, time.monotonic() - self._last_swap_time)
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

    # ── F4 / S4 — pin-aware infeasible detection + queued-work tiering ──────

    def _detect_evict_reload_oscillation(self, resident_models: set[str]) -> None:
        """Feed the behavioral evict↔reload detector (F4 PRIMARY signal).

        A model BASTION unloaded that REAPPEARS resident is a same-model
        oscillation — the version-independent fingerprint of an externally pinned
        working set BASTION is fighting. Expired watches/history are pruned to the
        configured window so a one-off churn never accumulates into a false latch.
        """
        now = time.monotonic()
        window = self.config.scheduler.swap_brake.infeasible_window_seconds
        for model, t in list(self._recently_unloaded.items()):
            if model in resident_models:
                self._evict_reload_history.setdefault(model, deque()).append(now)
                self._recently_unloaded.pop(model, None)
            elif (now - t) > window:
                self._recently_unloaded.pop(model, None)
        for model, hist in list(self._evict_reload_history.items()):
            while hist and (now - hist[0]) > window:
                hist.popleft()
            if not hist:
                self._evict_reload_history.pop(model, None)

    def _pinned_resident(self, resident_loaded: list) -> list:
        """Resident LoadedModels that are externally pinned (caller keep_alive)."""
        return [m for m in resident_loaded if m.name in self.vram._pinned]

    def _candidate_vram_gb(self, model: str) -> float:
        info = registry_lookup(self.config.models, model)
        return info.vram_gb if info else self.config.gpu.default_vram_estimate_gb

    def _pinned_overflow(self, candidate_model: str, resident_loaded: list) -> bool:
        """True when the pinned resident set + candidate overruns the VRAM budget.

        This is the set-level "would require evicting a pinned model" condition: a
        candidate that cannot fit alongside the pinned set provably demands evicting
        one of those caller pins — which BASTION refuses to do (it sheds instead).
        """
        if candidate_model in self.vram._pinned:
            return False
        pinned = self._pinned_resident(resident_loaded)
        if not pinned:
            return False
        budget = self.config.gpu.max_vram_gb
        if budget <= 0:
            return False
        pinned_vram = sum(m.vram_gb for m in pinned)
        return (pinned_vram + self._candidate_vram_gb(candidate_model)) > budget

    def _pinned_oscillation_count(self, resident_loaded: list) -> int:
        now = time.monotonic()
        window = self.config.scheduler.swap_brake.infeasible_window_seconds
        total = 0
        for m in self._pinned_resident(resident_loaded):
            hist = self._evict_reload_history.get(m.name)
            if hist:
                while hist and (now - hist[0]) > window:
                    hist.popleft()
                total += len(hist)
        return total

    def _maybe_latch_infeasible(self, candidate: QueuedRequest, resident_loaded: list) -> bool:
        """Latch the CANDIDATE (never the pinned victim) when its load would require
        evicting an externally pinned model, OR the pinned set's evict↔reload
        oscillation count crosses the behavioral threshold (set-level freeze)."""
        if not self.config.scheduler.pin_detection.enabled:
            return False
        if not self._pinned_resident(resident_loaded):
            return False
        threshold = self.config.scheduler.swap_brake.infeasible_evict_reload_threshold
        overflow = self._pinned_overflow(candidate.model, resident_loaded)
        behavioral = self._pinned_oscillation_count(resident_loaded) >= threshold
        if overflow or behavioral:
            self._brake.note_infeasible(candidate.model)
            return True
        return False

    def _is_feasible_candidate(self, model: str, resident_loaded: list) -> bool:
        """S4 HALF_OPEN feasible-probe filter: skip latched / pinned-evicting candidates."""
        if self._brake.is_latched(model):
            return False
        return not self._pinned_overflow(model, resident_loaded)

    def _peek_request_for_model(self, model: str) -> QueuedRequest | None:
        """Highest-priority queued request for a model WITHOUT dequeuing (read-only).

        Mirrors AffinityQueue.dequeue_for_model's selection without mutating the
        queue, so the S4 release-probe can present a specific feasible model's
        request to the swap path (which re-dequeues authoritatively downstream).
        """
        queues = getattr(self.queue, "_model_queues", None)
        if not queues:
            return None
        q = queues.get(model)
        if not q:
            return None
        now = time.time()
        return max(q, key=lambda r: r.effective_priority(self.config.scheduler.aging_rate, now=now))

    def _best_priority_for_model(self, model: str, now: float) -> float:
        req = self._peek_request_for_model(model)
        if req is None:
            return 0.0
        return req.effective_priority(self.config.scheduler.aging_rate, now=now)

    def _update_brake_engage_snapshot(self) -> None:
        """S4 — snapshot the priority-aging baseline on the CLOSED→engaged edge.

        At release exactly one swap is granted; without this snapshot a low-base
        background request that merely aged DURING the brake could outrank the
        foreground request and reload a stale model, re-triggering the storm. The
        engage transition is observed via ``brake.snapshot()['state']``.
        """
        state = self._brake.snapshot()["state"]
        engaged = state in ("open", "half_open")
        if engaged and self._engage_ranking is None:
            now = time.time()
            self._engage_ranking = {
                m: self._best_priority_for_model(m, now)
                for m in self.queue.get_models_with_requests()
            }
        elif not engaged and self._engage_ranking is not None:
            self._engage_ranking = None
            self._brake_backlog_count = 0

    async def _select_swap_candidate(self, resident_models: set[str]) -> QueuedRequest | None:
        """Pick the swap target. While the brake is engaged, prefer a feasible,
        non-latched model ranked by the ENGAGE-time priority snapshot (S4) instead
        of the age-inflated live ranking, never spending the probe evicting a pin."""
        base = self.queue.pick_next(self._current_model)
        if base is None or self._engage_ranking is None:
            return base
        resident_loaded = await self.vram.get_loaded_models()
        if resident_loaded is None:
            return base
        nonres = [m for m in self.queue.get_models_with_requests() if m not in resident_models]
        feasible = [m for m in nonres if self._is_feasible_candidate(m, resident_loaded)]
        if not feasible:
            return base
        feasible.sort(key=lambda m: self._engage_ranking.get(m, 0.0), reverse=True)
        chosen = feasible[0]
        if base.model == chosen:
            return base
        return self._peek_request_for_model(chosen) or base

    def _brake_backlog_exceeded(self) -> bool:
        """S4 backlog ceiling — under a long brake, shed rather than accumulate."""
        return self._brake_backlog_count > self._brake_backlog_ceiling

    def _note_swap_starvation(self, model: str) -> None:
        self._swap_starve_since.setdefault(model, time.monotonic())

    def _clear_swap_starvation(self, model: str) -> None:
        self._swap_starve_since.pop(model, None)

    def _swap_starved(self, model: str) -> bool:
        """S4 — has a swap-needing model starved past the ceiling behind Phase-1 traffic?"""
        t = self._swap_starve_since.get(model)
        return t is not None and (time.monotonic() - t) >= self._swap_starvation_ceiling

    async def _handle_swap_dispatch(self, candidate: QueuedRequest) -> bool:
        """Handle dispatch for a non-resident model that requires a swap.

        This is the serialized swap path extracted from _process_tick.
        When a VRAMManager is available, uses the assume/confirm/forget pattern
        to atomically reserve VRAM before loading, eliminating TOCTOU races.
        """
        # Advisory swap-rate telemetry — the SwapBrake now owns go/no-go (S2).
        # _get_swap_cooldown still publishes the swap_rate gauge + audit level.
        self._get_swap_cooldown()

        # --- Brake PRE-GATE (cheap, pure peek; early-reject BEFORE any eviction
        # so a doomed swap never evicts a model). The AUTHORITATIVE acquire runs
        # inside the load serializer below. ---
        pre = self._brake.peek(candidate.model)
        if pre.action != "proceed":
            # While the brake holds, drain the current (already-resident) model's
            # queue instead of swapping — co-resident dispatch causes no inrush.
            current_depth = self.queue.model_queue_size(self._current_model)
            if (
                pre.action == "stall"
                and current_depth > 0
                and not self._has_inflight_fn(self._current_model)
            ):
                return await self._dispatch_for_model(self._current_model, needs_swap=False)
            if pre.action == "stall":
                record_cooldown_wait()
                # S4 — clock starvation: a swap-needing request held off behind
                # ungated Phase-1 traffic must eventually earn a freed slot.
                self._note_swap_starvation(candidate.model)
            logger.debug(
                "Swap brake %s for '%s' (pre-gate): %s",
                pre.action, candidate.model, pre.reason,
            )
            return False

        # F4 — set-level infeasible detection BEFORE any reservation / eviction:
        # if satisfying this candidate would require evicting an externally pinned
        # model (pinned set overruns budget OR the pinned set is oscillating), LATCH
        # THE CANDIDATE (not the pinned victim) and surface it — the request stays
        # queued, the proxy 503s, and next tick the brake sheds. We must never issue
        # keep_alive=0 against a caller's pin (the actual storm-stopper).
        resident_loaded = await self.vram.get_loaded_models()
        if resident_loaded is not None and self._maybe_latch_infeasible(candidate, resident_loaded):
            logger.warning(
                "Infeasible swap latched: '%s' would evict an externally pinned "
                "model (pinned set overruns the %.1f GB budget); refusing eviction",
                candidate.model, self.config.gpu.max_vram_gb,
            )
            return False

        # S4 — backlog ceiling: under a sustained brake, shed swap attempts past a
        # bound rather than accumulate stale, age-inflated work.
        if self._engage_ranking is not None:
            self._brake_backlog_count += 1
            if self._brake_backlog_exceeded():
                record_cooldown_wait()
                logger.debug("Swap backlog ceiling hit for '%s' — shedding", candidate.model)
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

        # Re-check GPU health immediately before the swap. The top-of-tick
        # gate ran many awaits ago (cooldown wait, eviction, VRAM
        # reservation); a GPU that transitioned hot in that window must
        # abort here — loading a model onto a hot GPU is exactly the
        # crash cycle BASTION exists to prevent.
        gpu_safe, gpu_reason = await check_gpu_safe(self.config.gpu)
        if not gpu_safe:
            logger.warning("Swap aborted — GPU unsafe at dispatch time: %s", gpu_reason)
            if self.vram_manager is not None and reservation is not None:
                await self.vram_manager.release(reservation)
            return False

        # Perform the swap. The load serializer is THE single chokepoint; the
        # brake's AUTHORITATIVE acquire()+record_load() run inside it on every
        # branch (R2-1). This both closes the peek→load TOCTOU (only one task
        # holds the serializer; the first record_load advances the brake and a
        # racing second task re-checks and stalls) AND keeps the no-VRAMManager
        # path braked. We never sleep inside the serializer on a stall —
        # min-spacing is realized by the scheduler's tick retries.
        logger.info(
            "Model swap: %s -> %s (queue depth for new: %d)",
            self._current_model, candidate.model,
            self.queue.model_queue_size(candidate.model),
        )
        from_model = self._current_model or "none"

        async with self._load_serializer:
            # S5/R1-2 — forward the F5 hardware-gate-blind signal to the sensor-
            # independent brake at the chokepoint, so refill HALVES exactly when
            # nvidia-smi goes dark; recovery (blind False) restores full refill.
            # (vram.py never imports swapbrake — this scheduler forward is the only
            # cross-layer link.)
            if self.vram_manager is not None:
                self._brake.set_hw_degraded(self.vram_manager.hardware_gate_blind)
            auth = self._brake.acquire(candidate.model)
            if auth.action != "proceed":
                # Lost the race / brake opened or latched in the await window —
                # back out cleanly WITHOUT recording the swap (a stalled swap
                # must not inflate _total_swaps or the swap-rate window).
                if self.vram_manager is not None and reservation is not None:
                    await self.vram_manager.release(reservation)
                logger.debug(
                    "Swap brake %s for '%s' (authoritative): %s",
                    auth.action, candidate.model, auth.reason,
                )
                return False

            # Committed to the swap — record the transition accounting now.
            loaded_before = await self.vram.get_loaded_vram_gb()
            audit.emit(audit.EVENT_SWAP, {
                "from_model": from_model,
                "to_model": candidate.model,
                "queue_depth": self.queue.model_queue_size(candidate.model),
                "vram_before_gb": round(loaded_before, 2),
            })
            # Vision C schema-frozen metric: bastion_model_swap_total
            # reason="scheduler_pick"; the eviction path uses reason="eviction".
            record_model_swap(
                from_model=self._current_model,
                to_model=candidate.model,
                reason="scheduler_pick",
            )
            self._current_model = candidate.model
            self._last_swap_time = time.monotonic()
            self._swap_timestamps.append(self._last_swap_time)
            self._total_swaps += 1

            # Proactive eviction: keep resident count <= max_loaded_models. Skip
            # when tracker state is unknown (cannot decide without ground truth).
            max_loaded = self.config.scheduler.ollama_max_loaded_models
            resident_after = await self.vram.get_loaded_models()
            if resident_after is not None and len(resident_after) > max_loaded:
                excess = [
                    m for m in resident_after
                    if m.name != candidate.model
                    # F4 — never proactively evict an externally pinned model.
                    and m.name not in self.vram._pinned
                    and not (
                        (info := registry_lookup(self.config.models, m.name))
                        and info.always_allowed
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

            swap_start = time.monotonic()
            try:
                result = await self._dispatch_for_model(candidate.model, needs_swap=True)
                record_model_swap_duration(candidate.model, time.monotonic() - swap_start)
                if result:
                    # Debit the brake token at the true GPU-I/O point.
                    self._brake.record_load(candidate.model)
                    # S4 — the swap was granted: this model is no longer starving.
                    self._clear_swap_starvation(candidate.model)
                    if self.vram_manager is not None and reservation is not None:
                        await self.vram_manager.commit(reservation)
                else:
                    if self.vram_manager is not None and reservation is not None:
                        await self.vram_manager.release(reservation)
                return result
            except Exception:
                if self.vram_manager is not None and reservation is not None:
                    await self.vram_manager.release(reservation)
                raise

    async def _evict_for_model(self, candidate: QueuedRequest) -> bool:
        """Evict resident models to make VRAM space for a candidate model.

        Queries all resident models, evicts strategically (preferring models with
        no queued requests, then smallest VRAM first), and waits for VRAM
        convergence after each eviction if VRAMManager is available.

        Returns True if enough VRAM was freed, False otherwise.
        """
        resident = await self.vram.get_loaded_models()
        if resident is None:
            logger.warning(
                "_evict_for_model: tracker state unknown for '%s' — refusing to "
                "evict on unknown state; caller will retry",
                candidate.model,
            )
            return False
        evictable = [
            m for m in resident
            if m.name != candidate.model
            # F4 — never evict an externally pinned model (caller keep_alive=-1);
            # fighting the pin with keep_alive=0 is exactly the storm BASTION must
            # stop. The candidate is latched INFEASIBLE upstream instead.
            and m.name not in self.vram._pinned
            # Tag-aware: an always_allowed model resident as 'name:latest'
            # must not become evictable because the registry key is untagged.
            and not (
                (info := registry_lookup(self.config.models, m.name))
                and info.always_allowed
            )
            and not (self._reservation_check_fn and self._reservation_check_fn(m.name))
            and not self._has_inflight_fn(m.name)  # Never evict in-flight models
        ]
        # Sort: prefer evicting models with no queued requests, then smallest VRAM first
        evictable.sort(key=lambda m: (self.queue.model_queue_size(m.name), m.vram_gb))

        evicted_count = 0
        for model_to_evict in evictable:
            if not await self._unload_model(model_to_evict.name):
                # Failed/deferred unload — no VRAM freed, so skip the
                # convergence wait and the can_load_model retry. Try the
                # next candidate. Avoids counting a no-op as eviction
                # progress (KNOWN_ISSUES, resolved in v0.4.1).
                continue
            evicted_count += 1

            # Wait for VRAM to stabilize after unload
            if self.vram_manager is not None:
                await self.vram_manager.wait_for_vram_convergence()
            else:
                await asyncio.sleep(0.5)  # Brief pause for VRAM to free

            can_load, vram_reason = await self.vram.can_load_model(candidate.model)
            if can_load:
                self._eviction_stuck_streak.pop(candidate.model, None)
                return True
            if vram_reason == VRAM_STATE_UNKNOWN_REASON:
                # Tracker state became unknown mid-eviction: further unloads
                # cannot make can_load_model pass and would tear down
                # residents pointlessly during an Ollama transition. Stop;
                # the caller retries when state is known again.
                logger.warning(
                    "_evict_for_model: tracker state unknown mid-eviction for "
                    "'%s' (after %d eviction(s)) — stopping eviction loop",
                    candidate.model, evicted_count,
                )
                return False

        # T3.2: suppress per-tick spam.  Log loudly the first time, then a
        # heartbeat every ~10s (100 ticks at 0.1s loop_interval) while stuck;
        # cleared on success above so the next genuine failure logs again.
        streak = self._eviction_stuck_streak.get(candidate.model, 0) + 1
        self._eviction_stuck_streak[candidate.model] = streak
        if streak == 1:
            logger.error(
                "Cannot load '%s' after evicting %d models "
                "(entering stuck streak; will suppress until cleared)",
                candidate.model, evicted_count,
            )
        elif streak % 100 == 0:
            logger.warning(
                "Still cannot load '%s' after %d consecutive eviction attempts; "
                "all resident models appear to be in-flight or reserved",
                candidate.model, streak,
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

    async def _unload_model(self, model: str) -> bool:
        """Unload a model from VRAM with logging.

        Checks for active A2A reservations and in-flight requests before unloading.
        Releases VRAMManager allocation so the ledger stays in sync with reality.

        Returns
        -------
        bool
            True only when VRAM was actually freed (Ollama confirmed unload
            and the ledger was updated). False for deferred evictions (active
            reservation, in-flight request) and for unload failures. Callers
            in the eviction loop must check this so a failed unload is not
            counted as progress — see KNOWN_ISSUES (resolved in v0.4.1).
        """
        # Check if model has an active reservation (A2A integration)
        if self._reservation_check_fn and self._reservation_check_fn(model):
            logger.info("Deferring eviction of '%s' — active A2A reservation", model)
            return False

        # Check if model has in-flight inference requests
        if self._has_inflight_fn(model):
            logger.info("Deferring eviction of '%s' — in-flight inference request", model)
            return False

        logger.info("Unloading model '%s' to free VRAM", model)
        success = await self.vram.unload_model(model)
        if success:
            # Two-token accounting: a BASTION-initiated unload is a real residency
            # transition / power event (count_evictions). External (Ollama-timeout)
            # unloads are NOT recorded here — only ones BASTION drove.
            self._brake.record_unload(model)
            # F4 — watch for a same-model REAPPEARANCE next tick (evict↔reload
            # oscillation = an externally pinned working set BASTION is fighting).
            self._recently_unloaded[model] = time.monotonic()
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
            return True
        logger.warning("Failed to unload model '%s'", model)
        return False

    async def unload_model_admin(self, model: str) -> tuple[str, dict]:
        """Operator-driven unload with safety checks and ledger release.

        Mirrors the safety logic in :meth:`_unload_model` but returns a
        discriminated outcome so the admin API can map it to honest HTTP
        statuses (409 for in-use, 200 for confirmed unload, 500 for tracker
        failure).  Also releases the VRAMManager allocation on success so the
        ledger stays consistent with reality (the private path missed this for
        user-driven unloads pre-2026-05-19).

        Returns
        -------
        tuple[str, dict]
            ``(status, details)`` where status is one of:
            ``"unloaded"``    - confirmed gone from /api/ps; VRAMManager released.
            ``"reserved"``    - active A2A reservation; unload deferred.
            ``"inflight"``    - in-flight inference request; unload deferred.
            ``"failed"``      - vram tracker reported failure or could not confirm
                                the model left /api/ps within the timeout.
        """
        if self._reservation_check_fn and self._reservation_check_fn(model):
            return ("reserved", {"model": model, "reason": "active A2A reservation"})
        if self._has_inflight_fn(model):
            return ("inflight", {"model": model, "reason": "in-flight inference request"})

        confirmed = await self.vram.unload_model(model)
        if not confirmed:
            return (
                "failed",
                {
                    "model": model,
                    "reason": (
                        "Ollama did not confirm unload within the configured timeout; "
                        "the model may still be resident"
                    ),
                },
            )

        record_model_swap(from_model=model, to_model="_none", reason="eviction")
        # F4 — pair operator force-unload with "refuse this model's next loads" so
        # the keep_alive=0 force isn't instantly re-pinned by a caller's
        # last-writer-wins keep_alive=-1.
        self._brake.note_infeasible(model)
        if self._current_model == model:
            self._current_model = None
        if self.vram_manager is not None:
            await self.vram_manager.release_model(model)
            await self.vram_manager.wait_for_vram_convergence()
        return ("unloaded", {"model": model})

    async def _sync_current_model(self) -> None:
        """Detect which model is currently loaded in Ollama.

        Called on startup to sync scheduler state with Ollama's actual state.
        S3: With residency-aware scheduling, we pick the largest loaded model
        as the initial "current" model for affinity bonus purposes. The actual
        residency tracking is handled by VRAMTracker's residency cache.
        """
        # Seed the brake "just swapped" so a post-restart first swap is SPACED,
        # not free: a watchdog bounce after a hard-lock otherwise leaves
        # _last_swap_time=0.0 (= infinite elapsed) while the caller's keep_alive
        # pins survive in Ollama — handing the first swap a straight shot back
        # into the crash zone (the crash-restart-loop hole).
        self._brake.seed_just_swapped()
        try:
            loaded = await self.vram.get_loaded_models()
            if loaded:
                # Pick the largest loaded model as initial "current" for affinity
                largest = max(loaded, key=lambda m: m.vram_gb)
                self._current_model = largest.name
                # Set swap time baseline so first non-resident swap respects cooldown
                self._last_swap_time = time.monotonic()
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
