"""VRAM tracking — fuses nvidia-smi ground truth with Ollama /api/ps state.

VRAM tracking via Ollama /api/ps and nvidia-smi fusion. The key insight
from the crash investigation: you MUST check both nvidia-smi (hardware truth)
and Ollama /api/ps (model state) because they can disagree — Ollama may
auto-unload models that nvidia-smi still reports as allocated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from bastion import audit
from bastion.health import get_vram_free_gb, query_gpu_status
from bastion.metrics import update_vram_used_mb
from bastion.models import BrokerConfig, LoadedModel, ModelInfo

logger = logging.getLogger(__name__)

HARDWARE_MARGIN_GB = 2.0  # nvidia-smi free-VRAM safety margin (KV/compute/fragmentation)

# Sentinel reason returned by can_load_model() when residency state is
# unknown. Callers (scheduler eviction loop) compare against this to stop
# retry loops that cannot succeed until Ollama is reachable again.
VRAM_STATE_UNKNOWN_REASON = (
    "Cannot determine VRAM state: Ollama /api/ps unreachable. "
    "Refusing to admit load while broker/Ollama is in transition."
)


async def _hardware_admits(
    vram_bytes: int, margin_gb: float = HARDWARE_MARGIN_GB
) -> tuple[bool, float | None]:
    """Cross-check a prospective allocation against nvidia-smi free VRAM.

    Returns ``(admits, free_gb)``. Fails OPEN: when nvidia-smi gives no reading
    (``free_gb is None``) returns ``(True, None)`` so a transient nvidia-smi
    outage does not block loads — the logical ledger remains the gate.
    """
    free_gb = await get_vram_free_gb()
    if free_gb is None:
        # Operationally significant: the hardware gate is offline and only
        # the logical ledger protects the budget. Warn so an extended
        # nvidia-smi outage is visible in logs, not silent.
        logger.warning(
            "nvidia-smi backstop unavailable (no free-VRAM reading) — "
            "failing open, logical ledger is the only admission gate"
        )
        return True, None
    required_gb = vram_bytes / (1024 ** 3) + margin_gb
    return free_gb >= required_gb, free_gb


def registry_lookup(models: dict[str, ModelInfo], name: str) -> ModelInfo | None:
    """Resolve an Ollama ``/api/ps`` model name against the broker registry.

    ``/api/ps`` reports normalized names (``nomic-embed-text:latest``)
    while ``broker.yaml`` keys are often untagged (``nomic-embed-text``).
    Exact match wins; otherwise compare with the implicit ``:latest`` tag
    stripped from both sides. Returns the registry value or ``None``.
    """
    found = models.get(name)
    if found is not None:
        return found
    norm = name.removesuffix(":latest")
    for key, info in models.items():
        if key.removesuffix(":latest") == norm:
            return info
    return None


class ResidencyCache:
    """TTL cache for loaded model queries to avoid hammering /api/ps.

    Wraps VRAMTracker.get_loaded_models() with a short-lived cache (default 1s).
    Provides fast residency checks for the scheduler to skip cooldowns when
    switching between co-resident models.

    Parameters
    ----------
    vram_tracker : VRAMTracker
        The tracker instance to query when cache expires.
    ttl_seconds : float
        Time-to-live for cached data (default: 1.0).
    """

    def __init__(
        self,
        vram_tracker: VRAMTracker,
        ttl_seconds: float = 1.0,
        max_stale_seconds: float = 30.0,
        declassify_after: int = 2,
    ) -> None:
        self._vram_tracker = vram_tracker
        self._ttl_seconds = ttl_seconds
        self._max_stale_seconds = max_stale_seconds
        self._declassify_after = declassify_after
        self._cache: list[LoadedModel] | None = None
        self._cache_timestamp: float = 0.0
        self._miss_counts: dict[str, int] = {}
        self._accept_next_verbatim = False
        self._lock = asyncio.Lock()

    async def _refresh_if_needed(self) -> bool:
        """Refresh cache if expired. Returns whether cache holds usable state.

        When the tracker returns ``None`` (Ollama /api/ps unreachable), the
        prior cache (if any) is preserved — a brief Ollama hiccup must not
        promote known-good residency data to "unknown". The next call retries
        immediately because the timestamp is not advanced.

        Returns
        -------
        bool
            True if the cache contains usable state (fresh or stale-OK).
            False only when we have never had a successful read AND the
            current attempt failed — caller should treat as unknown.
        """
        now = time.time()
        if self._cache is None or (now - self._cache_timestamp) > self._ttl_seconds:
            fresh = await self._vram_tracker.get_loaded_models()
            if fresh is None:
                # Preserve prior cache; do not advance timestamp so the next
                # call retries instead of serving stale-and-stale forever.
                # Stale-OK is BOUNDED: past max_stale_seconds of consecutive
                # failures the cached picture is too old to gate VRAM
                # decisions and we surface "unknown" (fail-closed) instead.
                if (
                    self._cache is not None
                    and (now - self._cache_timestamp) > self._max_stale_seconds
                ):
                    logger.warning(
                        "Residency cache stale beyond %.0fs grace (tracker "
                        "unreachable) — reporting state unknown",
                        self._max_stale_seconds,
                    )
                    return False
                logger.debug("Residency cache refresh skipped: tracker state unknown")
                return self._cache is not None
            self._cache = self._merge_with_flicker_hold(fresh)
            self._cache_timestamp = now
            logger.debug("Residency cache refreshed: %d models loaded", len(self._cache))
        return True

    def _merge_with_flicker_hold(self, fresh: list[LoadedModel]) -> list[LoadedModel]:
        """Debounce residency *declassification* against /api/ps partial views.

        Under concurrent inference Ollama's ``/api/ps`` can omit a busy model
        from a response while it is still resident and serving (observed
        2026-06-12: council models missing from 1-2 consecutive 0.5s polls
        with warm sub-second answers throughout, and a production audit entry
        recording a swap from a model to itself). Treating a single missing
        read as an unload sends resident models down the scheduler's swap
        path — phantom ``total_model_swaps``, spurious 2s cooldowns that
        serialize concurrent dispatch, thrashing-detector noise, and
        reconcile() ledger churn.

        A model previously reported resident is therefore HELD until it is
        missing from ``declassify_after`` consecutive successful refreshes.
        Newly appearing models are accepted immediately. BASTION-initiated
        unloads bypass the hold: :meth:`invalidate` makes the next refresh
        authoritative (``declassify_after=1`` disables holding entirely).
        """
        if self._accept_next_verbatim:
            self._accept_next_verbatim = False
            self._miss_counts.clear()
            return fresh
        fresh_names = {m.name for m in fresh}
        held: list[LoadedModel] = []
        for prior in self._cache or []:
            if prior.name in fresh_names:
                continue
            misses = self._miss_counts.get(prior.name, 0) + 1
            if misses >= self._declassify_after:
                self._miss_counts.pop(prior.name, None)
                logger.info(
                    "Residency: '%s' missing from %d consecutive /api/ps "
                    "reads — declassified",
                    prior.name, misses,
                )
            else:
                self._miss_counts[prior.name] = misses
                held.append(prior)
                logger.debug(
                    "Residency: holding '%s' through /api/ps flicker "
                    "(miss %d/%d)",
                    prior.name, misses, self._declassify_after,
                )
        # A model that reappeared resets its miss streak.
        for name in list(self._miss_counts):
            if name in fresh_names:
                self._miss_counts.pop(name)
        return fresh + held

    async def get_resident_models(self) -> set[str] | None:
        """Get set of currently resident model names.

        Returns
        -------
        set[str]
            Names of loaded models (fresh or stale-OK from the cache).
        None
            Tracker state is unknown (Ollama unreachable and no prior cache).
            Callers gating VRAM decisions MUST treat this as fail-closed.
        """
        async with self._lock:
            known = await self._refresh_if_needed()
            if not known:
                return None
            return {m.name for m in (self._cache or [])}

    async def get_resident_loaded_models(self) -> list[LoadedModel] | None:
        """Return the cached :class:`LoadedModel` list (fresh or stale-OK).

        Reuses the same TTL cache as :meth:`get_resident_models`, so no extra
        ``/api/ps`` query is issued. Returns ``None`` when tracker state is
        unknown (Ollama unreachable and no prior cache) — callers must treat
        that as "no size information available".
        """
        async with self._lock:
            known = await self._refresh_if_needed()
            if not known:
                return None
            return list(self._cache or [])

    async def is_model_resident(self, model_name: str) -> bool:
        """Check if a specific model is currently loaded in VRAM.

        Parameters
        ----------
        model_name : str
            Model name to check.

        Returns
        -------
        bool
            True if model is resident (according to cache), False otherwise.
        """
        resident = await self.get_resident_models()
        if resident is None:
            return False  # state unknown — safer to treat as not-resident
        return model_name in resident

    def invalidate(self) -> None:
        """Force cache expiry on next query.

        Call this after BASTION-initiated load/unload operations to ensure
        the scheduler sees the updated state immediately. The next refresh is
        taken verbatim — the flicker-hold debounce is bypassed so a genuine
        unload declassifies without waiting out the miss streak.
        """
        self._cache_timestamp = 0.0
        self._accept_next_verbatim = True
        logger.debug("Residency cache invalidated")


class VRAMTracker:
    """Tracks VRAM state from Ollama and nvidia-smi.

    Parameters
    ----------
    config : BrokerConfig
        Broker configuration with model VRAM sizes and GPU limits.
    """

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=config.ollama.api_timeout_seconds)
        # Initialize residency cache with configured TTL
        cache_ttl = getattr(config.scheduler, "residency_cache_ttl_seconds", 1.0)
        self.residency_cache = ResidencyCache(self, ttl_seconds=cache_ttl)

    async def get_loaded_models(self) -> list[LoadedModel] | None:
        """Query Ollama /api/ps for currently loaded models.

        Returns
        -------
        list[LoadedModel]
            Loaded models on a successful query (may be empty).
        None
            ``/api/ps`` was unreachable or returned an error. Callers that
            gate VRAM admission MUST treat this as "state unknown" and
            refuse to approve new loads — returning ``[]`` would be
            indistinguishable from "no models loaded" and could let the
            scheduler approve a load that exceeds the VRAM budget (the
            exact crash failure mode BASTION exists to prevent).
        """
        try:
            resp = await self._http.get(f"{self.config.ollama.base_url}/api/ps")
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("models", []):
                size_bytes = m.get("size", 0)
                name = m.get("name", "unknown")
                # Look up known VRAM, fall back to size-based estimate
                known = self.config.models.get(name)
                vram_gb = known.vram_gb if known else (size_bytes / (1024**3))
                models.append(LoadedModel(
                    name=name,
                    size_bytes=size_bytes,
                    vram_gb=vram_gb,
                    details=m.get("details", {}),
                ))
            return models
        except Exception as e:
            logger.warning("Failed to query Ollama /api/ps: %s", e)
            return None

    async def get_loaded_vram_gb(self) -> float:
        """Total VRAM used by currently loaded models (from Ollama).

        Emits audit alert if VRAM usage exceeds 85% threshold. Also publishes
        the Vision C ``bastion_vram_used_mb`` gauge (single-GPU label
        ``gpu_index="0"``), reusing the value already computed for the ledger
        so no extra GPU query is issued.

        When ``/api/ps`` is unreachable, returns 0.0 and skips alert + metric
        publication — emitting a "0 MB used" gauge during an Ollama outage
        would mislead operators into thinking VRAM is free.
        """
        models = await self.get_loaded_models()
        if models is None:
            logger.warning("get_loaded_vram_gb: tracker state unknown — skipping alert/metric")
            return 0.0
        total_vram = sum(m.vram_gb for m in models)

        # Vision C schema-frozen metric: bastion_vram_used_mb
        # Single-GPU deployments use gpu_index="0". Reuses the ledger total
        # (no new GPU queries). 1 GB = 1024 MB.
        update_vram_used_mb(gpu_index="0", mb=total_vram * 1024.0)

        # Check for VRAM threshold alerts
        vram_budget = self.config.gpu.max_vram_gb
        usage_pct = (total_vram / vram_budget) * 100 if vram_budget > 0 else 0

        if usage_pct > 85.0:
            severity = "critical" if usage_pct > 95.0 else "warning"
            audit.emit(audit.EVENT_VRAM_ALERT, {
                "severity": severity,
                "vram_used_gb": round(total_vram, 2),
                "vram_budget_gb": round(vram_budget, 2),
                "usage_percent": round(usage_pct, 1),
                "loaded_models": [m.name for m in models],
            })

        return total_vram

    async def can_load_model(self, model_name: str) -> tuple[bool, str]:
        """Check if a model can be loaded within VRAM budget.

        Considers both currently loaded models (Ollama) and hardware state
        (nvidia-smi temperature/power).

        Returns
        -------
        tuple[bool, str]
            (can_load, reason)
        """
        # Check if model is always-allowed (e.g., embeddings)
        known = self.config.models.get(model_name)
        if known and known.always_allowed:
            return True, "Always-allowed model"

        # Check GPU health
        gpu_status = await query_gpu_status()
        if (
            gpu_status.temperature_c
            and gpu_status.temperature_c > self.config.gpu.max_temperature_c
        ):
            return False, (
                f"GPU too hot: {gpu_status.temperature_c}\u00b0C"
                f" > {self.config.gpu.max_temperature_c}\u00b0C"
            )

        # Check VRAM budget. Fail-closed when Ollama /api/ps is unreachable —
        # approving a load on unknown state can exceed the budget and crash
        # the GPU (the failure mode BASTION exists to prevent).
        loaded = await self.get_loaded_models()
        if loaded is None:
            await self.log_vram_snapshot("hard_gate_blocked", {
                "model": model_name,
                "reason": "tracker state unknown",
            })
            return False, VRAM_STATE_UNKNOWN_REASON
        loaded_names = {m.name for m in loaded}

        # Already loaded? No additional VRAM needed
        if model_name in loaded_names:
            return True, "Model already loaded"

        # Compute proposed total VRAM (tag-aware: /api/ps may report
        # ':latest'-tagged names for untagged registry keys)
        loaded_vram = sum(
            m.vram_gb for m in loaded
            if not (
                (info := registry_lookup(self.config.models, m.name))
                and info.always_allowed
            )
        )
        model_vram = known.vram_gb if known else self._estimate_vram(model_name)
        proposed_total = loaded_vram + model_vram

        if proposed_total > self.config.gpu.max_vram_gb:
            return False, (
                f"Would exceed VRAM budget: {loaded_vram:.1f}GB loaded + "
                f"{model_vram:.1f}GB requested = {proposed_total:.1f}GB > "
                f"{self.config.gpu.max_vram_gb:.1f}GB limit"
            )

        # nvidia-smi hard gate: cross-check actual free VRAM (shared helper)
        hw_ok, free_gb = await _hardware_admits(int(model_vram * (1024 ** 3)))
        if not hw_ok:
            required_free = model_vram + HARDWARE_MARGIN_GB
            await self.log_vram_snapshot("hard_gate_blocked", {
                "model": model_name,
                # free_gb is non-None whenever hw_ok is False (fail-open
                # returns True on a missing reading); guard for the type.
                "free_gb": round(free_gb, 2) if free_gb is not None else None,
                "required_free_gb": round(required_free, 2),
            })
            return False, (
                f"nvidia-smi: only {free_gb:.1f}GB free, "
                f"need {required_free:.1f}GB ({model_vram:.1f}GB model "
                f"+ {HARDWARE_MARGIN_GB:.1f}GB margin)"
            )

        await self.log_vram_snapshot("model_load_approved", {
            "model": model_name,
            "proposed_total_gb": round(proposed_total, 2),
        })
        return True, f"OK (proposed total: {proposed_total:.1f}GB)"

    async def unload_model(self, model_name: str) -> bool:
        """Request Ollama to unload a model (keep_alive=0) and confirm removal.

        Sends the unload request, then polls /api/ps to confirm the model has
        actually been freed from VRAM before returning.  Ollama's keep_alive=0
        response is an acknowledgement only — actual VRAM release is async.

        This prevents the race condition where a subsequent preload sees stale
        /api/ps data and gets a 409 VRAM-budget rejection.
        """
        try:
            resp = await self._http.post(
                f"{self.config.ollama.base_url}/api/generate",
                json={"model": model_name, "keep_alive": 0},
                timeout=self.config.ollama.unload_timeout_seconds,
            )
            resp.raise_for_status()

            # Poll /api/ps until model disappears (confirms VRAM freed).
            # When state is unknown (None) we cannot confirm — keep polling
            # until either confirmation or timeout, then surface the failure.
            confirmed = False
            deadline = time.time() + self.config.ollama.unload_timeout_seconds
            while time.time() < deadline:
                loaded = await self.get_loaded_models()
                if loaded is not None and model_name not in {m.name for m in loaded}:
                    confirmed = True
                    break
                await asyncio.sleep(0.2)

            if confirmed:
                logger.info("Unloaded model '%s' from VRAM (confirmed)", model_name)
            else:
                logger.warning(
                    "Model '%s' still in /api/ps after %.0fs - Ollama will unload "
                    "it when current inference finishes; treating as not-yet-unloaded "
                    "so the caller does not get a false success",
                    model_name, self.config.ollama.unload_timeout_seconds,
                )

            # Invalidate cache so scheduler sees updated state immediately
            self.residency_cache.invalidate()
            await self.log_vram_snapshot("model_unload", {
                "model": model_name,
                "confirmed": confirmed,
            })
            return confirmed
        except Exception as e:
            logger.warning("Failed to unload model '%s': %s", model_name, e)
            return False

    async def log_vram_snapshot(self, event: str, extra: dict[str, Any] | None = None) -> None:
        """Write VRAM snapshot to the VRAM journal for crash forensics.

        The journal path is resolved by :func:`bastion.paths.vram_journal_path`
        (default ``~/.local/share/bastion/bastion-vram-journal.jsonl``).

        Parameters
        ----------
        event : str
            Event type (model_load, model_unload, hard_gate_blocked, dispatch, tick).
        extra : dict, optional
            Additional event-specific data.
        """
        try:
            from bastion.paths import vram_journal_path

            gpu = await query_gpu_status()
            loaded = await self.get_loaded_models()
            if loaded is None:
                # Preserve the journal entry so the outage is visible in
                # crash forensics rather than silently writing "no models".
                loaded_payload: list[dict[str, Any]] = []
                total_loaded_vram_gb: float | None = None
                tracker_state = "unknown"
            else:
                loaded_payload = [{"name": m.name, "vram_gb": m.vram_gb} for m in loaded]
                total_loaded_vram_gb = round(sum(m.vram_gb for m in loaded), 2)
                tracker_state = "ok"
            snapshot = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "gpu_vram_used_mb": gpu.vram_used_mb,
                "gpu_vram_free_mb": gpu.vram_free_mb,
                "gpu_temp_c": gpu.temperature_c,
                "loaded_models": loaded_payload,
                "total_loaded_vram_gb": total_loaded_vram_gb,
                "tracker_state": tracker_state,
            }
            if extra:
                snapshot.update(extra)
            journal = vram_journal_path()
            with journal.open("a") as f:
                f.write(json.dumps(snapshot) + "\n")
        except Exception as e:
            logger.debug("VRAM journal write failed: %s", e)

    def _estimate_vram(self, model_name: str) -> float:
        """Estimate VRAM for unknown models.

        Falls back to gpu.default_vram_estimate_gb from config (default 10 GB).
        """
        # Try fuzzy matching against known models
        for known_name, info in self.config.models.items():
            if known_name in model_name or model_name in known_name:
                return info.vram_gb
        default = self.config.gpu.default_vram_estimate_gb
        logger.warning("Unknown model '%s' — estimating %.1f GB VRAM", model_name, default)
        return default

    async def close(self) -> None:
        await self._http.aclose()


class VRAMReservation:
    """Represents a pending VRAM reservation."""

    def __init__(
        self, reservation_id: str, model: str, vram_bytes: int, ttl: float = 120.0,
    ) -> None:
        self.reservation_id = reservation_id
        self.model = model
        self.vram_bytes = vram_bytes
        self.created_at = time.monotonic()
        self.ttl = ttl
        self.committed = False

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > self.ttl


class VRAMManager:
    """VRAM ledger with assume/confirm/forget pattern.

    Eliminates TOCTOU races by atomically reserving VRAM before async model loading.

    Lifecycle:
    1. reserve(model, vram_bytes) -> VRAMReservation  (deducts from available pool)
    2. load model (async, protected by reservation + load semaphore)
    3. commit(reservation) -> None  (move from reserved to allocated)
    4. release(reservation) -> None  (return to available pool on failure/TTL)

    Parameters
    ----------
    vram_tracker : VRAMTracker
        Ground-truth VRAM querier.
    total_vram_bytes : int
        Total GPU VRAM in bytes.
    safety_margin_pct : float
        Percentage of total VRAM to keep as safety margin (default 10%).
    """

    def __init__(
        self,
        vram_tracker: VRAMTracker,
        total_vram_bytes: int,
        safety_margin_pct: float = 10.0,
    ) -> None:
        self._tracker = vram_tracker
        self._total = total_vram_bytes
        self._safety_margin = int(total_vram_bytes * safety_margin_pct / 100.0)

        self._allocated: int = 0      # Confirmed (model loaded successfully)
        self._reserved: int = 0       # Pending (loading in progress)
        self._reservations: dict[str, VRAMReservation] = {}
        self._model_allocations: dict[str, int] = {}  # Per-model committed bytes
        self._lock = asyncio.Lock()
        self._load_semaphore = asyncio.Semaphore(1)  # Serialize GPU I/O

        logger.info(
            "VRAMManager initialized: total=%dMB, safety_margin=%dMB (%.0f%%)",
            total_vram_bytes // (1024 * 1024),
            self._safety_margin // (1024 * 1024),
            safety_margin_pct,
        )

    @property
    def available_vram(self) -> int:
        """Available VRAM after allocations, reservations, and safety margin."""
        return max(0, self._total - self._safety_margin - self._allocated - self._reserved)

    @property
    def allocated_bytes(self) -> int:
        return self._allocated

    @property
    def reserved_bytes(self) -> int:
        return self._reserved

    async def reserve(self, model: str, vram_bytes: int, ttl: float = 120.0) -> VRAMReservation:
        """Atomically reserve VRAM for a pending model load.

        This is the critical section -- NO await points between check and deduction.
        The Lock is defense-in-depth (asyncio cooperative scheduling already makes
        synchronous code between await points atomic).

        The nvidia-smi hardware backstop is queried BEFORE the lock so the await
        never enters the atomic critical section; its verdict is evaluated inside
        the lock. It fails open (a missing reading trusts the logical ledger).

        Raises
        ------
        ValueError
            If insufficient VRAM (logical ledger) or nvidia-smi reports
            insufficient free VRAM (hardware backstop).
        """
        hw_ok, free_gb = await _hardware_admits(vram_bytes)
        async with self._lock:
            # Reclaim runs INSIDE the lock so reclaim+check+deduct
            # is atomic against concurrent reservers. Outside the
            # lock, two coroutines could both observe and free the
            # same expired reservation, double-decrementing
            # self._reserved and inflating available_vram beyond
            # the budget — potentially approving an over-budget
            # load (the exact crash failure mode BASTION exists to
            # prevent).
            self._reclaim_expired_sync()  # No awaits inside this method.

            if vram_bytes > self.available_vram:
                raise ValueError(
                    f"Insufficient VRAM: need {vram_bytes // (1024*1024)}MB, "
                    f"available {self.available_vram // (1024*1024)}MB "
                    f"(allocated={self._allocated // (1024*1024)}MB, "
                    f"reserved={self._reserved // (1024*1024)}MB)"
                )

            if not hw_ok:
                raise ValueError(
                    f"nvidia-smi backstop: only {free_gb:.1f}GB free, need "
                    f"{vram_bytes / (1024 ** 3):.1f}GB + {HARDWARE_MARGIN_GB:.1f}GB "
                    f"margin for model '{model}'"
                )

            reservation = VRAMReservation(
                reservation_id=uuid.uuid4().hex[:12],
                model=model,
                vram_bytes=vram_bytes,
                ttl=ttl,
            )
            self._reserved += vram_bytes
            self._reservations[reservation.reservation_id] = reservation

            logger.info(
                "VRAM reserved: %s model=%s bytes=%dMB (available=%dMB)",
                reservation.reservation_id, model,
                vram_bytes // (1024*1024), self.available_vram // (1024*1024),
            )
            return reservation

    async def commit(self, reservation: VRAMReservation) -> None:
        """Move reservation from pending to allocated (model load succeeded)."""
        async with self._lock:
            if reservation.reservation_id not in self._reservations:
                logger.warning("Commit for unknown reservation: %s", reservation.reservation_id)
                return
            self._reserved -= reservation.vram_bytes
            self._allocated += reservation.vram_bytes
            self._model_allocations[reservation.model] = (
                self._model_allocations.get(reservation.model, 0) + reservation.vram_bytes
            )
            reservation.committed = True
            del self._reservations[reservation.reservation_id]
            logger.info(
                "VRAM committed: %s model=%s bytes=%dMB",
                reservation.reservation_id, reservation.model,
                reservation.vram_bytes // (1024*1024),
            )

    async def release(self, reservation: VRAMReservation) -> None:
        """Release a reservation (model load failed or TTL expired)."""
        async with self._lock:
            if reservation.reservation_id not in self._reservations:
                if reservation.committed:
                    # Release committed allocation
                    self._allocated = max(0, self._allocated - reservation.vram_bytes)
                    # Update per-model tracking
                    model_alloc = self._model_allocations.get(reservation.model, 0)
                    model_alloc = max(0, model_alloc - reservation.vram_bytes)
                    if model_alloc > 0:
                        self._model_allocations[reservation.model] = model_alloc
                    else:
                        self._model_allocations.pop(reservation.model, None)
                    logger.info(
                        "VRAM deallocated: %s model=%s bytes=%dMB",
                        reservation.reservation_id, reservation.model,
                        reservation.vram_bytes // (1024*1024),
                    )
                return
            self._reserved -= reservation.vram_bytes
            del self._reservations[reservation.reservation_id]
            logger.info(
                "VRAM released: %s model=%s bytes=%dMB",
                reservation.reservation_id, reservation.model,
                reservation.vram_bytes // (1024*1024),
            )

    def _reclaim_expired_sync(self) -> None:
        """Reclaim expired reservations (synchronous -- no awaits!)."""
        expired = [
            r for r in self._reservations.values() if r.expired
        ]
        for r in expired:
            self._reserved -= r.vram_bytes
            del self._reservations[r.reservation_id]
            logger.warning(
                "VRAM reservation expired: %s model=%s bytes=%dMB (TTL=%.0fs)",
                r.reservation_id, r.model, r.vram_bytes // (1024*1024), r.ttl,
            )

    async def release_model(self, model_name: str) -> int:
        """Release all VRAM allocated to a model (e.g., after explicit unload).

        Parameters
        ----------
        model_name : str
            Name of the model to release.

        Returns
        -------
        int
            Bytes freed.
        """
        async with self._lock:
            freed = self._model_allocations.pop(model_name, 0)
            self._allocated = max(0, self._allocated - freed)
            if freed:
                logger.info(
                    "VRAM deallocated for model '%s': %dMB freed (allocated now %dMB)",
                    model_name, freed // (1024 * 1024),
                    self._allocated // (1024 * 1024),
                )
            return freed

    async def reconcile(self, loaded_model_names: set[str] | None) -> int:
        """Reconcile ledger with actual Ollama state (bidirectional).

        Removes allocations for models Ollama no longer reports (auto-unload,
        failed load) AND imports resident models that entered VRAM without a
        BASTION swap (loaded before startup, or by a direct Ollama client), so
        the ledger stops under-counting actual residency. Per-model sizes for
        import are read from the residency cache (no extra ``/api/ps`` query);
        ``loaded_model_names`` remains the authoritative residency set.

        Parameters
        ----------
        loaded_model_names : set[str] | None
            Model names currently reported by Ollama /api/ps. ``None`` means
            tracker state is unknown (transient backend failure); the ledger
            is left untouched to prevent wiping per-model allocations during
            a brief outage.

        Returns
        -------
        int
            Total bytes freed by stale-removal (``0`` when state is unknown).
            Imports are logged and audited but not included in this count.
        """
        if loaded_model_names is None:
            return 0
        self._reclaim_expired_sync()

        # Sizes for import — only fetched when there could be something to
        # import (an empty residency set can never import, so skip the lookup).
        sizes: dict[str, float] = {}
        if loaded_model_names:
            loaded_list = await self._tracker.residency_cache.get_resident_loaded_models()
            sizes = {m.name: m.vram_gb for m in (loaded_list or [])}

        async with self._lock:
            reserved_models = {r.model for r in self._reservations.values()}

            # Removal: drop allocations for models Ollama no longer reports.
            stale = [m for m in self._model_allocations if m not in loaded_model_names]
            freed_total = 0
            for model in stale:
                freed = self._model_allocations.pop(model)
                self._allocated = max(0, self._allocated - freed)
                freed_total += freed
                logger.warning(
                    "VRAM reconciliation: released stale allocation for '%s' "
                    "(%dMB) — model no longer in Ollama /api/ps",
                    model, freed // (1024 * 1024),
                )

            # Import: account for resident models not tracked via a BASTION swap.
            imported: list[str] = []
            imported_total = 0
            for name in loaded_model_names:
                if name in self._model_allocations:
                    continue  # already tracked
                if name in reserved_models:
                    continue  # mid-reservation — commit() will account for it
                # Tag-aware: /api/ps says 'nomic-embed-text:latest' while the
                # registry key is untagged — an exact .get() would miss the
                # always_allowed exclusion and import the model into the
                # budget permanently (removal can't fire: the name IS loaded).
                known = registry_lookup(self._tracker.config.models, name)
                if known and known.always_allowed:
                    continue  # excluded from budget accounting (design D2)
                vram_gb = sizes.get(name)
                if vram_gb is None:
                    continue  # no size info — cannot import responsibly
                import_bytes = int(vram_gb * (1024 ** 3))
                if import_bytes <= 0:
                    continue
                self._model_allocations[name] = import_bytes
                self._allocated += import_bytes
                imported.append(name)
                imported_total += import_bytes
                logger.warning(
                    "VRAM reconciliation: imported untracked resident '%s' "
                    "(%dMB) — present in Ollama /api/ps but not in ledger",
                    name, import_bytes // (1024 * 1024),
                )

            if freed_total:
                audit.emit("vram_reconciliation", {
                    "stale_models": stale,
                    "freed_bytes": freed_total,
                    "allocated_after": self._allocated,
                    "loaded_models": list(loaded_model_names),
                })
            if imported_total:
                audit.emit("vram_import", {
                    "imported_models": imported,
                    "imported_bytes": imported_total,
                    "allocated_after": self._allocated,
                    "loaded_models": list(loaded_model_names),
                })
            return freed_total

    async def wait_for_vram_convergence(self, timeout: float = 5.0, interval: float = 0.25) -> bool:
        """Poll VRAM until free memory stabilizes after unload.

        Ollama's scheduler doesn't free VRAM instantly. This polls nvidia-smi
        every ``interval`` seconds until the delta between consecutive reads is
        less than 1MB, or timeout is reached.

        Returns True if converged, False if timed out.
        """
        deadline = time.monotonic() + timeout
        last_free = await get_vram_free_gb()

        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            current_free = await get_vram_free_gb()
            if current_free is None or last_free is None:
                continue
            delta = abs(current_free - last_free)
            if delta < 0.001:  # < 1MB
                logger.debug("VRAM converged: %.2fGB free (delta=%.4fGB)", current_free, delta)
                return True
            last_free = current_free

        logger.warning("VRAM convergence timed out after %.1fs", timeout)
        return False

    def status(self) -> dict:
        """Return ledger status for monitoring."""
        self._reclaim_expired_sync()  # Clean up on every status poll
        return {
            "total_bytes": self._total,
            "safety_margin_bytes": self._safety_margin,
            "allocated_bytes": self._allocated,
            "reserved_bytes": self._reserved,
            "available_bytes": self.available_vram,
            "active_reservations": len(self._reservations),
            "model_allocations": {
                model: bytes_val
                for model, bytes_val in self._model_allocations.items()
            },
            "reservations": [
                {
                    "id": r.reservation_id,
                    "model": r.model,
                    "vram_bytes": r.vram_bytes,
                    "age_seconds": round(time.monotonic() - r.created_at, 1),
                    "committed": r.committed,
                }
                for r in self._reservations.values()
            ],
        }
