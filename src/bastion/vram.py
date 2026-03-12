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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from bastion import audit
from bastion.health import get_vram_free_gb, query_gpu_status
from bastion.models import BrokerConfig, LoadedModel

logger = logging.getLogger(__name__)


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

    def __init__(self, vram_tracker: "VRAMTracker", ttl_seconds: float = 1.0) -> None:
        self._vram_tracker = vram_tracker
        self._ttl_seconds = ttl_seconds
        self._cache: Optional[List[LoadedModel]] = None
        self._cache_timestamp: float = 0.0
        self._lock = asyncio.Lock()

    async def _refresh_if_needed(self) -> None:
        """Refresh cache if expired (TTL exceeded)."""
        now = time.time()
        if self._cache is None or (now - self._cache_timestamp) > self._ttl_seconds:
            self._cache = await self._vram_tracker.get_loaded_models()
            self._cache_timestamp = now
            logger.debug("Residency cache refreshed: %d models loaded", len(self._cache))

    async def get_resident_models(self) -> set[str]:
        """Get set of currently resident model names.

        Returns
        -------
        set[str]
            Names of all loaded models (from cache if fresh, otherwise refreshed).
        """
        async with self._lock:
            await self._refresh_if_needed()
            return {m.name for m in (self._cache or [])}

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
        return model_name in resident

    def invalidate(self) -> None:
        """Force cache expiry on next query.

        Call this after BASTION-initiated load/unload operations to ensure
        scheduler sees the updated state immediately.
        """
        self._cache_timestamp = 0.0
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

    async def get_loaded_models(self) -> List[LoadedModel]:
        """Query Ollama /api/ps for currently loaded models."""
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
            return []

    async def get_loaded_vram_gb(self) -> float:
        """Total VRAM used by currently loaded models (from Ollama).

        Emits audit alert if VRAM usage exceeds 85% threshold.
        """
        models = await self.get_loaded_models()
        total_vram = sum(m.vram_gb for m in models)

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
        if gpu_status.temperature_c and gpu_status.temperature_c > self.config.gpu.max_temperature_c:
            return False, f"GPU too hot: {gpu_status.temperature_c}°C > {self.config.gpu.max_temperature_c}°C"

        # Check VRAM budget
        loaded = await self.get_loaded_models()
        loaded_names = {m.name for m in loaded}

        # Already loaded? No additional VRAM needed
        if model_name in loaded_names:
            return True, "Model already loaded"

        # Compute proposed total VRAM
        loaded_vram = sum(
            m.vram_gb for m in loaded
            if not (self.config.models.get(m.name) and self.config.models[m.name].always_allowed)
        )
        model_vram = known.vram_gb if known else self._estimate_vram(model_name)
        proposed_total = loaded_vram + model_vram

        if proposed_total > self.config.gpu.max_vram_gb:
            return False, (
                f"Would exceed VRAM budget: {loaded_vram:.1f}GB loaded + "
                f"{model_vram:.1f}GB requested = {proposed_total:.1f}GB > "
                f"{self.config.gpu.max_vram_gb:.1f}GB limit"
            )

        # nvidia-smi hard gate: cross-check actual free VRAM
        free_gb = await get_vram_free_gb()
        if free_gb is not None:
            required_free = model_vram + 2.0  # 2 GB safety margin for KV/compute/fragmentation
            if free_gb < required_free:
                await self.log_vram_snapshot("hard_gate_blocked", {
                    "model": model_name,
                    "free_gb": round(free_gb, 2),
                    "required_free_gb": round(required_free, 2),
                })
                return False, (
                    f"nvidia-smi: only {free_gb:.1f}GB free, "
                    f"need {required_free:.1f}GB ({model_vram:.1f}GB model + 2.0GB margin)"
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

            # Poll /api/ps until model disappears (confirms VRAM freed)
            confirmed = False
            deadline = time.time() + self.config.ollama.unload_timeout_seconds
            while time.time() < deadline:
                loaded = await self.get_loaded_models()
                if model_name not in {m.name for m in loaded}:
                    confirmed = True
                    break
                await asyncio.sleep(0.2)

            if confirmed:
                logger.info("Unloaded model '%s' from VRAM (confirmed)", model_name)
            else:
                logger.warning(
                    "Model '%s' still in /api/ps after %.0fs — proceeding anyway",
                    model_name, self.config.ollama.unload_timeout_seconds,
                )

            # Invalidate cache so scheduler sees updated state immediately
            self.residency_cache.invalidate()
            await self.log_vram_snapshot("model_unload", {
                "model": model_name,
                "confirmed": confirmed,
            })
            return True
        except Exception as e:
            logger.warning("Failed to unload model '%s': %s", model_name, e)
            return False

    async def log_vram_snapshot(self, event: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Write VRAM snapshot to /tmp/bastion-vram-journal.jsonl for crash forensics.

        Parameters
        ----------
        event : str
            Event type (model_load, model_unload, hard_gate_blocked, dispatch, tick).
        extra : dict, optional
            Additional event-specific data.
        """
        try:
            gpu = await query_gpu_status()
            loaded = await self.get_loaded_models()
            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "gpu_vram_used_mb": gpu.vram_used_mb,
                "gpu_vram_free_mb": gpu.vram_free_mb,
                "gpu_temp_c": gpu.temperature_c,
                "loaded_models": [{"name": m.name, "vram_gb": m.vram_gb} for m in loaded],
                "total_loaded_vram_gb": round(sum(m.vram_gb for m in loaded), 2),
            }
            if extra:
                snapshot.update(extra)
            journal_path = Path("/tmp/bastion-vram-journal.jsonl")
            with journal_path.open("a") as f:
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

    def __init__(self, reservation_id: str, model: str, vram_bytes: int, ttl: float = 120.0) -> None:
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
        self._reservations: Dict[str, VRAMReservation] = {}
        self._model_allocations: Dict[str, int] = {}  # Per-model committed bytes
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
        """Available VRAM in bytes after accounting for allocations, reservations, and safety margin."""
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

        Raises
        ------
        ValueError
            If insufficient VRAM available.
        """
        self._reclaim_expired_sync()  # No awaits!

        async with self._lock:
            if vram_bytes > self.available_vram:
                raise ValueError(
                    f"Insufficient VRAM: need {vram_bytes // (1024*1024)}MB, "
                    f"available {self.available_vram // (1024*1024)}MB "
                    f"(allocated={self._allocated // (1024*1024)}MB, "
                    f"reserved={self._reserved // (1024*1024)}MB)"
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

    async def reconcile(self, loaded_model_names: set[str]) -> int:
        """Reconcile ledger with actual Ollama state.

        Removes allocations for models that Ollama no longer reports as loaded
        (e.g., auto-unloaded via keep_alive timeout). Also reclaims expired
        reservations.

        Parameters
        ----------
        loaded_model_names : set[str]
            Model names currently reported by Ollama /api/ps.

        Returns
        -------
        int
            Total bytes freed by reconciliation.
        """
        self._reclaim_expired_sync()

        async with self._lock:
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
            if freed_total:
                audit.emit("vram_reconciliation", {
                    "stale_models": stale,
                    "freed_bytes": freed_total,
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