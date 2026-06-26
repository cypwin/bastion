"""FastAPI application factory for BASTION.

Creates the FastAPI app with all routes:
  - Ollama proxy routes (catch-all for /api/*)
  - Admin API routes (/broker/*)
  - A2A interface routes (/.well-known/*, /a2a/*)  [Phase 5]

Lifecycle management: creates/destroys the proxy, VRAM tracker, queue,
and scheduler on startup/shutdown via FastAPI lifespan.

Scheduler integration:
  The scheduler runs as a background task. Scheduled proxy requests are
  placed in the AffinityQueue and block until the scheduler grants them
  (model loaded and ready). The grant mechanism uses per-request
  asyncio.Events stored in a pending dict.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import statistics
import subprocess
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import bastion
from bastion import audit
from bastion.auth import make_a2a_token_dependency, make_admin_key_dependency
from bastion.circuitbreaker import CircuitBreakerTransport
from bastion.correlation import (
    ContentionEventDetector,
    CorrelationEngine,
    build_thermal_coupling,
    compute_risk_index,
    enrich_stall_reason,
)
from bastion.health import check_gpu_safe, query_gpu_status
from bastion.latency_aggregator import aggregate_latency
from bastion.metrics import (
    CONTENT_TYPE_LATEST,
    PROMETHEUS_AVAILABLE,
    get_metrics_text,
    record_contention_event,
    record_risk_dominant_factor,
    update_risk_index,
    update_thermal_coupling_active,
    update_thermal_headroom_celsius,
    update_vram_ledger_drift,
)
from bastion.middleware import MetricsMiddleware
from bastion.models import (
    BlockDeviceIOStats,
    BrokerCatalog,
    BrokerConfig,
    BrokerCounters,
    BrokerLatency,
    BrokerStatus,
    BrokerThrashing,
    BrokerThrashingAgent,
    CatalogEntry,
    ContentionSnapshot,
    CorrelationState,
    GPUExtendedStatus,
    GPUStatus,
    InferenceThroughputState,
    IntentDeclaration,
    IntentResponse,
    MachineSnapshot,
    PriorityTier,
    ProcessSnapshot,
    QueuedRequest,
    ThrashingVerdictLabel,
    XidEvent,
)
from bastion.proxy import OllamaProxy
from bastion.queue import AffinityQueue
from bastion.ratelimit import RateLimitMiddleware
from bastion.scheduler import Scheduler
from bastion.thrashing import ThrashingDetector, ThrashingVerdict
from bastion.vram import VRAMManager, VRAMTracker
from bastion.watchdog import (
    ProcessMonitor,
    init_watchdog,
    notify_ready,
    notify_status,
    notify_stopping,
)

logger = logging.getLogger(__name__)

# Module-level state (set during lifespan)
_proxy: OllamaProxy | None = None
_vram_tracker: VRAMTracker | None = None
_vram_manager: VRAMManager | None = None
_queue: AffinityQueue | Any = None  # AffinityQueue or PersistentQueue at runtime
_scheduler: Scheduler | None = None
_a2a_handler: Any | None = None  # A2AHandler (avoid circular import at module level)
_a2a_http_client: httpx.AsyncClient | None = None  # Shared httpx client for A2A (CB transport)
_config: BrokerConfig | None = None
_process_monitor: ProcessMonitor | None = None
_sweep_task: asyncio.Task | None = None
_start_time: float = 0.0
_reset_epoch: str = ""  # ISO-8601 UTC timestamp set once at broker startup


def _detect_git_sha() -> str:
    """Return the BASTION git SHA for this install, or ``unknown``.

    Order of precedence:
      1. ``BASTION_GIT_SHA`` env var (set by deploy tooling for wheels).
      2. ``git rev-parse HEAD`` in the package root (development installs).
      3. ``unknown`` (no git, no env var — e.g., wheel install without deploy SHA).

    Captured once at module load by ``_GIT_SHA``; the /broker/version route
    returns this value so A2A clients can pin SHA across a
    long batch and detect mid-run redeploys.
    """
    sha = os.environ.get("BASTION_GIT_SHA", "").strip()
    if sha:
        return sha
    try:
        # __file__ -> .../src/bastion/server.py; package repo root is two up.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        # Only trust git when the package root itself is the checkout (.git
        # entry present — a dir, or a file for worktrees). Without this, a
        # wheel under site-packages nested inside some unrelated repo would
        # report THAT repo's SHA.
        if not os.path.exists(os.path.join(repo_root, ".git")):
            return "unknown"
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        logger.debug(
            "git SHA detection: rev-parse rc=%s stderr=%s",
            result.returncode, result.stderr.strip(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git SHA detection failed: %s", exc)
    return "unknown"


def _redact_home(path: str) -> str:
    """Replace the home-directory prefix with ``~`` in operator-facing paths.

    The admin surface is unauthenticated by default (ADR-006 bearer auth
    pending); absolute paths would disclose the username and filesystem
    layout to anything that can reach the port.
    """
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home):]
    return path


_GIT_SHA: str = _detect_git_sha()
_thrashing_detector: ThrashingDetector | None = None

# Verdict label and ordering maps used by /broker/thrashing in both create_app
# and create_admin_app.  Defined at module level to avoid duplication.
_THRASHING_VERDICT_LABEL: dict[ThrashingVerdict, ThrashingVerdictLabel] = {
    ThrashingVerdict.OK: "OK",
    ThrashingVerdict.WARN: "WARNED",
    ThrashingVerdict.HALT: "HALTED",
}
_THRASHING_VERDICT_ORDER: dict[ThrashingVerdict, int] = {
    ThrashingVerdict.OK: 0,
    ThrashingVerdict.WARN: 1,
    ThrashingVerdict.HALT: 2,
}

# Maps request ID -> asyncio.Event that the scheduler sets when the request
# is granted (model loaded, ready to forward to Ollama).
_pending_grants: dict[str, asyncio.Event] = {}

# Maps request ID -> asyncio.Event set by the proxy when the Ollama response
# completes. _dispatch_request awaits this to block the scheduler until one
# request finishes before granting the next — prevents concurrent Ollama access
# which can crash the GPU under sustained load.
_pending_completions: dict[str, asyncio.Event] = {}

# Tracks models with in-flight (actively running) inference requests.
# Used by the scheduler to decide whether concurrent dispatch is safe:
# - Different co-resident models → dispatch concurrently
# - Same model already in-flight → serialize (OLLAMA_NUM_PARALLEL=1)
_inflight_models: dict[str, int] = {}  # model -> count of in-flight requests
_inflight_lock: asyncio.Lock | None = None  # initialized in lifespan

# S6: Active intent declarations (intent_id -> IntentDeclaration)
_active_intents: dict[str, IntentDeclaration] = {}

# S6: Resolved intent metadata (intent_id -> (PriorityTier, model_sequence))
_resolved_intents: dict[str, tuple] = {}


def _lookup_intent(intent_id: str):
    """Look up an active intent by ID for priority resolution.

    Returns (PriorityTier, model_sequence) or None if not found/expired.
    """
    return _resolved_intents.get(intent_id)

# ── Recent requests ring buffer (S5: Dashboard Evolution) ────────────
_recent_requests: deque[dict] = deque(maxlen=500)

# ── Machine snapshot collection (observability spec 4.9/4.10) ─────────
# The broker owns collection so /broker/snapshot works headless; the TUI is
# a client (ADR-005). The deque stores model_dump() dicts so ?history=N
# slices need no re-serialization (mirrors _recent_requests). maxlen=180 ≈
# 6 min at the 2s fast tick (Constraint #1). Bounded, in-memory, no DB.
_machine_snapshot_deque: deque[dict] = deque(maxlen=180)
_machine_snapshot_task: asyncio.Task | None = None
# SSE snapshot stream (spec 5.6). Caps concurrent /broker/snapshot/stream
# clients so a misbehaving fleet of subscribers can't open unbounded
# generators (each holds a slow loop). The 9th concurrent client gets 503.
# In-process int (single event loop) — no lock needed; mutated only on the
# loop thread at stream open/close.
_SNAPSHOT_STREAM_MAX_CLIENTS = 8
_snapshot_stream_clients = 0
# Most-recent slow-path GPU extended status (throttle/PCIe/Xid). Refreshed only
# on the slow tick (spec 4.9, ~30s) by a backend subprocess; cached here so the
# 2s fast snapshot can attach the latest value without re-polling — the fast
# path stays free of blocking 30s-stale subprocess work. None until the first
# slow tick runs (graceful: a partial snapshot with gpu_extended=None is valid).
_gpu_extended_latest: GPUExtendedStatus | None = None
# Broker-side host-pressure collector (PSI/swap/OOM). Distinct from the TUI's
# own SystemDataCollector instance — each tracks its own delta state on an
# independent cadence (spec 4.9). Instantiated lazily so the import stays cheap
# and tests that never touch the snapshot path pay nothing.
_system_collector: Any | None = None
# Most-recent process-attribution snapshot (spec 5.3 / 4.5). Owned by the
# broker-side slow tick so GET /broker/processes works headless (no TUI). The
# collector instance holds the per-process IO / churn delta state; this caches
# only the latest assembled ProcessSnapshot for the endpoint. TUI + JSON only —
# never a Prometheus label. None until the first tick runs.
_process_snapshot_latest: ProcessSnapshot | None = None
# Correlation engine + discrete-contention detector (observability spec Section 6).
# Both are in-memory + bounded (ring maxlen=512, contention deque maxlen=50). The
# engine is PASSIVE: it consumes the MachineSnapshot the loop already assembled
# (pull, never push) and owns no background task — its tick() runs at the end of
# each _collect_machine_snapshot pass. Instantiated in lifespan AFTER
# scheduler/vram_tracker/vram_manager are built and BEFORE the snapshot loop
# starts; deps are scheduler/vram only (NOT _a2a_handler, so A2A-disabled
# deployments are fully functional, spec 6.6).
_correlation_engine: CorrelationEngine | None = None
_contention_detector: ContentionEventDetector | None = None


def _get_system_collector() -> Any:
    """Return the broker-side SystemDataCollector, creating it on first use."""
    global _system_collector
    if _system_collector is None:
        from bastion.dashboard.collectors import SystemDataCollector
        _system_collector = SystemDataCollector()
    return _system_collector


async def _collect_contention() -> ContentionSnapshot | None:
    """Assemble the host-pressure leg of the snapshot (spec 4.4, fast 2s path).

    Every sub-collector is individually graceful (returns None fields / empty
    lists on hosts that lack the source — no PSI on old kernels, no swap on
    first read, no powercap on containers/ARM, non-NVMe storage), so a partial
    ``ContentionSnapshot`` is the expected value here and is still emitted. All
    of PSI/swap/OOM/block-device-IO/CPU-package-power are cheap 2s fast-path
    signals (spec 5.2); ``gpu_board_watts`` stays ``None`` (backend-provided,
    Tier 4). No degradation path emits a misleading ``0``.
    """
    try:
        collector = _get_system_collector()
        psi = collector.get_psi_data()
        swap = collector.get_swap_rate_data()
        oom = collector.get_oom_data()

        device_filter = None
        if _config is not None:
            device_filter = _config.observability.storage_device_filter
        rapl_path = None
        if _config is not None:
            rapl_path = _config.observability.rapl_domain_path

        block_rows = collector.get_block_io_data(device_filter)
        block_devices = [BlockDeviceIOStats(**row) for row in block_rows]
        cpu_package_watts = collector.read_package_power(rapl_path)

        return ContentionSnapshot(
            psi_cpu_some_avg10=psi.get("psi_cpu_some_avg10"),
            psi_cpu_full_avg10=psi.get("psi_cpu_full_avg10"),
            psi_mem_some_avg10=psi.get("psi_mem_some_avg10"),
            psi_mem_full_avg10=psi.get("psi_mem_full_avg10"),
            psi_io_some_avg10=psi.get("psi_io_some_avg10"),
            psi_io_full_avg10=psi.get("psi_io_full_avg10"),
            swap_in_rate_mb_s=swap.get("swap_in_rate_mb_s"),
            swap_out_rate_mb_s=swap.get("swap_out_rate_mb_s"),
            block_devices=block_devices,
            cpu_package_watts=cpu_package_watts,
            oom_kill_total=oom.get("oom_kill_total"),
            oom_kill_rate=oom.get("oom_kill_rate"),
        )
    except Exception:
        logger.exception("contention collection failed; emitting None leg")
        return None


async def _collect_gpu_extended() -> GPUExtendedStatus | None:
    """Assemble the slow-path GPU leg (spec 4.3, ~30s slow tick).

    Routes every signal through the ``GPUBackend`` seam (Constraint #7c): the
    throttle reasons, PCIe tx/rx, and Xid scan are NVIDIA concepts and come
    back empty/``None`` from ``StubBackend`` (non-NVIDIA / no-GPU), which is
    the *correct complete* value there — not a degradation. Each leg is
    individually graceful so a denied ``dmesg`` (``dmesg_restrict=1``) or a
    pre-R418 driver (no PCIe tx/rx) yields ``[]``/``None`` rather than killing
    the slow tick. The ``recent_xids`` list is already bounded (maxlen 20) at
    the backend.
    """
    from bastion.gpu import get_backend

    backend = get_backend()
    try:
        throttle_reasons = await backend.query_throttle_reasons()
    except Exception:
        logger.debug("throttle-reasons slow poll failed", exc_info=True)
        throttle_reasons = []
    try:
        pcie_tx, pcie_rx = await backend.query_pcie_throughput()
    except Exception:
        logger.debug("PCIe-throughput slow poll failed", exc_info=True)
        pcie_tx, pcie_rx = None, None
    try:
        xid_rows = await backend.query_xid_errors()
    except Exception:
        logger.debug("Xid slow poll failed", exc_info=True)
        xid_rows = []

    recent_xids = [XidEvent(**row) for row in xid_rows]
    # xid_count_since_start lives on NvidiaBackend; StubBackend has no such
    # attribute, so read it defensively (0 on non-NVIDIA — correct, complete).
    xid_count = getattr(backend, "xid_count_since_start", 0)

    return GPUExtendedStatus(
        throttle_reasons=throttle_reasons,
        pcie_tx_kb_s=pcie_tx,
        pcie_rx_kb_s=pcie_rx,
        recent_xids=recent_xids,
        xid_count_since_start=xid_count,
        last_polled_at=time.time(),
    )


async def _collect_process_snapshot(slow_tick: bool) -> ProcessSnapshot | None:
    """Assemble the per-process attribution leg (spec 5.3 / 4.5).

    Delegates to the broker-side ``SystemDataCollector.collect_process_snapshot``
    (which owns the per-process IO/churn delta state). The collector is fully
    graceful — a missing ``psutil`` or a wholesale scan failure yields a valid
    (empty) ``ProcessSnapshot`` — so this only guards the call itself and the
    config read. The GPU sub-data join (compute-apps VRAM + pmon) runs only on
    the slow tick through the async ``GPUBackend`` seam and is empty on a
    ``StubBackend`` / no-GPU host (no error). This data is **TUI + JSON only** —
    never a Prometheus label (Constraint #2).
    """
    try:
        collector = _get_system_collector()
        return await collector.collect_process_snapshot(_config, slow_tick=slow_tick)
    except Exception:
        logger.exception("process snapshot leg failed; emitting None leg")
        return None


def _collect_inference_throughput() -> InferenceThroughputState | None:
    """Aggregate the stream-tapped per-request token signals (spec 4.6).

    Reads the per-request token/TTFT/ctx fields that the inference tap writes
    into ``_recent_requests`` (Section 4.6) and folds them into p50 aggregates.
    Model-agnostic: it uses whatever ``model`` Ollama reported on each request.
    Returns ``None`` when no recent request carried any token signal (e.g. only
    non-inference traffic, or before the first stream completes) — never a
    misleading ``0``.
    """
    try:
        decode: list[float] = []
        prefill: list[float] = []
        ttft: list[float] = []
        ctx: list[float] = []
        last_model: str | None = None
        for rec in _recent_requests:
            d = rec.get("decode_tps")
            if d is not None:
                decode.append(d)
            p = rec.get("prefill_tps")
            if p is not None:
                prefill.append(p)
            t = rec.get("ttft_s")
            if t is not None:
                ttft.append(t)
            c = rec.get("ctx_utilization")
            if c is not None:
                ctx.append(c)
            if (
                rec.get("decode_tps") is not None
                or rec.get("ttft_s") is not None
            ) and rec.get("model"):
                last_model = rec.get("model")

        if not (decode or prefill or ttft or ctx):
            return None  # no token signal yet -> None, not a row of zeros

        def _p50(values: list[float]) -> float | None:
            if not values:
                return None
            return statistics.median(values)

        return InferenceThroughputState(
            decode_tps_p50=_p50(decode),
            prefill_tps_p50=_p50(prefill),
            ttft_p50_s=_p50(ttft),
            ctx_utilization_p50=_p50(ctx),
            last_model=last_model,
        )
    except Exception:
        logger.debug("inference-throughput aggregation failed", exc_info=True)
        return None


async def _collect_broker_status_lite() -> BrokerStatus | None:
    """Assemble a lightweight ``BrokerStatus`` for embedding in the snapshot.

    Mirrors the /broker/status assembly but stays defensive: any missing
    collaborator yields a partial status rather than raising, so the snapshot
    loop never dies. The full observability-field enrichment lives on the
    /broker/status route; the snapshot embeds the core model.
    """
    try:
        loaded_raw = (
            await _vram_tracker.get_loaded_models() if _vram_tracker else []
        )
        loaded = loaded_raw if loaded_raw is not None else []
        return BrokerStatus(
            uptime_seconds=time.time() - _start_time,
            queue_depth=_queue.total_size if _queue else 0,
            queue_by_model=_queue.queue_depth_by_model() if _queue else {},
            loaded_models=loaded,
            vram_state="unknown" if loaded_raw is None else "ok",
            current_model=_scheduler.current_model if _scheduler else None,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_model_swaps=_scheduler.total_swaps if _scheduler else 0,
            state="draining" if (_scheduler and _scheduler.is_draining) else "running",
            total_dispatched=_scheduler.total_dispatched if _scheduler else 0,
            inflight_models=dict(_inflight_models),
        )
    except Exception:
        logger.exception("broker status leg failed; emitting None leg")
        return None


def _slow_tick_divisor() -> int:
    """Integer tick-modulo for the slow path (spec 4.9).

    Derived from ``round(slow_tick_interval_s / snapshot_interval_s)`` so the
    cadence comes from ``ObservabilityConfig`` rather than a magic literal
    (default 30s / 2s = 15). Clamped to >=1 so a misconfiguration cannot make
    the slow path run never or every-tick-divide-by-zero.
    """
    fast = 2.0
    slow = 30.0
    if _config is not None:
        fast = max(0.25, _config.observability.snapshot_interval_s)
        slow = max(fast, _config.observability.slow_tick_interval_s)
    return max(1, round(slow / fast))


async def _collect_machine_snapshot(tick: int) -> MachineSnapshot:
    """Assemble one ``MachineSnapshot`` from the backend + collectors (spec 4.9).

    Each leg is wrapped for graceful ``None`` (Constraint #4): a failing or
    absent source yields a ``None``/empty leg, never an exception and never a
    misleading ``0``. On a ``StubBackend`` / no-GPU host ``query_gpu_status``
    returns an empty ``GPUStatus`` (all inner fields ``None``) — the correct
    complete value there.

    Two cadences (spec 4.9): the **fast 2s path** builds the GPU status,
    contention (PSI/swap/OOM/block-IO/CPU-power), and the cheap in-memory
    ``inference`` aggregate every tick. The **slow path** (``tick %
    _slow_tick_divisor() == 0``, ~30s) refreshes the subprocess-heavy
    ``gpu_extended`` leg (throttle/PCIe/Xid via the backend) and the VRAM
    ledger-drift gauge; the most recent ``gpu_extended`` is cached in
    ``_gpu_extended_latest`` and reattached on the intervening fast ticks, so
    the 2s path never blocks on 30s-stale subprocess work. After assembly the
    correlation engine ticks on the snapshot (spec 6.6) and the synthesized
    ``correlation`` leg (RiskIndex / thermal coupling / contention events /
    enriched stall / ring tail) is folded back in.
    """
    global _gpu_extended_latest, _process_snapshot_latest
    snapshot_ts = time.time()

    try:
        gpu = await query_gpu_status()
    except Exception:
        logger.exception("GPU status leg failed; using empty GPUStatus")
        gpu = GPUStatus()

    broker = await _collect_broker_status_lite()
    contention = await _collect_contention()
    inference = _collect_inference_throughput()

    slow = _slow_tick_divisor()
    # Process attribution: the cheap top-N / IO / watchlist legs run every fast
    # tick; the subprocess-heavy churn + GPU-join legs run only on the slow tick
    # (gated inside the collector by ``slow_tick``). The assembled snapshot is
    # cached for GET /broker/processes so the endpoint works headless.
    process = await _collect_process_snapshot(slow_tick=(tick % slow == 0))
    if process is not None:
        _process_snapshot_latest = process

    if tick % slow == 0:
        # Slow tick: refresh the subprocess-heavy GPU extended leg and cache it
        # so fast ticks reuse the latest value without re-polling.
        try:
            _gpu_extended_latest = await _collect_gpu_extended()
        except Exception:
            logger.debug("gpu_extended slow collection failed", exc_info=True)
        # Publish the signed VRAM ledger-drift gauge (spec 5.4). Drift =
        # measured (backend) − tracked (allocated+reserved). SKIP — never
        # publish 0 — when the backend reports no measured VRAM (StubBackend /
        # non-NVIDIA) or no VRAMManager is configured.
        await _emit_vram_ledger_drift(gpu)

    snapshot = MachineSnapshot(
        snapshot_ts=snapshot_ts,
        broker=broker,
        gpu=gpu,
        gpu_extended=_gpu_extended_latest,
        contention=contention,
        process=process,
        inference=inference,
        correlation=None,
    )

    # Tick the correlation engine on the just-assembled snapshot (pull, never
    # push — spec 6.6: tick at the END of the iteration, consuming the snapshot
    # the loop built) and fold the synthesized CorrelationState back in. Wrapped
    # so a correlation failure never kills the snapshot tick (Constraint #4).
    try:
        snapshot.correlation = await _collect_correlation_state(snapshot)
    except Exception:
        logger.debug("correlation leg failed; leaving correlation=None", exc_info=True)

    return snapshot


def _correlation_config():
    """Return the active ``CorrelationConfig`` (defaults when unconfigured)."""
    if _config is not None:
        return _config.observability.correlation
    from bastion.models import CorrelationConfig

    return CorrelationConfig()


async def _vram_utilization_pct() -> float | None:
    """VRAM used as a percentage of the budget, or ``None`` (RiskIndex input).

    Reads the in-memory ledger (``allocated + reserved`` over ``total``). Returns
    ``None`` — never a misleading ``0`` — when no ``VRAMManager`` is configured or
    the ledger reports a non-positive total, so the RiskIndex term is *absent*
    rather than reading zero-risk for an unmeasured budget.
    """
    if _vram_manager is None:
        return None
    try:
        status = await _vram_manager.status()
    except Exception:
        return None
    total = status.get("total_bytes") or 0
    if total <= 0:
        return None
    used = (status.get("allocated_bytes") or 0) + (status.get("reserved_bytes") or 0)
    return max(0.0, min(100.0, (used / total) * 100.0))


def _worst_thrashing_verdict() -> str | None:
    """The worst verdict label across tracked agents, or ``None`` (RiskIndex input).

    ``None`` when no detector is configured or no agent is tracked yet — the
    thrashing risk term is then *absent*, not a misleading zero-risk reading.
    """
    if _thrashing_detector is None:
        return None
    try:
        snaps = _thrashing_detector.snapshot()
    except Exception:
        return None
    if not snaps:
        return None
    # Rank OK < WARNED < HALTED; return the worst label seen.
    order = {"OK": 0, "WARNED": 1, "HALTED": 2}
    worst = max(snaps, key=lambda s: order.get(str(s.verdict), 0))
    return str(worst.verdict)


def _read_cpu_temp_for_correlation() -> float | None:
    """Best-effort host CPU temperature for the thermal-coupling derivation.

    Sourced from the broker-side ``SystemDataCollector`` (portable hwmon
    discovery; ``None`` when no CPU sensor exists). Never raises — a missing
    sensor degrades the coupling to its GPU-only / present-terms-only form.
    """
    try:
        collector = _get_system_collector()
        return collector.read_cpu_temp()
    except Exception:
        return None


async def _collect_correlation_state(
    snapshot: MachineSnapshot,
) -> CorrelationState | None:
    """Tick the engine on ``snapshot`` and assemble the ``CorrelationState`` (6.6).

    This is the single integration point (spec 6.6): it (1) ticks the passive
    :class:`CorrelationEngine` on the already-assembled snapshot so the ring
    ingests the four pull sources, (2) feeds the discrete
    :class:`ContentionEventDetector` with the live stall state, (3) derives the
    composite RiskIndex + thermal coupling from inputs already in hand, (4)
    enriches the scheduler stall reason with live host context, and (5) emits the
    bounded-label Prometheus metrics. Every step is individually guarded so a
    single failure never blocks the snapshot tick (Constraint #4); a partial
    ``CorrelationState`` (e.g. no thermal coupling on a no-GPU host) is valid.

    Returns ``None`` only when the engine was never constructed (e.g. an
    out-of-lifespan on-demand collect in a degenerate test path); the snapshot is
    still valid with ``correlation=None``.
    """
    engine = _correlation_engine
    if engine is None:
        return None
    cfg = _correlation_config()

    # (1) Tick the engine (audit/inference cursor pulls + system/GPU/throttle
    # edge emitters). Guarded inside tick(), but belt-and-suspenders here too.
    try:
        engine.tick(snapshot)
    except Exception:
        logger.debug("engine.tick failed", exc_info=True)

    # Live scheduler stall state (the coincidence-join input + enrichment base).
    stall_reason_base = _scheduler.stall_reason if _scheduler else ""
    inference_stalled = bool(stall_reason_base)

    # (2) Discrete contention detector — fire only on a pressure crossing that
    # COINCIDES with a real inference stall (the moat). Emits the bounded-kind
    # Prometheus counter on a fired event (attribution stays JSON-only).
    if _contention_detector is not None:
        try:
            fired = _contention_detector.feed(
                snapshot,
                inference_stalled=inference_stalled,
                stall_reason=stall_reason_base or None,
            )
            if fired is not None:
                with contextlib.suppress(Exception):
                    record_contention_event(fired.kind)
        except Exception:
            logger.debug("contention detector feed failed", exc_info=True)

    # (3a) Thermal coupling (CPU/GPU temps, fan, headroom). All inputs None-
    # tolerant; a no-GPU host yields the CPU-only / present-terms form.
    thermal = None
    try:
        gpu_max = _config.gpu.max_temperature_c if _config is not None else None
        thermal = build_thermal_coupling(
            cpu_temp_c=_read_cpu_temp_for_correlation(),
            gpu_temp_c=snapshot.gpu.temperature_c if snapshot.gpu else None,
            fan_speed_pct=snapshot.gpu.fan_speed_pct if snapshot.gpu else None,
            gpu_max_temperature_c=gpu_max,
            config=cfg,
        )
    except Exception:
        logger.debug("thermal coupling build failed", exc_info=True)

    # (3b) Composite RiskIndex — each component degrades independently (None =
    # absent term, never a misleading zero-risk).
    risk = None
    try:
        mem_psi = (
            snapshot.contention.psi_mem_some_avg10
            if snapshot.contention is not None
            else None
        )
        risk = compute_risk_index(
            vram_utilization_pct=await _vram_utilization_pct(),
            thermal_headroom_c=(
                thermal.thermal_headroom_min_c if thermal is not None else None
            ),
            swap_rate_level=_scheduler._swap_rate_level if _scheduler else None,
            thrashing_verdict=_worst_thrashing_verdict(),
            memory_psi=mem_psi,
            config=cfg,
        )
    except Exception:
        logger.debug("risk index compute failed", exc_info=True)

    # (4) Enrich the stall reason with live host context (additive; None-omitting).
    enriched_stall = enrich_stall_reason(stall_reason_base or None, snapshot)

    # (5) Emit the bounded-label Prometheus metrics. Each guarded; the thermal-
    # headroom gauge is SKIPPED (never 0) when no headroom term is computable.
    _emit_correlation_metrics(risk, thermal)

    detector = _contention_detector
    recent_contentions = (
        list(detector.recent_contentions) if detector is not None else []
    )
    tail_n = getattr(cfg, "ring_tail_in_snapshot", 32)
    return CorrelationState(
        risk_index=risk,
        thermal_coupling=thermal,
        recent_contentions=recent_contentions,
        enriched_stall_reason=enriched_stall,
        ring_size=len(engine.ring),
        recent_ring_events=engine.ring.tail(tail_n),
    )


def _emit_correlation_metrics(risk, thermal) -> None:
    """Publish the five bounded-label correlation metrics (spec 6.3/6.4/6.5 / 7).

    Bounded labels only (Constraint #2): the risk gauge + thermal gauges are
    label-less; the dominant-factor counter uses the 5-name ``factor`` enum.
    Process attribution never reaches Prometheus. Guarded individually; the
    thermal-headroom gauge is SKIPPED — never set to ``0`` — when no headroom
    term is computable, so a no-GPU + no-CPU-sensor host does not falsely report
    zero headroom.
    """
    try:
        if risk is not None:
            update_risk_index(risk.score)
            if risk.dominant_factor:
                record_risk_dominant_factor(risk.dominant_factor)
    except Exception:
        logger.debug("risk metric emit failed", exc_info=True)
    try:
        if thermal is not None:
            update_thermal_coupling_active(bool(thermal.coupling_active))
            if thermal.thermal_headroom_min_c is not None:
                update_thermal_headroom_celsius(thermal.thermal_headroom_min_c)
    except Exception:
        logger.debug("thermal metric emit failed", exc_info=True)


async def _emit_vram_ledger_drift(gpu: GPUStatus) -> None:
    """Publish ``bastion_vram_ledger_drift_mb{gpu_index}`` (spec 5.4, slow tick).

    Drift is the signed delta between **measured** VRAM (the backend's
    ``query_status`` ``vram_used_mb``, already in hand on this tick) and
    **tracked** VRAM (``allocated + reserved`` from the in-memory ledger). A
    growing positive drift means the ledger under-counts real residency, which
    is the unsafe direction BASTION exists to catch.

    Graceful degradation (Constraint #4): if the backend reports no measured
    value (``vram_used_mb is None`` on StubBackend / non-NVIDIA) or no
    ``VRAMManager`` is configured, the gauge is **skipped** — publishing ``0``
    would falsely claim a perfectly-synced ledger on a host that simply cannot
    read VRAM. Any failure degrades to a logged skip, never an exception that
    would kill the snapshot tick.
    """
    try:
        measured_mb = gpu.vram_used_mb
        if measured_mb is None:
            return  # no measured value → skip, do not emit a misleading 0
        if _vram_manager is None:
            return  # nothing to compare against
        status = await _vram_manager.status()
        tracked_bytes = status.get("allocated_bytes", 0) + status.get("reserved_bytes", 0)
        tracked_mb = tracked_bytes / (1024 * 1024)
        drift_mb = float(measured_mb) - tracked_mb
        update_vram_ledger_drift(gpu_index=str(gpu.gpu_index), mb=drift_mb)
    except Exception:
        logger.debug("VRAM ledger-drift gauge emission skipped", exc_info=True)


async def _machine_snapshot_loop() -> None:
    """Monotonic-anchored collection loop (spec 4.9, the single tick authority).

    Records ``collection_start = time.monotonic()`` before the work and sleeps
    ``max(0.0, interval - elapsed)`` so a slow ``nvidia-smi`` (up to its 5s
    timeout during a GPU lockup) does not compound cadence drift. The interval
    is the configured fast tick (``observability.snapshot_interval_s``, default
    2s). The body is wrapped so a transient exception never kills the loop and
    leaves the snapshot stale forever.
    """
    interval = 2.0
    if _config is not None:
        interval = max(0.25, _config.observability.snapshot_interval_s)
    tick = 0
    while True:
        collection_start = time.monotonic()
        try:
            snap = await _collect_machine_snapshot(tick)
            _machine_snapshot_deque.appendleft(snap.model_dump())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("snapshot loop iteration failed")  # loop never dies
        tick += 1
        elapsed = time.monotonic() - collection_start
        await asyncio.sleep(max(0.0, interval - elapsed))


async def _handle_snapshot(request: Request) -> Any:
    """Standalone handler for ``GET /broker/snapshot`` (dual-registered, 4.10).

    Returns the most recent ``MachineSnapshot`` from the in-memory deque. If
    the collection loop has not produced one yet (e.g. immediately after
    startup), it collects one on demand so the endpoint never 404s or returns
    an empty body before the first tick. ``?history=N`` returns the last N
    snapshots (newest first), capped at the deque length. ``?include_ring=true``
    expands the full correlation ring into ``correlation.recent_ring_events``
    (debug surface, spec 6.1) instead of the default bounded tail.
    """
    history_raw = request.query_params.get("history")
    if history_raw is not None:
        try:
            n = int(history_raw)
        except ValueError:
            n = 1
        n = max(1, min(n, len(_machine_snapshot_deque) or 1))
        items = list(_machine_snapshot_deque)[:n]
        if not items:
            items = [(await _collect_machine_snapshot(0)).model_dump()]
        return JSONResponse({"snapshots": items, "count": len(items)})

    include_ring = request.query_params.get("include_ring", "").lower() in (
        "1", "true", "yes",
    )
    if _machine_snapshot_deque:
        body = _machine_snapshot_deque[0]
    else:
        # No tick has landed yet — collect one on demand.
        body = (await _collect_machine_snapshot(0)).model_dump()
    if include_ring:
        body = _expand_full_ring(body)
    return JSONResponse(body)


def _expand_full_ring(body: dict) -> dict:
    """Return a copy of ``body`` with the FULL correlation ring (6.1 debug).

    The snapshot deque stores only the bounded ring tail; ``?include_ring=true``
    swaps in the entire ring from the live engine. Degrades to the unchanged body
    when the engine is absent or the correlation leg is missing — never an error.
    """
    engine = _correlation_engine
    corr = body.get("correlation")
    if engine is None or not isinstance(corr, dict):
        return body
    try:
        full = [ev.model_dump() for ev in engine.ring]
    except Exception:
        return body
    out = dict(body)
    out_corr = dict(corr)
    out_corr["recent_ring_events"] = full
    out_corr["ring_size"] = len(full)
    out["correlation"] = out_corr
    return out


async def _sse_wrapper(
    generator: AsyncGenerator[dict | None, None],
) -> AsyncGenerator[bytes, None]:
    """Wrap a dict-event generator as SSE-formatted bytes (shared helper).

    Single shared SSE encoder for every ``StreamingResponse`` in this module
    (the A2A task stream and ``/broker/snapshot/stream``). Deduped from the two
    near-identical nested copies that previously lived inside ``create_app`` and
    ``create_admin_app``. Handles three event shapes:

    - Heartbeats (``{"_heartbeat": True}``): emitted as an SSE comment
      ``: heartbeat\\n\\n`` (keeps the connection warm without a data frame).
    - Sentinels (``None``): stop the stream cleanly (generator should end).
    - Regular events (any other dict): ``data: {json}\\n\\n``.
    """
    async for event in generator:
        if event is None:
            break
        if isinstance(event, dict) and event.get("_heartbeat"):
            yield b": heartbeat\n\n"
            continue
        data = json.dumps(event)
        yield f"data: {data}\n\n".encode()


async def _snapshot_stream_events(
    request: Request,
) -> AsyncGenerator[dict | None, None]:
    """Yield the latest ``MachineSnapshot`` dict periodically for SSE (5.6).

    Emits the freshest snapshot immediately, then re-emits on the configured
    fast-tick cadence (``observability.snapshot_interval_s``). Between snapshot
    emits it yields a heartbeat marker so a stalled collection loop still keeps
    the connection warm. Honors client disconnect (``request.is_disconnected``)
    so a closed browser tab frees its slot promptly. Non-buffering: each frame
    is yielded the instant it is built (Constraint #3 — no accumulation).
    """
    interval = 2.0
    if _config is not None:
        interval = max(0.25, _config.observability.snapshot_interval_s)
    # Emit one snapshot right away so a subscriber sees data without waiting a
    # full interval; collect on demand if the loop hasn't produced one yet.
    if _machine_snapshot_deque:
        yield _machine_snapshot_deque[0]
    else:
        yield (await _collect_machine_snapshot(0)).model_dump()
    last_emitted: int = id(_machine_snapshot_deque[0]) if _machine_snapshot_deque else 0
    # Poll on a short sub-cadence so disconnects are noticed quickly, but only
    # push a new data frame when a fresh snapshot has actually landed.
    poll = min(0.5, interval)
    while True:
        if await request.is_disconnected():
            break
        await asyncio.sleep(poll)
        if _machine_snapshot_deque:
            head = _machine_snapshot_deque[0]
            if id(head) != last_emitted:
                last_emitted = id(head)
                yield head
                continue
        yield {"_heartbeat": True}


async def _handle_snapshot_stream(request: Request) -> Any:
    """Standalone handler for ``GET /broker/snapshot/stream`` (dual-registered).

    Server-Sent-Events surface (spec 5.6) that pushes the latest
    ``MachineSnapshot`` to web/monitoring/MCP clients. Supersedes the older
    2026-03-13 ``/broker/status/stream``; the TUI is unaffected (it keeps
    polling). Returns **501** when the stream is disabled by config
    (``observability.snapshot_stream_enabled``); caps concurrent clients at
    ``_SNAPSHOT_STREAM_MAX_CLIENTS`` (8) and returns **503** beyond that. The
    live-client counter is decremented in the generator's ``finally`` so a
    disconnect always frees the slot.
    """
    global _snapshot_stream_clients
    enabled = True
    if _config is not None:
        enabled = _config.observability.snapshot_stream_enabled
    if not enabled:
        return JSONResponse(
            {"error": "snapshot stream disabled"}, status_code=501
        )
    if _snapshot_stream_clients >= _SNAPSHOT_STREAM_MAX_CLIENTS:
        return JSONResponse(
            {"error": "too many concurrent stream clients"}, status_code=503
        )
    _snapshot_stream_clients += 1

    async def _bounded() -> AsyncGenerator[bytes, None]:
        global _snapshot_stream_clients
        try:
            async for frame in _sse_wrapper(_snapshot_stream_events(request)):
                yield frame
        finally:
            _snapshot_stream_clients -= 1

    return StreamingResponse(
        _bounded(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Prevents nginx 16KB buffering
        },
    )


async def _handle_contention(request: Request) -> Any:
    """Standalone handler for ``GET /broker/contention`` (dual-registered, 4.10).

    Returns the host-pressure leg (``ContentionSnapshot``, spec 4.4) of the
    most recent ``MachineSnapshot`` when the loop has produced one; otherwise
    collects the leg on demand so the endpoint never 404s before the first
    tick. ``None`` legs (no PSI on an old kernel/container, no powercap, no
    matching block device) are valid and returned as a partial snapshot with
    ``None`` fields — never a misleading ``0``.
    """
    if _machine_snapshot_deque:
        latest = _machine_snapshot_deque[0]
        contention = latest.get("contention")
        if contention is not None:
            return JSONResponse(contention)
    snap = await _collect_contention()
    if snap is None:
        # Collection failed wholesale — still return a valid (empty) body, not
        # a 500: a partial/empty ContentionSnapshot is the graceful contract.
        return JSONResponse(ContentionSnapshot().model_dump())
    return JSONResponse(snap.model_dump())


async def _handle_gpu_extended(request: Request) -> Any:
    """Standalone handler for ``GET /broker/gpu/extended`` (dual-registered, 4.10).

    Returns the slow-path GPU leg (``GPUExtendedStatus``, spec 4.3) — throttle
    reasons, PCIe tx/rx, recent Xids. Serves the cached value the slow tick
    maintains (``_gpu_extended_latest``); if no slow tick has run yet it
    collects once on demand. On a ``StubBackend`` / non-NVIDIA host the lists
    are the *correct complete* empty value, not an error.
    """
    if _gpu_extended_latest is not None:
        return JSONResponse(_gpu_extended_latest.model_dump())
    ext = await _collect_gpu_extended()
    if ext is None:
        return JSONResponse(GPUExtendedStatus().model_dump())
    return JSONResponse(ext.model_dump())


async def _handle_processes(request: Request) -> Any:
    """Standalone handler for ``GET /broker/processes`` (dual-registered, 4.10).

    Returns the most-recent ``ProcessSnapshot`` (spec 4.5) the slow tick
    maintains. If no tick has run yet it collects one on demand (fast legs only)
    so the endpoint returns **empty lists, not a 404**, before the first run. On
    a ``StubBackend`` / no-GPU host ``gpu_processes`` is the correct complete
    empty list. This is the JSON surface for the attribution data, which is
    **TUI + JSON only** — never a Prometheus label (Constraint #2).
    """
    if _process_snapshot_latest is not None:
        return JSONResponse(_process_snapshot_latest.model_dump())
    snap = await _collect_process_snapshot(slow_tick=False)
    if snap is None:
        return JSONResponse(ProcessSnapshot(collected_at=time.time()).model_dump())
    return JSONResponse(snap.model_dump())


def _latest_correlation_dict() -> dict | None:
    """Return the ``correlation`` leg of the most recent snapshot, or ``None``."""
    if _machine_snapshot_deque:
        corr = _machine_snapshot_deque[0].get("correlation")
        if corr is not None:
            return corr
    return None


async def _handle_correlation_risk(request: Request) -> Any:
    """Standalone handler for ``GET /broker/correlation/risk`` (dual-registered).

    Surfaces the composite RiskIndex + CPU<->GPU thermal coupling (spec 6.4/6.5)
    from the correlation leg of the most recent ``MachineSnapshot``. If no tick
    has produced one yet it collects a snapshot on demand so the endpoint never
    404s before the first collection. Both inner fields may legitimately be
    ``None`` (no GPU / no recent risk inputs) — a partial body, never an error.
    """
    corr = _latest_correlation_dict()
    if corr is None:
        # No tick has landed yet — collect one on demand (also ticks the engine).
        with contextlib.suppress(Exception):
            snap = await _collect_machine_snapshot(0)
            corr = snap.correlation.model_dump() if snap.correlation else None
    if corr is None:
        # Engine not constructed (out-of-lifespan): empty, not a 500.
        return JSONResponse({"risk_index": None, "thermal_coupling": None})
    return JSONResponse(
        {
            "risk_index": corr.get("risk_index"),
            "thermal_coupling": corr.get("thermal_coupling"),
        }
    )


async def _handle_correlation_contentions(request: Request) -> Any:
    """Standalone handler for ``GET /broker/correlation/contentions`` (dual-reg).

    Returns the last-N discrete contention events (spec 6.3) from the detector's
    dedicated bounded ``deque(maxlen=50)`` — kept separate from the snapshot body
    because contention events are not in the snapshot. Returns an **empty list,
    not a 404**, before the first coincidence fires. Attribution stays
    category-level (it may name a device, never a process — JSON-only).
    """
    detector = _contention_detector
    if detector is None:
        return JSONResponse({"contentions": [], "count": 0})
    events = [ev.model_dump() for ev in detector.recent_contentions]
    return JSONResponse({"contentions": events, "count": len(events)})


def record_recent_request(
    model: str,
    endpoint: str,
    tier: str,
    queue_wait_s: float,
    duration_s: float,
    status_code: int,
    streaming: bool = False,
    source: str | None = None,
    *,
    prefill_tps: float | None = None,
    decode_tps: float | None = None,
    ttft_s: float | None = None,
    ctx_utilization: float | None = None,
    eval_count: int | None = None,
    prompt_eval_count: int | None = None,
) -> None:
    """Record a completed request in the recent requests ring buffer.

    Called at true completion: for streaming requests that is after the
    last byte reached the client, so ``duration_s`` covers the full
    stream and ``status_code`` reflects the actual outcome. ``source`` is
    the client's declared identity (``X-Agent-ID`` header) or, failing
    that, its User-Agent product token — ``None`` when neither is sent.

    The six keyword-only inference signals (spec 4.6) are supplied by the
    proxy's :class:`~bastion.inference_tap.InferenceTapCollector` when a
    request completes; they all default to ``None`` so every existing
    caller that omits them keeps working unchanged (back-compat is a hard
    requirement). A ``None`` means "not measured" — never a misleading 0
    (e.g. a cache hit with ``eval_duration==0`` records ``decode_tps=None``).
    """
    _recent_requests.appendleft({
        "timestamp": time.time(),
        "model": model,
        "endpoint": endpoint,
        "tier": tier,
        "queue_wait_s": round(queue_wait_s, 3),
        "duration_s": round(duration_s, 3),
        "status_code": status_code,
        "streaming": streaming,
        "source": source,
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "ttft_s": ttft_s,
        "ctx_utilization": ctx_utilization,
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
    })


async def _enqueue_request(
    request: QueuedRequest,
) -> tuple[asyncio.Event, Callable[[], None], Callable[[], None]]:
    """Enqueue a scheduled request and return (grant_event, done_fn, cancel_fn).

    Called by OllamaProxy for /api/generate, /api/chat, /api/embed.
    The proxy awaits grant_event (set by scheduler when model is ready),
    then calls done_fn() when the Ollama response has fully completed.
    _dispatch_request awaits done_fn() before returning, which serializes
    all Ollama access through a single in-flight request at a time.

    cancel_fn removes the request from all tracking structures (queue,
    pending grants, pending completions) and should be called on grant
    timeout instead of done_fn to prevent ghost requests.

    Raises
    ------
    RuntimeError
        If the queue is full or broker is draining (caller should return 503).
    """
    # Reject new requests while draining
    if _scheduler and _scheduler.is_draining:
        raise RuntimeError("Draining")

    grant_event = asyncio.Event()
    done_event = asyncio.Event()
    _pending_grants[request.id] = grant_event
    _pending_completions[request.id] = done_event

    accepted = _queue.enqueue(request)
    if not accepted:
        _pending_grants.pop(request.id, None)
        _pending_completions.pop(request.id, None)
        raise RuntimeError("Queue full")

    # Wake the scheduler so it picks up the new request
    if _scheduler:
        _scheduler.notify()

    def done_fn() -> None:
        evt = _pending_completions.pop(request.id, None)
        if evt:
            evt.set()

    def cancel_fn() -> None:
        """Remove from all tracking: queue, pending grants, pending completions."""
        _pending_grants.pop(request.id, None)
        completion_evt = _pending_completions.pop(request.id, None)
        if completion_evt:
            completion_evt.set()  # Unblock any waiter
        if _queue:
            _queue.cancel(request.id)

    return grant_event, done_fn, cancel_fn


def has_inflight(model: str) -> bool:
    """Check if a model has an in-flight inference request.

    Called by the scheduler to decide dispatch strategy:
    - If model has in-flight request → serialize (same-model requests)
    - If model has no in-flight request → can dispatch concurrently
    """
    return _inflight_models.get(model, 0) > 0


def inflight_count() -> int:
    """Total number of in-flight inference requests across all models."""
    return sum(_inflight_models.values())


def _release_swept_request(req: QueuedRequest) -> None:
    """Release tracking state for a request swept as stale.

    The grant event is marked ``swept`` before being set so the waiting
    proxy handler returns 504 instead of forwarding to Ollama — a sweep
    is a rejection, not a grant (see KNOWN_ISSUES, resolved in v0.5).
    """
    grant_evt = _pending_grants.pop(req.id, None)
    if grant_evt:
        grant_evt.swept = True  # type: ignore[attr-defined]
        grant_evt.set()  # Unblock proxy handler waiting for grant
    completion_evt = _pending_completions.pop(req.id, None)
    if completion_evt:
        completion_evt.set()
    logger.warning(
        "Swept stale request %s (model=%s, age=%.0fs)",
        req.id, req.model, req.age_seconds,
    )
    audit.emit("queue_sweep", {
        "request_id": req.id,
        "model": req.model,
        "age_seconds": round(req.age_seconds, 1),
    })


async def _queue_sweep_loop(ttl_seconds: float) -> None:
    """Background task that sweeps stale requests every 60 seconds.

    For each swept request: unblocks any waiting proxy handler by setting
    grant and completion events, and logs an audit event.

    The body is wrapped in a per-iteration ``try/except`` so a transient
    exception (audit init race, queue invariant violation, etc.) doesn't
    kill the task and leave stale requests piling up forever — see
    scheduler._loop() for the matching pattern.
    """
    while True:
        try:
            await asyncio.sleep(60.0)
            if not _queue:
                continue
            swept = _queue.sweep_stale(ttl_seconds)
            for req in swept:
                _release_swept_request(req)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Queue sweep loop iteration failed; continuing")
            # Brief backoff so a deterministic crash doesn't spin
            await asyncio.sleep(5.0)


def _dispatch_error_cleanup(request_id: str) -> None:
    """Clean up tracking state when dispatch fails.

    Pops from _pending_grants and _pending_completions, setting both events
    so nothing waits forever. Called by scheduler._dispatch_error_fn.
    """
    grant_evt = _pending_grants.pop(request_id, None)
    if grant_evt:
        grant_evt.set()
    completion_evt = _pending_completions.pop(request_id, None)
    if completion_evt:
        completion_evt.set()
    logger.warning("Dispatch error cleanup for request %s", request_id)


async def _dispatch_request(request: QueuedRequest, needs_swap: bool = True) -> None:
    """Grant a queued request — called by Scheduler.

    Dispatch strategy depends on whether a model swap is needed:
    - **Swap needed OR same-model in-flight**: block until inference completes
      (serialized, prevents concurrent Ollama access during model transitions)
    - **Co-resident model, no same-model in-flight**: grant and return immediately
      (concurrent dispatch — scheduler can dispatch to other models in parallel)

    The done_fn() callback (called by proxy on Ollama response completion)
    always handles cleanup of the inflight tracking state.

    Parameters
    ----------
    request : QueuedRequest
        The request to dispatch.
    needs_swap : bool
        True if a model swap is needed (non-resident model). Default True
        for backward compatibility (serialized behavior).
    """
    global _inflight_lock
    if _inflight_lock is None:
        _inflight_lock = asyncio.Lock()

    grant_event = _pending_grants.pop(request.id, None)
    done_event = _pending_completions.get(request.id)  # Don't pop — proxy will pop via done_fn

    if grant_event is None:
        logger.warning("Dispatch for unknown request %s (may have timed out)", request.id)
        return

    # Determine blocking strategy
    async with _inflight_lock:
        model_inflight = _inflight_models.get(request.model, 0) > 0
    should_block = needs_swap or model_inflight

    # Track this request as in-flight
    async with _inflight_lock:
        _inflight_models[request.model] = _inflight_models.get(request.model, 0) + 1

    # Grant the request (unblocks proxy handler)
    grant_event.set()

    if should_block:
        # Blocking path: wait for inference to complete before returning
        # This serializes swaps and same-model requests
        if done_event is not None:
            timeout = (_config.proxy.inference_timeout_seconds if _config else 300.0) + 60.0
            try:
                await asyncio.wait_for(done_event.wait(), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "Completion event timed out for request %s (%.0fs) — "
                    "client may have disconnected or Ollama hung",
                    request.id, timeout,
                )
                _pending_completions.pop(request.id, None)
            finally:
                # Clean up inflight tracking on blocking path
                async with _inflight_lock:
                    count = _inflight_models.get(request.model, 1) - 1
                    if count <= 0:
                        _inflight_models.pop(request.model, None)
                    else:
                        _inflight_models[request.model] = count
    else:
        # Non-blocking path: return immediately, let done_fn handle cleanup
        # The proxy's done_fn will decrement _inflight_models when complete
        original_done_event = done_event

        # Wrap the done_fn cleanup: when the proxy signals completion,
        # also clean up inflight tracking
        async def _cleanup_inflight() -> None:
            # The wait + decrement are wrapped in try/finally so the
            # inflight counter decrement ALWAYS runs — otherwise a raise
            # inside done_event.wait() (CancelledError, network errors, ...)
            # would pin the counter above its true value and block the
            # scheduler from evicting this model forever. KNOWN_ISSUES,
            # resolved in v0.4.1.
            try:
                if original_done_event is not None:
                    timeout = (
                        _config.proxy.inference_timeout_seconds
                        if _config else 300.0
                    ) + 60.0
                    try:
                        await asyncio.wait_for(
                            original_done_event.wait(), timeout=timeout,
                        )
                    except TimeoutError:
                        logger.warning(
                            "Completion event timed out for non-blocking request %s (%.0fs)",
                            request.id, timeout,
                        )
                        _pending_completions.pop(request.id, None)
            except Exception:
                logger.exception(
                    "Unexpected error waiting on completion event "
                    "(request=%s); decrementing inflight anyway",
                    request.id,
                )
            finally:
                # Decrement inflight tracking even when the wait failed —
                # otherwise this model's counter is stuck above its true
                # value and the scheduler refuses to evict it.
                if _inflight_lock is not None:
                    try:
                        async with _inflight_lock:
                            count = _inflight_models.get(request.model, 1) - 1
                            if count <= 0:
                                _inflight_models.pop(request.model, None)
                            else:
                                _inflight_models[request.model] = count
                    except Exception:
                        logger.exception(
                            "Failed to decrement _inflight_models for request=%s",
                            request.id,
                        )
                # Wake the scheduler so it can dispatch queued same-model requests
                # immediately instead of waiting for the next loop_interval timeout.
                # (Fix for issue #3: see reference/QUEUE_STALENESS_INVESTIGATION.md)
                if _scheduler:
                    try:
                        _scheduler.notify()
                    except Exception:
                        logger.exception(
                            "Failed to notify scheduler after cleanup (request=%s)",
                            request.id,
                        )

        # Fire-and-forget: track completion in background
        asyncio.create_task(_cleanup_inflight(), name=f"inflight-cleanup-{request.id}")


async def _startup_self_test(config: BrokerConfig) -> None:
    """Post-startup self-test: verify the proxy pipeline works end-to-end.

    Sends lightweight requests through BASTION's own proxy port to verify
    the full chain (FastAPI -> proxy -> Ollama) is functional. Runs as a
    fire-and-forget task — failures are logged but don't block startup.

    In two-port mode, /broker/health lives on the admin port, so the
    self-test targets the correct endpoint for each mode.
    """
    await asyncio.sleep(1.0)  # Let uvicorn finish binding the port

    proxy_url = f"http://127.0.0.1:{config.server.port}"
    # In two-port mode, /broker/* is on admin_port; otherwise same port
    if config.server.two_port_mode:
        admin_url = f"http://127.0.0.1:{config.server.admin_port}"
    else:
        admin_url = proxy_url

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{admin_url}/broker/health")
            if resp.status_code == 200:
                health = resp.json()
                logger.info(
                    "Self-test: health OK (healthy=%s, scheduler=%s)",
                    health.get("healthy"), health.get("scheduler_running"),
                )
            else:
                logger.warning("Self-test: /broker/health returned %d", resp.status_code)

            resp = await client.get(f"{proxy_url}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                logger.info(
                    "Self-test: proxy passthrough OK (%d models via /api/tags)",
                    len(models),
                )
            else:
                logger.warning(
                    "Self-test: /api/tags returned %d — Ollama may be down",
                    resp.status_code,
                )
        logger.info("Self-test: all checks passed — BASTION fully operational")
    except Exception as e:
        logger.warning("Self-test failed: %s — check Ollama connectivity", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Manage proxy, tracker, queue, and scheduler lifecycle."""
    global _proxy, _vram_tracker, _vram_manager, _queue, _scheduler
    global _a2a_handler, _a2a_http_client, _start_time, _reset_epoch

    config: BrokerConfig = app.state.config

    # Initialize audit logger (tier from config, path from bastion.paths)
    from bastion.paths import audit_log_path as _audit_path

    _resolved_audit = _audit_path()
    audit.init_audit_logger(
        log_path=_resolved_audit,
        max_bytes=10 * 1024 * 1024,
        backup_count=5,
        tier=config.audit.tier,
    )
    logger.info("Audit logging initialized: %s (tier=%d)", _resolved_audit, config.audit.tier)
    from bastion.paths import harden_audit_log
    harden_audit_log()

    # --- Optional SQLite persistence (Phase 3.2) ---
    _db_manager = None
    if config.persistence.enabled:
        try:
            from bastion.persistence import (
                DatabaseManager,
                PersistentAuditLog,
                PersistentQueue,
                PersistentTaskStore,
            )
        except ImportError as exc:
            logger.error(
                "Persistence requires aiosqlite. "
                "Install with: pip install bastion-broker[persistence]"
            )
            raise SystemExit(1) from exc

        # Resolve database path
        if config.persistence.database_path:
            db_path = config.persistence.database_path
        else:
            from bastion.paths import database_path as _default_db_path
            db_path = str(_default_db_path())

        try:
            _db_manager = DatabaseManager(db_path)
            await _db_manager.open()
            logger.info("Persistence database opened: %s", db_path)
        except Exception as e:
            logger.error("SQLite open failed: %s — falling back to in-memory", e)
            _db_manager = None
            config.persistence.enabled = False

    # Pre-flight: check Ollama connectivity (non-blocking, warn-only)
    try:
        async with httpx.AsyncClient(timeout=5.0) as preflight:
            resp = await preflight.get(f"{config.ollama.base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            tag_count = len(data.get("models", []))
            if tag_count == 0:
                logger.info(
                    "Ollama reachable at %s but has no models installed. "
                    "Pull models from https://ollama.com/library — e.g.: "
                    "ollama pull llama3.1:8b",
                    config.ollama.base_url,
                )
            else:
                logger.info(
                    "Ollama pre-flight OK: %s reachable (%d model%s available)",
                    config.ollama.base_url, tag_count, "" if tag_count == 1 else "s",
                )
    except httpx.ConnectError:
        logger.warning(
            "Cannot connect to Ollama at %s. Is Ollama running? "
            "Start it with: OLLAMA_HOST=127.0.0.1:%d ollama serve",
            config.ollama.base_url, config.ollama.port,
        )
    except Exception as e:
        logger.warning(
            "Ollama pre-flight failed: %s \u2014 BASTION will start anyway, "
            "but proxy requests will fail until Ollama is available. Error: %s",
            config.ollama.base_url, e,
        )

    _vram_tracker = VRAMTracker(config)

    # Create VRAMManager (VRAM ledger with assume/confirm/forget pattern)
    total_vram_bytes = int(config.gpu.total_vram_gb * 1024 * 1024 * 1024)
    _vram_manager = VRAMManager(_vram_tracker, total_vram_bytes, safety_margin_pct=10.0)

    _queue = AffinityQueue(config.scheduler)

    # Wrap audit logger with persistence if enabled
    if (
        _db_manager
        and config.persistence.enabled
        and config.persistence.persist_audit
        and audit._audit_logger is not None
    ):
        audit._audit_logger = PersistentAuditLog(audit._audit_logger, _db_manager)
        logger.info("Audit persistence enabled (dual-write JSONL+SQLite)")

    # Wrap queue with persistence if enabled
    if _db_manager and config.persistence.enabled and config.persistence.persist_queue:
        _queue = PersistentQueue(_queue, _db_manager)
        recovered, discarded = await _queue.hydrate(config.persistence.queue_recovery_ttl)
        logger.info("Queue persistence enabled: recovered=%d, discarded=%d", recovered, discarded)

    # Initialize thrashing detector (M58)
    global _thrashing_detector
    _thrashing_detector = ThrashingDetector(config.thrashing_detection)

    _proxy = OllamaProxy(
        config,
        enqueue_fn=_enqueue_request,
        record_fn=record_recent_request,
        intent_lookup_fn=_lookup_intent,
        thrashing_detector=_thrashing_detector,
    )

    # Create scheduler (reservation callback set later if A2A enabled)
    _scheduler = Scheduler(
        config=config,
        queue=_queue,
        vram_tracker=_vram_tracker,
        dispatch_fn=_dispatch_request,
        has_inflight_fn=has_inflight,
        inflight_count_fn=inflight_count,
        vram_manager=_vram_manager,
    )
    _scheduler._dispatch_error_fn = _dispatch_error_cleanup

    # Wire drain visibility into the proxy passthrough handler so /api/embeddings
    # and any other inference-adjacent path that isn't in scheduled_endpoints
    # still respects drain mode (management endpoints in passthrough_endpoints
    # continue to serve).
    if _proxy is not None:
        _proxy._is_draining_fn = lambda: _scheduler is not None and _scheduler.is_draining
    _start_time = time.time()
    _reset_epoch = datetime.now(UTC).isoformat()

    # Initialize A2A handler if enabled
    if config.a2a.enabled:
        from bastion.a2a import A2AHandler

        # Build a shared httpx client for A2A Ollama calls.
        # If the proxy has a circuit breaker, wrap the transport so all
        # A2A requests go through the same breaker instance.
        proxy_cb = _proxy.circuit_breaker if _proxy else None
        if proxy_cb:
            a2a_transport = CircuitBreakerTransport(proxy_cb)
            _a2a_http_client = httpx.AsyncClient(
                transport=a2a_transport,
                timeout=httpx.Timeout(
                    config.proxy.inference_timeout_seconds,
                    connect=config.proxy.connect_timeout_seconds,
                ),
            )
            logger.info("A2A shared httpx client created with CircuitBreakerTransport")
        else:
            _a2a_http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    config.proxy.inference_timeout_seconds,
                    connect=config.proxy.connect_timeout_seconds,
                ),
            )
            logger.info("A2A shared httpx client created (no circuit breaker)")

        # Build task store, optionally wrapping with persistence
        from bastion.taskstore import TaskStore as _TaskStore

        _a2a_task_store: _TaskStore | Any = _TaskStore(
            maxsize=10_000,
            task_ttl_seconds=config.a2a.task_ttl_seconds,
            completed_ttl_seconds=config.a2a.task_ttl_seconds,
        )
        if _db_manager and config.persistence.enabled and config.persistence.persist_tasks:
            _a2a_task_store = PersistentTaskStore(_a2a_task_store, _db_manager)
            recovered = await _a2a_task_store.hydrate()
            logger.info("Task persistence enabled: recovered=%d active tasks", recovered)

        _a2a_handler = A2AHandler(
            config=config,
            enqueue_fn=_enqueue_request,
            vram_tracker=_vram_tracker,
            scheduler=_scheduler,
            circuit_breaker=proxy_cb,
            http_client=_a2a_http_client,
            task_store=_a2a_task_store,
        )
        # Wire up reservation check callback to scheduler
        _scheduler._reservation_check_fn = _a2a_handler.has_active_reservation
        logger.info("A2A interface enabled")

    await _scheduler.start()
    logger.info("BASTION started — proxying to %s", config.ollama.base_url)

    # Start queue TTL sweep background task
    global _sweep_task
    ttl = config.scheduler.queue_ttl_seconds
    _sweep_task = asyncio.create_task(
        _queue_sweep_loop(ttl), name="bastion-queue-sweep"
    )
    logger.info("Queue sweep started (TTL=%.0fs, interval=60s)", ttl)

    # Construct the correlation engine + discrete-contention detector AFTER the
    # scheduler/vram_tracker/vram_manager are built and BEFORE the snapshot loop
    # starts (spec 6.6). Deps are scheduler/vram only — NOT _a2a_handler, so an
    # A2A-disabled deployment is fully functional. The engine is PASSIVE: its
    # tick() runs inside _collect_machine_snapshot on the snapshot the loop
    # already assembled (pull, never push) and it owns no background task. The
    # inference emitter PULLS _recent_requests via this provider callable, so the
    # record site (record_recent_request) never imports the engine.
    global _correlation_engine, _contention_detector
    _corr_cfg = config.observability.correlation
    _correlation_engine = CorrelationEngine(
        recent_requests_provider=lambda: list(_recent_requests),
        config=_corr_cfg,
    )
    _contention_detector = ContentionEventDetector(config=_corr_cfg)
    logger.info(
        "Correlation engine started (ring maxlen=%d, contention maxlen=%d)",
        _correlation_engine.ring.maxlen,
        _contention_detector.recent_contentions.maxlen,
    )

    # Start the broker-side machine-snapshot collection loop (observability
    # spec 4.9). The broker owns collection so /broker/snapshot works headless;
    # the TUI is a client that polls it (ADR-005).
    global _machine_snapshot_task
    _machine_snapshot_task = asyncio.create_task(
        _machine_snapshot_loop(), name="bastion-machine-snapshot"
    )
    logger.info(
        "Machine snapshot loop started (interval=%.1fs)",
        config.observability.snapshot_interval_s,
    )

    # Start process monitor (Ollama health + GPU lockup detection)
    global _process_monitor
    _process_monitor = ProcessMonitor(
        ollama_url=config.ollama.base_url,
        check_interval=10.0,
        ollama_timeout=config.ollama.api_timeout_seconds,
        gpu_timeout=config.gpu.nvidia_smi_timeout_seconds,
        failure_threshold=3,
        on_unhealthy=_scheduler.drain if _scheduler else None,
        on_healthy=_scheduler.resume if _scheduler else None,
    )
    await _process_monitor.start()

    # Systemd watchdog: signal READY and start heartbeating
    init_watchdog()
    notify_ready()
    notify_status(f"proxying to {config.ollama.base_url}")

    # Fire-and-forget startup self-test
    asyncio.create_task(_startup_self_test(config), name="bastion-self-test")

    yield

    # Shutdown
    notify_stopping()
    logger.info("BASTION shutting down...")
    if _sweep_task:
        _sweep_task.cancel()
        _sweep_task = None
    if _machine_snapshot_task:
        _machine_snapshot_task.cancel()
        _machine_snapshot_task = None
    if _process_monitor:
        await _process_monitor.stop()
    if _scheduler:
        await _scheduler.stop()
    # Unblock any proxy handlers waiting for grant or completion events
    for event in _pending_grants.values():
        event.set()
    _pending_grants.clear()
    for event in _pending_completions.values():
        event.set()
    _pending_completions.clear()
    _inflight_models.clear()
    if _a2a_http_client:
        await _a2a_http_client.aclose()
    if _db_manager:
        await _db_manager.close()
        logger.info("Persistence database closed")
    if _proxy:
        await _proxy.close()
    if _vram_tracker:
        await _vram_tracker.close()


def _find_rate_limiter(app: Any) -> RateLimitMiddleware | None:
    """Walk the built ASGI middleware stack and return the rate limiter.

    The shed path needs the live :class:`RateLimitMiddleware` instance to
    drain the offending caller's bucket (admission coupling). ``add_middleware``
    does not hand back the instance, so we locate it by walking the
    ``middleware_stack`` chain (each node exposes its inner app via ``.app``).
    """
    node = getattr(app, "middleware_stack", None)
    for _ in range(64):  # bounded — the stack is short; never loop forever
        if node is None:
            break
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


async def _funnel_preload(request: Request, config: BrokerConfig) -> Any:
    """Shared body for BOTH ``/broker/preload`` routes (SRV1).

    The admin preload is a residency-INCREASING load path and must therefore
    pass through the SAME non-skippable chokepoint as the scheduler swap:
    the scheduler-owned load serializer, with the swap brake's authoritative
    ``acquire()`` + ``record_load()`` running INSIDE it. A direct
    ``keep_alive:-1`` load that bypassed the serializer would be exactly the
    unbounded swap-velocity hole the brake exists to close.
    """
    body = await request.json()
    model = body.get("model")
    if not model:
        return JSONResponse({"error": "Missing 'model' field"}, status_code=400)

    if not _vram_tracker:
        return JSONResponse({"error": "VRAM tracker not initialized"}, status_code=503)

    # None-guard: the serializer + brake live on the scheduler. Without it we
    # MUST shed (503) rather than fall through to a direct, ungated load.
    if _scheduler is None:
        return JSONResponse(
            {"error": "Scheduler not initialized", "reason_code": "scheduler_unavailable"},
            status_code=503,
        )

    # Cheap pre-check before queueing on the serializer (fail fast on no-fit).
    can_load, reason = await _vram_tracker.can_load_model(model)
    if not can_load:
        return JSONResponse({"error": reason, "reason_code": "vram_no_fit"}, status_code=409)

    brake = _scheduler.swap_brake

    # THE single chokepoint: hold the load serializer across gate + load.
    async with _scheduler.load_serializer:
        # Authoritative brake gate INSIDE the serializer (closes the TOCTOU:
        # only one task holds the serializer, so its record_load wins the race).
        decision = brake.acquire(model)
        if decision.action != "proceed":
            # Admission coupling: throttle this caller so a client that ignores
            # Retry-After (the stress calibrator will) cannot hot-retry the
            # shed path into a CPU busy-loop.
            limiter = _find_rate_limiter(request.app)
            if limiter is not None:
                caller = limiter._get_client_ip(request)
                limiter.throttle(caller, model)
            retry_after = max(1, int(decision.retry_after_s) + 1)
            return JSONResponse(
                {
                    "error": f"swap brake {decision.action}: {decision.reason}",
                    "reason_code": f"swap_brake_{decision.action}",
                },
                status_code=503,
                headers={"Retry-After": str(retry_after)},
            )

        # Re-check fit INSIDE the serializer — another load may have landed
        # while we awaited the semaphore (the fit TOCTOU the spec calls out).
        can_load, reason = await _vram_tracker.can_load_model(model)
        if not can_load:
            return JSONResponse({"error": reason, "reason_code": "vram_no_fit"}, status_code=409)

        # Trigger the cold load via a minimal generate request.
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{config.ollama.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": -1,  # Keep loaded indefinitely
                    "options": {"use_mmap": False},
                },
            )
        # Debit the brake token at the true GPU-I/O point.
        brake.record_load(model)

    return {"status": "loaded", "model": model}


def _embed_brake_snapshot(result: dict[str, Any], loaded: list[Any]) -> None:
    """Embed the swap-brake snapshot into a ``/broker/status`` result dict (SRV2).

    Populates the ``BrokerStatus`` brake fields (3am visibility). ``pinned_*``
    are fused from the VRAM tracker's pin set and the already-fetched loaded
    model list, so no extra GPU query is incurred.
    """
    if _scheduler is None:
        return
    snap = _scheduler.swap_brake.snapshot()
    result["brake_state"] = str(snap["state"])
    result["brake_reason"] = snap["reason"]
    result["cooloff_remaining_s"] = snap["cooloff_remaining_s"]
    result["windowed_rate_per_min"] = snap["windowed_rate_per_min"]
    result["backoff_level"] = snap["backoff_level"]
    # The VRAMManager hardware gate is the authoritative blind signal; fall
    # back to the brake's own degraded flag when the manager is absent.
    if _vram_manager is not None:
        result["hardware_gate_blind"] = _vram_manager.hardware_gate_blind
    else:
        result["hardware_gate_blind"] = snap["hardware_gate_blind"]

    pinned = sorted(_vram_tracker._pinned) if _vram_tracker is not None else []
    result["pinned_models"] = pinned
    pinned_set = set(pinned)
    result["pinned_vram_gb"] = round(
        sum(getattr(m, "vram_gb", 0.0) for m in loaded if getattr(m, "name", None) in pinned_set),
        3,
    )


async def _force_swap_brake(request: Request) -> Any:
    """Shared body for BOTH ``POST /broker/swap-brake`` admin routes (SRV2).

    Maps ``{release: bool, ttl_s: float}`` to ``SwapBrake.force`` — an
    auto-expiring override (force-release cannot be silently left on). Separate
    from ``/drain``. Emits exactly one audit event per engage/release.
    """
    if _scheduler is None:
        return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)
    body = await request.json()
    release = bool(body.get("release", False))
    try:
        ttl_s = float(body.get("ttl_s", 0.0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl_s must be a number"}, status_code=400)
    if ttl_s < 0:
        return JSONResponse({"error": "ttl_s must be >= 0"}, status_code=400)

    brake = _scheduler.swap_brake
    brake.force(release=release, ttl_s=ttl_s)
    action = "force_release" if release else "force_engage"
    audit.emit("swap_brake_override", {"action": action, "ttl_s": ttl_s})
    return {"status": action, "ttl_s": ttl_s, "snapshot": brake.snapshot()}


def create_app(config: BrokerConfig) -> FastAPI:
    """Create and configure the FastAPI application."""
    global _config
    _config = config

    app = FastAPI(
        title="BASTION",
        description="Batch Affinity Scheduler for Throttled Inference on Ollama Networks",
        version=bastion.__version__,
        lifespan=lifespan,
        # Don't show docs on the standard Ollama port (would confuse clients)
        docs_url="/broker/docs",
        redoc_url="/broker/redoc",
        openapi_url="/broker/openapi.json",
    )
    app.state.config = config

    # Add middleware (order matters: outermost first)
    # Rate limiting per client IP
    app.add_middleware(RateLimitMiddleware, config=config.rate_limit)
    # Metrics recording
    app.add_middleware(MetricsMiddleware)

    # ── Auth dependencies from config ────────────────────────────────
    verify_admin = make_admin_key_dependency(config.auth)
    verify_a2a = make_a2a_token_dependency(config.a2a)

    # Create routers with auth dependencies
    broker_router = APIRouter(prefix="/broker", dependencies=[Depends(verify_admin)])
    a2a_router = APIRouter(prefix="/a2a", dependencies=[Depends(verify_a2a)])

    # ── Admin API routes (/broker/*) ─────────────────────────────────

    @broker_router.get("/status")
    async def broker_status() -> dict[str, Any]:
        """Get broker status: queue depth, loaded models, GPU health.

        Returns the base BrokerStatus fields plus additional observability
        fields (total_dispatched, swap_rate_level, stall diagnostics,
        inflight models, circuit breaker state, GPU safety, VRAM budget).
        """
        loaded_raw = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        # State-unknown sentinel (None) coerced to [] so the BrokerStatus
        # contract (loaded_models: list) stays satisfied during outages.
        loaded = loaded_raw if loaded_raw is not None else []
        gpu = await query_gpu_status()
        status = BrokerStatus(
            uptime_seconds=time.time() - _start_time,
            queue_depth=_queue.total_size if _queue else 0,
            queue_by_model=_queue.queue_depth_by_model() if _queue else {},
            loaded_models=loaded,
            vram_state="unknown" if loaded_raw is None else "ok",
            gpu=gpu,
            current_model=_scheduler.current_model if _scheduler else None,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_model_swaps=_scheduler.total_swaps if _scheduler else 0,
            state="draining" if (_scheduler and _scheduler.is_draining) else "running",
            vram_ledger=(await _vram_manager.status()) if _vram_manager else None,
        )
        result = status.model_dump()

        # -- Observability fields (Phase 1: wire hidden data) --

        # T1-02: total_dispatched (distinct from total_requests_served)
        result["total_dispatched"] = _scheduler.total_dispatched if _scheduler else 0

        # T1-03: swap_rate_level (normal / warn / critical)
        result["swap_rate_level"] = _scheduler._swap_rate_level if _scheduler else "normal"

        # T1-04: stall diagnostics
        result["stall_reason"] = _scheduler.stall_reason if _scheduler else ""
        if _scheduler and _scheduler.stall_reason and _scheduler.stall_time > 0:
            result["stall_duration_seconds"] = round(
                time.time() - _scheduler.stall_time, 2,
            )
        else:
            result["stall_duration_seconds"] = None

        # T1-05: inflight models dict
        result["inflight_models"] = dict(_inflight_models)

        # T1-08: circuit breaker state
        if _proxy and hasattr(_proxy, "circuit_breaker") and _proxy.circuit_breaker:
            cb = _proxy.circuit_breaker
            result["circuit_breaker"] = {
                "state": cb.state,
                "consecutive_failures": cb._consecutive_failures,
                "opened_at": cb._opened_at if cb._opened_at > 0 else None,
            }
        else:
            result["circuit_breaker"] = None

        # T1-10: max_vram_gb from config
        result["max_vram_gb"] = config.gpu.max_vram_gb
        result["max_temperature_c"] = config.gpu.max_temperature_c

        # T1-06: gpu_is_safe computed from GPUStatus.is_safe(config.gpu)
        result["gpu_is_safe"] = gpu.is_safe(config.gpu)

        # M58: thrashing detection stats
        if _thrashing_detector:
            result["thrashing_warnings"] = _thrashing_detector.total_warnings
            result["thrashing_halts"] = _thrashing_detector.total_halts

        # A2A + lease snapshot (dashboard panels)
        if _a2a_handler:
            snap = _a2a_handler.get_snapshot()
            result["a2a_summary"] = snap["summary"]
            result["a2a_tasks"] = snap["tasks"]
            result["active_leases"] = snap["leases"]
        # Recent audit events for AuditStreamPanel
        result["recent_audit_events"] = audit.recent_events(10)

        # SRV2: swap-velocity brake snapshot (3am visibility).
        _embed_brake_snapshot(result, loaded)

        return result

    @broker_router.get("/queue")
    async def broker_queue():
        """Detailed queue view with stall diagnostics."""
        if not _queue:
            return {"models": {}, "total": 0}
        result: dict[str, Any] = {
            "models": _queue.queue_depth_by_model(),
            "total": _queue.total_size,
            "pending_grants": len(_pending_grants),
            "inflight": dict(_inflight_models),
            "inflight_total": sum(_inflight_models.values()),
            "scheduler_state": "draining" if (_scheduler and _scheduler.is_draining) else "running",
        }
        if _scheduler:
            result["stall_reason"] = _scheduler.stall_reason
            result["stall_since"] = _scheduler.stall_time if _scheduler.stall_reason else None
            # Compute cooldown remaining
            swap_cooldown = _scheduler._get_swap_cooldown()
            elapsed = time.time() - _scheduler._last_swap_time
            remaining = max(0.0, swap_cooldown - elapsed)
            result["cooldown_remaining"] = round(remaining, 1)
        return result

    @broker_router.get("/health")
    async def broker_health():
        """GPU health check endpoint.

        Uses check_gpu_safe() with configured thresholds (not hardcoded).
        Also reports circuit breaker state when available.
        """
        gpu = await query_gpu_status()
        gpu_safe, reason = await check_gpu_safe(config.gpu)
        result = {
            "healthy": gpu_safe,
            "reason": reason,
            "gpu": gpu.model_dump(),
            "scheduler_running": _scheduler.is_running if _scheduler else False,
        }
        # Include circuit breaker state if proxy is initialized
        if _proxy and hasattr(_proxy, "circuit_breaker") and _proxy.circuit_breaker:
            result["circuit"] = _proxy.circuit_breaker.state
        return result

    @broker_router.get("/vram")
    async def broker_vram():
        """VRAM ledger status from VRAMManager.

        Returns the full assume/confirm/forget ledger state including
        total VRAM, safety margin, allocated, reserved, available bytes,
        and all active reservations.
        """
        if not _vram_manager:
            return JSONResponse({"error": "VRAMManager not initialized"}, status_code=503)
        return await _vram_manager.status()

    # ── Kubernetes-compatible health probes ──────────────────────────

    @broker_router.get("/livez")
    async def broker_livez():
        """Liveness probe — is the process alive and responsive?"""
        return Response(content="ok", media_type="text/plain")

    @broker_router.get("/readyz")
    async def broker_readyz():
        """Readiness probe — is the broker ready to accept traffic?"""
        if not _scheduler or not _scheduler.is_running:
            return Response(content="not ready: scheduler not running",
                            status_code=503, media_type="text/plain")
        if not _proxy:
            return Response(content="not ready: proxy not initialized",
                            status_code=503, media_type="text/plain")
        # Check circuit breaker — if open, we're not ready for proxy traffic
        if (
            hasattr(_proxy, "circuit_breaker")
            and _proxy.circuit_breaker
            and _proxy.circuit_breaker.state == "open"
        ):
            return Response(content="not ready: circuit breaker open",
                            status_code=503, media_type="text/plain")
        return Response(content="ok", media_type="text/plain")

    @broker_router.post("/preload")
    async def broker_preload(request: Request):
        """Pre-load a model into VRAM (funnelled through the brake + serializer)."""
        return await _funnel_preload(request, config)

    @broker_router.post("/unload")
    async def broker_unload(request: Request):
        """Force-unload a model from VRAM with safety checks.

        Routes through the scheduler so in-flight inference and active A2A
        reservations are honoured; returns 409 in those cases instead of
        silently failing to free VRAM.  On success the VRAMManager allocation
        is released so the ledger stays consistent with reality.
        """
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)

        if not _scheduler:
            return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

        status, details = await _scheduler.unload_model_admin(model)
        if status == "unloaded":
            return {"status": "unloaded", **details}
        if status in ("reserved", "inflight"):
            return JSONResponse(
                {"status": "failed", "error": details.get("reason", status), **details},
                status_code=409,
            )
        return JSONResponse(
            {"status": "failed", "error": details.get("reason", "unknown"), **details},
            status_code=500,
        )

    @broker_router.post("/drain")
    async def broker_drain():
        """Enter drain mode: finish current queue, reject new requests."""
        if _scheduler:
            await _scheduler.drain()
            return {"status": "draining", "queue_depth": _queue.total_size if _queue else 0}
        return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

    @broker_router.post("/resume")
    async def broker_resume():
        """Exit drain mode and resume normal scheduling."""
        if _scheduler:
            await _scheduler.resume()
            return {"status": "running"}
        return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

    @broker_router.post("/swap-brake")
    async def broker_swap_brake(request: Request):
        """Auto-expiring admin override of the swap-velocity brake (SRV2).

        Body ``{release: bool, ttl_s: float}`` -> ``SwapBrake.force``. Separate
        from ``/drain``; force-release expires after ``ttl_s`` so the crash
        backstop can never be silently left off.
        """
        return await _force_swap_brake(request)

    @broker_router.get("/metrics")
    async def broker_metrics():
        """Prometheus metrics endpoint (text exposition format).

        Returns 501 Not Implemented if prometheus-client is not installed.
        To enable: pip install bastion-broker[metrics]
        """
        if not PROMETHEUS_AVAILABLE:
            return JSONResponse(
                {
                    "error": "Metrics not available",
                    "details": (
                        "prometheus-client not installed."
                        " Install with: pip install bastion-broker[metrics]"
                    ),
                },
                status_code=501,
            )

        metrics_output = get_metrics_text()
        return Response(
            content=metrics_output,
            media_type=CONTENT_TYPE_LATEST,
        )

    # ── Watchdog status (S11: Process Monitor) ──────────────────────

    @broker_router.get("/watchdog")
    async def broker_watchdog():
        """Process monitor status: Ollama health, GPU responsiveness."""
        if not _process_monitor:
            return JSONResponse({"error": "Process monitor not initialized"}, status_code=503)
        return _process_monitor.status.model_dump()

    # ── Recent Requests (S5: Dashboard Evolution) ──────────────────

    @broker_router.get("/recent")
    async def broker_recent():
        """Return last 50 completed requests for dashboard trace viewer."""
        return list(_recent_requests)

    # ── Counters (WT-C-A-05) ────────────────────────────────────────

    @broker_router.get("/counters")
    async def broker_counters() -> BrokerCounters:
        """Cumulative counters since broker startup with a reset_epoch sentinel.

        Consumers compare ``reset_epoch`` across polls to detect a broker
        restart; any change means all counter deltas must be discarded to
        avoid computing negative-delta rates.
        """
        return BrokerCounters(
            reset_epoch=_reset_epoch,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_dispatched=_scheduler.total_dispatched if _scheduler else 0,
            model_swap_total=_scheduler.total_swaps if _scheduler else 0,
            thrashing_halt_total=(
                _thrashing_detector.total_halts if _thrashing_detector else 0
            ),
        )

    @broker_router.get("/thrashing")
    async def broker_thrashing() -> BrokerThrashing:
        """Per-agent thrashing detector state.

        Returns the worst global verdict and one entry per tracked agent with
        its individual verdict, cooloff_remaining_s, swap_ratio, and last_run_s.
        ``detector_state`` is "OK" when no agents have been tracked yet.

        Verdict mapping from ThrashingVerdict (internal) to API label:
          ok   -> OK
          warn -> WARNED
          halt -> HALTED
        """
        if not _thrashing_detector:
            return BrokerThrashing(detector_state="OK", agents=[])
        snapshots = _thrashing_detector.snapshot()
        agent_entries = [
            BrokerThrashingAgent(
                agent_id=s.agent_id,
                verdict=_THRASHING_VERDICT_LABEL[s.verdict],
                cooloff_remaining_s=s.cooloff_remaining_s,
                swap_ratio=s.swap_ratio,
                last_run_s=s.last_run_s,
            )
            for s in snapshots
        ]
        if agent_entries:
            worst = max(snapshots, key=lambda s: _THRASHING_VERDICT_ORDER[s.verdict])
            global_verdict: ThrashingVerdictLabel = _THRASHING_VERDICT_LABEL[worst.verdict]
        else:
            global_verdict = "OK"
        return BrokerThrashing(detector_state=global_verdict, agents=agent_entries)

    @broker_router.get("/version")
    async def broker_version() -> dict[str, Any]:
        """Build identity for client SHA-pinning during long batches.

        Returns the BASTION version, git SHA, and process boot timestamp.
        A2A clients can pin ``git_sha`` at the start of a long
        batch and refuse to retry against a different SHA — the signal that
        a mid-run redeploy (not a transient infra blip) caused the errors.
        ``boot_time_unix`` distinguishes process restarts at unchanged SHA.
        """
        return {
            "version": bastion.__version__,
            "git_sha": _GIT_SHA,
            "boot_time_unix": _start_time,
            "boot_time_iso": (
                datetime.fromtimestamp(_start_time, UTC).isoformat()
                if _start_time else ""
            ),
        }

    # ── S6: Model Intent API ────────────────────────────────────────

    @broker_router.post("/intent")
    async def broker_intent(request: Request):
        """Declare an upcoming model sequence for scheduler optimization.

        Accepts a profile name (referencing session_profiles in broker.yaml)
        or an ad-hoc model_sequence. Returns the resolved priority tier
        and an intent_id for tracking.
        """
        body = await request.json()

        # Build IntentDeclaration from request body
        declaration = IntentDeclaration(**body)

        # Resolve profile if specified
        if declaration.profile:
            profile = config.session_profiles.get(declaration.profile)
            if profile is None:
                return JSONResponse(
                    {"error": f"Unknown profile: {declaration.profile}",
                     "available_profiles": list(config.session_profiles.keys())},
                    status_code=404,
                )
            # Use profile's model sequence and priority
            resolved_sequence = profile.model_sequence
            resolved_priority = profile.default_priority
        elif declaration.model_sequence:
            resolved_sequence = declaration.model_sequence
            resolved_priority = PriorityTier.AGENT  # default for ad-hoc
        else:
            return JSONResponse(
                {"error": "Must specify either 'profile' or 'model_sequence'"},
                status_code=400,
            )

        # Store the intent and resolved metadata
        _active_intents[declaration.intent_id] = declaration
        _resolved_intents[declaration.intent_id] = (resolved_priority, resolved_sequence)

        # Log the intent
        logger.info(
            "Intent registered: %s (client=%s, models=%s, priority=%s)",
            declaration.intent_id,
            declaration.client_id,
            resolved_sequence,
            resolved_priority.value,
        )

        return IntentResponse(
            intent_id=declaration.intent_id,
            resolved_priority=resolved_priority.value,
            model_sequence=resolved_sequence,
            estimated_requests=declaration.estimated_requests,
        ).model_dump()

    @broker_router.get("/intents")
    async def broker_intents():
        """List all active intent declarations."""
        return {
            "intents": {
                k: v.model_dump() for k, v in _active_intents.items()
            },
            "total": len(_active_intents),
        }

    @broker_router.post("/intent/{intent_id}/complete")
    async def broker_intent_complete(intent_id: str):
        """Mark an intent as completed and remove it."""
        if intent_id not in _active_intents:
            return JSONResponse({"error": "Intent not found"}, status_code=404)
        del _active_intents[intent_id]
        _resolved_intents.pop(intent_id, None)
        logger.info("Intent completed: %s", intent_id)
        return {"status": "completed", "intent_id": intent_id}

    @broker_router.delete("/intent/{intent_id}")
    async def broker_intent_delete(intent_id: str):
        """Cancel/delete an active intent."""
        if intent_id not in _active_intents:
            return JSONResponse({"error": "Intent not found"}, status_code=404)
        del _active_intents[intent_id]
        _resolved_intents.pop(intent_id, None)
        logger.info("Intent deleted: %s", intent_id)
        return {"status": "deleted", "intent_id": intent_id}

    @broker_router.get("/latency")
    async def broker_latency(window_s: float = 300.0) -> BrokerLatency:
        """Per-model latency percentiles over the rolling window.

        Aggregates the ``_recent_requests`` ring buffer. Models with fewer
        than 3 samples in the window are omitted from ``per_model``
        (single-call noise); the ``overall`` bucket aggregates everything.

        Query parameters
        ----------------
        window_s : float
            Default 300.0 (5 min). Clamped to ``[10.0, 3600.0]``.
        """
        window_s = max(10.0, min(3600.0, window_s))
        return aggregate_latency(list(_recent_requests), window_s)

    @broker_router.get("/catalog")
    async def broker_catalog() -> BrokerCatalog:
        """Registered models from broker.yaml enriched with residency state.

        Returns the full ``models:`` dict from broker.yaml, augmented with:
        - ``currently_loaded`` — whether VRAMTracker reports the model in
          Ollama right now
        - ``actual_vram_gb`` — measured VRAM at the snapshot, or null
        - ``is_evictable`` — loaded AND not the scheduler's current model
          AND not always_allowed

        ``snapshot_age_s`` reflects the age of the residency snapshot used
        to build this response (close to zero — a fresh ``/api/ps`` query
        is issued per request). ``is_evictable`` is computed at response
        time and can flip between calls if a swap is in flight.

        When ``/api/ps`` is unreachable, residency information collapses
        to "nothing loaded" rather than raising — the catalog itself
        remains queryable so operators can still see the registry shape
        during an Ollama outage.
        """
        snapshot_ts = time.time()
        loaded_raw = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        loaded = loaded_raw if loaded_raw is not None else []
        # Tag-aware residency: /api/ps reports 'name:latest' for untagged
        # registry keys — normalize the implicit tag on both sides so a
        # resident model isn't shown as not-loaded under a tag mismatch.
        loaded_norm = {m.name.removesuffix(":latest"): m for m in loaded}
        snapshot_age_s = max(0.0, time.time() - snapshot_ts)
        current = _scheduler.current_model if _scheduler else None

        entries: list[CatalogEntry] = []
        for name, info in config.models.items():
            resident = loaded_norm.get(name.removesuffix(":latest"))
            is_loaded = resident is not None
            entries.append(CatalogEntry(
                name=name,
                vram_gb=info.vram_gb,
                default_num_ctx=info.default_num_ctx,
                tags=list(info.tags),
                always_allowed=info.always_allowed,
                currently_loaded=is_loaded,
                actual_vram_gb=resident.vram_gb if resident is not None else None,
                is_evictable=(
                    is_loaded
                    and name != current
                    and not info.always_allowed
                ),
            ))

        return BrokerCatalog(
            models=entries,
            total=len(entries),
            loaded_count=sum(1 for e in entries if e.currently_loaded),
            evictable_count=sum(1 for e in entries if e.is_evictable),
            registry_source=(
                _redact_home(str(config.loaded_from))
                if config.loaded_from else "<unknown>"
            ),
            snapshot_age_s=snapshot_age_s,
            residency_state="unknown" if loaded_raw is None else "ok",
        )

    # ── Machine snapshot (observability spec 4.9/4.10) ──────────────
    # Single-sourced handlers registered in BOTH factories (dual-factory tax,
    # spec 4.10 — there is no shared router; registering once would 404 in the
    # admin-only two-port deployment).
    broker_router.add_api_route("/snapshot", _handle_snapshot, methods=["GET"])
    broker_router.add_api_route(
        "/snapshot/stream", _handle_snapshot_stream, methods=["GET"]
    )
    broker_router.add_api_route("/contention", _handle_contention, methods=["GET"])
    broker_router.add_api_route(
        "/gpu/extended", _handle_gpu_extended, methods=["GET"]
    )
    broker_router.add_api_route("/processes", _handle_processes, methods=["GET"])
    # Correlation engine surfaces (spec 6.4/6.3/7). Ring + enriched stall are
    # FOLDED into /broker/snapshot (no /ring or /stall endpoints); only the
    # composite-risk and discrete-contention surfaces get their own routes.
    broker_router.add_api_route(
        "/correlation/risk", _handle_correlation_risk, methods=["GET"]
    )
    broker_router.add_api_route(
        "/correlation/contentions",
        _handle_correlation_contentions,
        methods=["GET"],
    )

    # ── A2A Interface Routes ────────────────────────────────────────
    # SSE encoding is the shared module-level _sse_wrapper (deduped, spec 5.6).

    @a2a_router.get("/stats")
    async def a2a_stats():
        """Task store statistics for monitoring."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        return _a2a_handler._store.stats()

    @a2a_router.post("/tasks")
    async def a2a_create_task(request: Request):
        """Create a new A2A task (SendMessage equivalent)."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        body = await request.json()
        result = await _a2a_handler.create_task(body)
        return JSONResponse(result, status_code=201)

    @a2a_router.get("/tasks/{task_id}")
    async def a2a_get_task(task_id: str, request: Request):
        """Get task status and results."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        result = await _a2a_handler.get_task(task_id)
        if result is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return result

    @a2a_router.get("/tasks/{task_id}/stream")
    async def a2a_stream_task(task_id: str, request: Request):
        """SSE stream for task status/artifact updates."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        # Check if task exists before subscribing
        if not _a2a_handler._store.has_task(task_id):
            return JSONResponse({"error": "Task not found"}, status_code=404)

        generator = _a2a_handler.subscribe_task(task_id, request=request)
        return StreamingResponse(
            _sse_wrapper(generator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Prevents nginx 16KB buffering
            },
        )

    @a2a_router.delete("/tasks/{task_id}")
    async def a2a_cancel_task(task_id: str, request: Request):
        """Cancel a running task."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        success = await _a2a_handler.cancel_task(task_id)
        if not success:
            if _a2a_handler.is_task_terminal(task_id):
                return JSONResponse(
                    {"error": "Task already in terminal state (not cancelable)"},
                    status_code=409,
                )
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return {"status": "canceled", "task_id": task_id}

    # ── Lease Management (Hybrid Lease Model) ───────────────────────

    @a2a_router.post("/leases/{lease_id}/heartbeat")
    async def a2a_lease_heartbeat(lease_id: str, request: Request):
        """Touch a lease to keep it alive (implicit heartbeat via request activity)."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        body = await request.json()
        fencing_token = body.get("fencing_token")

        if fencing_token is None:
            return JSONResponse({"error": "Missing fencing_token"}, status_code=400)

        valid, reason = _a2a_handler.validate_lease(lease_id, fencing_token)
        if not valid:
            return JSONResponse({"error": reason}, status_code=409)

        # Touch the lease
        lease = _a2a_handler._leases.get(lease_id)
        if lease:
            lease.touch()
            return {
                "lease_id": lease_id,
                "remaining_requests": lease.remaining_requests,
                "state": lease.state.value,
            }
        return JSONResponse({"error": "Lease not found"}, status_code=404)

    @a2a_router.delete("/leases/{lease_id}")
    async def a2a_release_lease(lease_id: str, request: Request):
        """Explicitly release a model lease."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        success = _a2a_handler.release_lease(lease_id)
        if not success:
            return JSONResponse({"error": "Lease not found"}, status_code=404)
        return {"status": "released", "lease_id": lease_id}

    # ── Agent Card (A2A Protocol) -- Three-Tier Disclosure ─────────

    @app.get("/.well-known/agent-card.json")
    async def agent_card():
        """Tier 1: Public Agent Card — no auth required.

        Returns a stripped-down card with generic info only:
        agent name, protocol version, broad skill categories,
        and authentication requirements. NO model names, NO VRAM
        data, NO queue depth, NO GPU hardware info.
        """
        if _a2a_handler:
            return _a2a_handler.build_public_card()

        # Static fallback (A2A disabled) — also stripped down
        return {
            "name": "BASTION GPU Inference Broker",
            "description": "GPU inference broker with scheduling, batching, and model management",
            "version": bastion.__version__,
            "serviceEndpoint": f"http://localhost:{config.server.port}/a2a",
            "protocolVersion": "0.1",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
            },
            "skills": [
                "text-generation",
                "embeddings",
            ],
            "securitySchemes": {
                "BearerToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A2A bearer token for task and extended card endpoints",
                }
            },
            "security": [{"BearerToken": []}],
        }

    # ── Tier 2: Extended Card (authenticated A2A agents) ────────────

    @a2a_router.get("/extended-card")
    async def a2a_extended_card():
        """Tier 2: Extended Agent Card — A2A auth required.

        Returns detailed capability info for authenticated agents:
        specific model families, capability parameters, availability
        status, supported model list. Protected by A2A token auth.
        """
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)

        return await _a2a_handler.build_extended_card()

    # ── Ollama proxy routes (catch-all) ──────────────────────────────
    # These MUST be last so /broker/* and /.well-known/* match first

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
        dependencies=[Depends(verify_admin)],
    )
    async def proxy_ollama(request: Request, path: str):
        """Proxy all /api/* requests to Ollama backend."""
        if not _proxy:
            return JSONResponse({"error": "Proxy not initialized"}, status_code=503)
        return await _proxy.handle_request(request)

    # OpenAI-compatible endpoints — Ollama serves /v1/chat/completions,
    # /v1/completions, /v1/embeddings, /v1/models. Passthrough to Ollama.
    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
        dependencies=[Depends(verify_admin)],
    )
    async def proxy_ollama_v1(request: Request, path: str):
        """Proxy all /v1/* requests to Ollama backend (OpenAI-compatible API)."""
        if not _proxy:
            return JSONResponse({"error": "Proxy not initialized"}, status_code=503)
        return await _proxy.handle_request(request)

    # Root endpoint — Ollama returns "Ollama is running"
    # HEAD is needed because the ollama CLI sends HEAD / to check connectivity
    @app.api_route("/", methods=["GET", "HEAD"])
    async def root():
        """Mimic Ollama's root response (some clients check this)."""
        return "Ollama is running"

    # ── Include routers ─────────────────────────────────────────────
    app.include_router(broker_router)
    app.include_router(a2a_router)

    return app


# ── Two-port mode: separate proxy and admin apps ────────────────
# When config.server.two_port_mode is True, BASTION runs two uvicorn
# servers sharing the same module-level state (scheduler, queue, VRAM).


def create_proxy_app(config: BrokerConfig) -> FastAPI:
    """Create the proxy-only FastAPI app for port 11434 (Ollama-compatible).

    Contains only:
      - /api/* proxy routes (transparent passthrough)
      - / root endpoint (Ollama compatibility)
      - Rate limiting per IP
      - Metrics middleware

    The lifespan is attached here since the proxy app owns the scheduler,
    queue, VRAM tracker, and other shared state.

    Parameters
    ----------
    config : BrokerConfig
        Validated broker configuration.

    Returns
    -------
    FastAPI
        Proxy-only application.
    """
    global _config
    _config = config

    app = FastAPI(
        title="BASTION Proxy",
        description="Ollama-compatible proxy (two-port mode)",
        version=bastion.__version__,
        lifespan=lifespan,
        # No docs on the proxy port — pure Ollama compatibility
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config

    # Middleware: rate limiting + metrics
    app.add_middleware(RateLimitMiddleware, config=config.rate_limit)
    app.add_middleware(MetricsMiddleware)

    # ── Auth dependency ─────────────────────────────────────────────
    verify_admin = make_admin_key_dependency(config.auth)

    # ── Ollama proxy routes ─────────────────────────────────────────

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
        dependencies=[Depends(verify_admin)],
    )
    async def proxy_ollama(request: Request, path: str) -> Response:
        """Proxy all /api/* requests to Ollama backend."""
        if not _proxy:
            return JSONResponse({"error": "Proxy not initialized"}, status_code=503)
        return await _proxy.handle_request(request)

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
        dependencies=[Depends(verify_admin)],
    )
    async def proxy_ollama_v1(request: Request, path: str) -> Response:
        """Proxy all /v1/* requests to Ollama backend (OpenAI-compatible API)."""
        if not _proxy:
            return JSONResponse({"error": "Proxy not initialized"}, status_code=503)
        return await _proxy.handle_request(request)

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> str:
        """Mimic Ollama's root response (some clients check this)."""
        return "Ollama is running"

    return app


@asynccontextmanager
async def _admin_lifespan(app: FastAPI) -> AsyncGenerator:
    """No-op lifespan for the admin app.

    All shared state (scheduler, queue, VRAM) is managed by the proxy
    app's lifespan. The admin app simply reads that module-level state.
    """
    yield


def create_admin_app(config: BrokerConfig) -> FastAPI:
    """Create the admin+A2A FastAPI app for the separate admin port.

    Contains only:
      - /broker/* admin routes (API key auth)
      - /a2a/* A2A task routes (bearer token auth)
      - /.well-known/agent-card.json (public, no auth)
      - Metrics middleware (no proxy-style rate limiting)

    Shares module-level state with the proxy app (scheduler, queue,
    VRAM tracker, A2A handler).

    Parameters
    ----------
    config : BrokerConfig
        Validated broker configuration.

    Returns
    -------
    FastAPI
        Admin-only application.
    """
    app = FastAPI(
        title="BASTION Admin",
        description="Admin + A2A interface (two-port mode)",
        version=bastion.__version__,
        lifespan=_admin_lifespan,
        docs_url="/broker/docs",
        redoc_url="/broker/redoc",
        openapi_url="/broker/openapi.json",
    )
    app.state.config = config

    # Metrics middleware only (no proxy-style rate limiting on admin port)
    app.add_middleware(MetricsMiddleware)

    # ── Auth dependencies ──────────────────────────────────────────
    verify_admin = make_admin_key_dependency(config.auth)
    verify_a2a = make_a2a_token_dependency(config.a2a)

    broker_router = APIRouter(prefix="/broker", dependencies=[Depends(verify_admin)])
    a2a_router = APIRouter(prefix="/a2a", dependencies=[Depends(verify_a2a)])

    # ── Admin API routes (/broker/*) ─────────────────────────────────

    @broker_router.get("/status")
    async def broker_status() -> dict[str, Any]:
        """Get broker status: queue depth, loaded models, GPU health.

        Returns the base BrokerStatus fields plus additional observability
        fields (total_dispatched, swap_rate_level, stall diagnostics,
        inflight models, circuit breaker state, GPU safety, VRAM budget).
        """
        loaded_raw = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        # State-unknown sentinel (None) coerced to [] so the BrokerStatus
        # contract (loaded_models: list) stays satisfied during outages.
        loaded = loaded_raw if loaded_raw is not None else []
        gpu = await query_gpu_status()
        status = BrokerStatus(
            uptime_seconds=time.time() - _start_time,
            queue_depth=_queue.total_size if _queue else 0,
            queue_by_model=_queue.queue_depth_by_model() if _queue else {},
            loaded_models=loaded,
            vram_state="unknown" if loaded_raw is None else "ok",
            gpu=gpu,
            current_model=_scheduler.current_model if _scheduler else None,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_model_swaps=_scheduler.total_swaps if _scheduler else 0,
            state="draining" if (_scheduler and _scheduler.is_draining) else "running",
            vram_ledger=(await _vram_manager.status()) if _vram_manager else None,
        )
        result = status.model_dump()

        # -- Observability fields (Phase 1: wire hidden data) --

        # T1-02: total_dispatched (distinct from total_requests_served)
        result["total_dispatched"] = _scheduler.total_dispatched if _scheduler else 0

        # T1-03: swap_rate_level (normal / warn / critical)
        result["swap_rate_level"] = _scheduler._swap_rate_level if _scheduler else "normal"

        # T1-04: stall diagnostics
        result["stall_reason"] = _scheduler.stall_reason if _scheduler else ""
        if _scheduler and _scheduler.stall_reason and _scheduler.stall_time > 0:
            result["stall_duration_seconds"] = round(
                time.time() - _scheduler.stall_time, 2,
            )
        else:
            result["stall_duration_seconds"] = None

        # T1-05: inflight models dict
        result["inflight_models"] = dict(_inflight_models)

        # T1-08: circuit breaker state
        if _proxy and hasattr(_proxy, "circuit_breaker") and _proxy.circuit_breaker:
            cb = _proxy.circuit_breaker
            result["circuit_breaker"] = {
                "state": cb.state,
                "consecutive_failures": cb._consecutive_failures,
                "opened_at": cb._opened_at if cb._opened_at > 0 else None,
            }
        else:
            result["circuit_breaker"] = None

        # T1-10: max_vram_gb from config
        result["max_vram_gb"] = config.gpu.max_vram_gb
        result["max_temperature_c"] = config.gpu.max_temperature_c

        # T1-06: gpu_is_safe computed from GPUStatus.is_safe(config.gpu)
        result["gpu_is_safe"] = gpu.is_safe(config.gpu)

        # M58: thrashing detection stats
        if _thrashing_detector:
            result["thrashing_warnings"] = _thrashing_detector.total_warnings
            result["thrashing_halts"] = _thrashing_detector.total_halts

        # A2A + lease snapshot (dashboard panels)
        if _a2a_handler:
            snap = _a2a_handler.get_snapshot()
            result["a2a_summary"] = snap["summary"]
            result["a2a_tasks"] = snap["tasks"]
            result["active_leases"] = snap["leases"]
        # Recent audit events for AuditStreamPanel
        result["recent_audit_events"] = audit.recent_events(10)

        # SRV2: swap-velocity brake snapshot (3am visibility).
        _embed_brake_snapshot(result, loaded)

        return result

    @broker_router.get("/queue")
    async def broker_queue():
        """Detailed queue view with stall diagnostics."""
        if not _queue:
            return {"models": {}, "total": 0}
        result: dict[str, Any] = {
            "models": _queue.queue_depth_by_model(),
            "total": _queue.total_size,
            "pending_grants": len(_pending_grants),
            "inflight": dict(_inflight_models),
            "inflight_total": sum(_inflight_models.values()),
            "scheduler_state": "draining" if (_scheduler and _scheduler.is_draining) else "running",
        }
        if _scheduler:
            result["stall_reason"] = _scheduler.stall_reason
            result["stall_since"] = _scheduler.stall_time if _scheduler.stall_reason else None
            swap_cooldown = _scheduler._get_swap_cooldown()
            elapsed = time.time() - _scheduler._last_swap_time
            remaining = max(0.0, swap_cooldown - elapsed)
            result["cooldown_remaining"] = round(remaining, 1)
        return result

    @broker_router.get("/health")
    async def broker_health():
        """GPU health check endpoint."""
        gpu = await query_gpu_status()
        gpu_safe, reason = await check_gpu_safe(config.gpu)
        result = {
            "healthy": gpu_safe,
            "reason": reason,
            "gpu": gpu.model_dump(),
            "scheduler_running": _scheduler.is_running if _scheduler else False,
        }
        if _proxy and hasattr(_proxy, "circuit_breaker") and _proxy.circuit_breaker:
            result["circuit"] = _proxy.circuit_breaker.state
        return result

    @broker_router.get("/vram")
    async def broker_vram():
        """VRAM ledger status from VRAMManager."""
        if not _vram_manager:
            return JSONResponse({"error": "VRAMManager not initialized"}, status_code=503)
        return await _vram_manager.status()

    @broker_router.get("/livez")
    async def broker_livez():
        """Liveness probe."""
        return Response(content="ok", media_type="text/plain")

    @broker_router.get("/readyz")
    async def broker_readyz():
        """Readiness probe."""
        if not _scheduler or not _scheduler.is_running:
            return Response(content="not ready: scheduler not running",
                            status_code=503, media_type="text/plain")
        if not _proxy:
            return Response(content="not ready: proxy not initialized",
                            status_code=503, media_type="text/plain")
        if (
            hasattr(_proxy, "circuit_breaker")
            and _proxy.circuit_breaker
            and _proxy.circuit_breaker.state == "open"
        ):
            return Response(content="not ready: circuit breaker open",
                            status_code=503, media_type="text/plain")
        return Response(content="ok", media_type="text/plain")

    @broker_router.post("/preload")
    async def broker_preload(request: Request):
        """Pre-load a model into VRAM (funnelled through the brake + serializer)."""
        return await _funnel_preload(request, config)

    @broker_router.post("/unload")
    async def broker_unload(request: Request):
        """Force-unload a model from VRAM with safety checks (admin app).

        See create_app's broker_unload for the contract.  Routes through the
        scheduler so in-flight inference and active A2A reservations are
        honoured.
        """
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)

        if not _scheduler:
            return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

        status, details = await _scheduler.unload_model_admin(model)
        if status == "unloaded":
            return {"status": "unloaded", **details}
        if status in ("reserved", "inflight"):
            return JSONResponse(
                {"status": "failed", "error": details.get("reason", status), **details},
                status_code=409,
            )
        return JSONResponse(
            {"status": "failed", "error": details.get("reason", "unknown"), **details},
            status_code=500,
        )

    @broker_router.post("/drain")
    async def broker_drain():
        """Enter drain mode."""
        if _scheduler:
            await _scheduler.drain()
            return {"status": "draining", "queue_depth": _queue.total_size if _queue else 0}
        return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

    @broker_router.post("/resume")
    async def broker_resume():
        """Exit drain mode."""
        if _scheduler:
            await _scheduler.resume()
            return {"status": "running"}
        return JSONResponse({"error": "Scheduler not initialized"}, status_code=503)

    @broker_router.post("/swap-brake")
    async def broker_swap_brake(request: Request):
        """Auto-expiring admin override of the swap-velocity brake (admin app, SRV2)."""
        return await _force_swap_brake(request)

    @broker_router.get("/metrics")
    async def broker_metrics():
        """Prometheus metrics endpoint."""
        if not PROMETHEUS_AVAILABLE:
            return JSONResponse(
                {
                    "error": "Metrics not available",
                    "details": (
                        "prometheus-client not installed."
                        " Install with: pip install bastion-broker[metrics]"
                    ),
                },
                status_code=501,
            )
        metrics_output = get_metrics_text()
        return Response(content=metrics_output, media_type=CONTENT_TYPE_LATEST)

    @broker_router.get("/watchdog")
    async def broker_watchdog():
        """Process monitor status: Ollama health, GPU responsiveness."""
        if not _process_monitor:
            return JSONResponse({"error": "Process monitor not initialized"}, status_code=503)
        return _process_monitor.status.model_dump()

    @broker_router.get("/recent")
    async def broker_recent():
        """Return last 50 completed requests for dashboard trace viewer."""
        return list(_recent_requests)

    # ── Counters (WT-C-A-05) ────────────────────────────────────────

    @broker_router.get("/counters")
    async def broker_counters() -> BrokerCounters:
        """Cumulative counters since broker startup with a reset_epoch sentinel.

        Consumers compare ``reset_epoch`` across polls to detect a broker
        restart; any change means all counter deltas must be discarded to
        avoid computing negative-delta rates.
        """
        return BrokerCounters(
            reset_epoch=_reset_epoch,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_dispatched=_scheduler.total_dispatched if _scheduler else 0,
            model_swap_total=_scheduler.total_swaps if _scheduler else 0,
            thrashing_halt_total=(
                _thrashing_detector.total_halts if _thrashing_detector else 0
            ),
        )

    @broker_router.get("/thrashing")
    async def broker_thrashing_admin() -> BrokerThrashing:
        """Per-agent thrashing detector state (admin app mirror).

        See the main app's /broker/thrashing for full documentation.
        Verdict mapping: ok -> OK, warn -> WARNED, halt -> HALTED.
        """
        if not _thrashing_detector:
            return BrokerThrashing(detector_state="OK", agents=[])
        snapshots = _thrashing_detector.snapshot()
        agent_entries = [
            BrokerThrashingAgent(
                agent_id=s.agent_id,
                verdict=_THRASHING_VERDICT_LABEL[s.verdict],
                cooloff_remaining_s=s.cooloff_remaining_s,
                swap_ratio=s.swap_ratio,
                last_run_s=s.last_run_s,
            )
            for s in snapshots
        ]
        if agent_entries:
            worst = max(snapshots, key=lambda s: _THRASHING_VERDICT_ORDER[s.verdict])
            global_verdict: ThrashingVerdictLabel = _THRASHING_VERDICT_LABEL[worst.verdict]
        else:
            global_verdict = "OK"
        return BrokerThrashing(detector_state=global_verdict, agents=agent_entries)

    @broker_router.get("/version")
    async def broker_version() -> dict[str, Any]:
        """Build identity for client SHA-pinning during long batches.

        Returns the BASTION version, git SHA, and process boot timestamp.
        A2A clients can pin ``git_sha`` at the start of a long
        batch and refuse to retry against a different SHA — the signal that
        a mid-run redeploy (not a transient infra blip) caused the errors.
        ``boot_time_unix`` distinguishes process restarts at unchanged SHA.
        """
        return {
            "version": bastion.__version__,
            "git_sha": _GIT_SHA,
            "boot_time_unix": _start_time,
            "boot_time_iso": (
                datetime.fromtimestamp(_start_time, UTC).isoformat()
                if _start_time else ""
            ),
        }

    @broker_router.post("/intent")
    async def broker_intent(request: Request):
        """Declare an upcoming model sequence for scheduler optimization."""
        body = await request.json()
        declaration = IntentDeclaration(**body)
        if declaration.profile:
            profile = config.session_profiles.get(declaration.profile)
            if profile is None:
                return JSONResponse(
                    {"error": f"Unknown profile: {declaration.profile}",
                     "available_profiles": list(config.session_profiles.keys())},
                    status_code=404,
                )
            resolved_sequence = profile.model_sequence
            resolved_priority = profile.default_priority
        elif declaration.model_sequence:
            resolved_sequence = declaration.model_sequence
            resolved_priority = PriorityTier.AGENT
        else:
            return JSONResponse(
                {"error": "Must specify either 'profile' or 'model_sequence'"},
                status_code=400,
            )
        _active_intents[declaration.intent_id] = declaration
        _resolved_intents[declaration.intent_id] = (resolved_priority, resolved_sequence)
        logger.info(
            "Intent registered: %s (client=%s, models=%s, priority=%s)",
            declaration.intent_id, declaration.client_id,
            resolved_sequence, resolved_priority.value,
        )
        return IntentResponse(
            intent_id=declaration.intent_id,
            resolved_priority=resolved_priority.value,
            model_sequence=resolved_sequence,
            estimated_requests=declaration.estimated_requests,
        ).model_dump()

    @broker_router.get("/intents")
    async def broker_intents():
        """List all active intent declarations."""
        return {
            "intents": {k: v.model_dump() for k, v in _active_intents.items()},
            "total": len(_active_intents),
        }

    @broker_router.post("/intent/{intent_id}/complete")
    async def broker_intent_complete(intent_id: str):
        """Mark an intent as completed and remove it."""
        if intent_id not in _active_intents:
            return JSONResponse({"error": "Intent not found"}, status_code=404)
        del _active_intents[intent_id]
        _resolved_intents.pop(intent_id, None)
        logger.info("Intent completed: %s", intent_id)
        return {"status": "completed", "intent_id": intent_id}

    @broker_router.delete("/intent/{intent_id}")
    async def broker_intent_delete(intent_id: str):
        """Cancel/delete an active intent."""
        if intent_id not in _active_intents:
            return JSONResponse({"error": "Intent not found"}, status_code=404)
        del _active_intents[intent_id]
        _resolved_intents.pop(intent_id, None)
        logger.info("Intent deleted: %s", intent_id)
        return {"status": "deleted", "intent_id": intent_id}

    @broker_router.get("/latency")
    async def broker_latency(window_s: float = 300.0) -> BrokerLatency:
        """Per-model latency percentiles over the rolling window.

        Aggregates the ``_recent_requests`` ring buffer. Models with fewer
        than 3 samples in the window are omitted from ``per_model``
        (single-call noise); the ``overall`` bucket aggregates everything.

        Query parameters
        ----------------
        window_s : float
            Default 300.0 (5 min). Clamped to ``[10.0, 3600.0]``.
        """
        window_s = max(10.0, min(3600.0, window_s))
        return aggregate_latency(list(_recent_requests), window_s)

    @broker_router.get("/catalog")
    async def broker_catalog() -> BrokerCatalog:
        """Registered models from broker.yaml enriched with residency state.

        Returns the full ``models:`` dict from broker.yaml, augmented with:
        - ``currently_loaded`` — whether VRAMTracker reports the model in
          Ollama right now
        - ``actual_vram_gb`` — measured VRAM at the snapshot, or null
        - ``is_evictable`` — loaded AND not the scheduler's current model
          AND not always_allowed

        ``snapshot_age_s`` reflects the age of the residency snapshot used
        to build this response (close to zero — a fresh ``/api/ps`` query
        is issued per request). ``is_evictable`` is computed at response
        time and can flip between calls if a swap is in flight.

        When ``/api/ps`` is unreachable, residency information collapses
        to "nothing loaded" rather than raising — the catalog itself
        remains queryable so operators can still see the registry shape
        during an Ollama outage.
        """
        snapshot_ts = time.time()
        loaded_raw = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        loaded = loaded_raw if loaded_raw is not None else []
        # Tag-aware residency: /api/ps reports 'name:latest' for untagged
        # registry keys — normalize the implicit tag on both sides so a
        # resident model isn't shown as not-loaded under a tag mismatch.
        loaded_norm = {m.name.removesuffix(":latest"): m for m in loaded}
        snapshot_age_s = max(0.0, time.time() - snapshot_ts)
        current = _scheduler.current_model if _scheduler else None

        entries: list[CatalogEntry] = []
        for name, info in config.models.items():
            resident = loaded_norm.get(name.removesuffix(":latest"))
            is_loaded = resident is not None
            entries.append(CatalogEntry(
                name=name,
                vram_gb=info.vram_gb,
                default_num_ctx=info.default_num_ctx,
                tags=list(info.tags),
                always_allowed=info.always_allowed,
                currently_loaded=is_loaded,
                actual_vram_gb=resident.vram_gb if resident is not None else None,
                is_evictable=(
                    is_loaded
                    and name != current
                    and not info.always_allowed
                ),
            ))

        return BrokerCatalog(
            models=entries,
            total=len(entries),
            loaded_count=sum(1 for e in entries if e.currently_loaded),
            evictable_count=sum(1 for e in entries if e.is_evictable),
            registry_source=(
                _redact_home(str(config.loaded_from))
                if config.loaded_from else "<unknown>"
            ),
            snapshot_age_s=snapshot_age_s,
            residency_state="unknown" if loaded_raw is None else "ok",
        )

    # ── Machine snapshot (observability spec 4.9/4.10) ──────────────
    # Same single-sourced handlers as create_app — registered here too so the
    # admin-only two-port deployment serves these endpoints (spec 4.10).
    broker_router.add_api_route("/snapshot", _handle_snapshot, methods=["GET"])
    broker_router.add_api_route(
        "/snapshot/stream", _handle_snapshot_stream, methods=["GET"]
    )
    broker_router.add_api_route("/contention", _handle_contention, methods=["GET"])
    broker_router.add_api_route(
        "/gpu/extended", _handle_gpu_extended, methods=["GET"]
    )
    broker_router.add_api_route("/processes", _handle_processes, methods=["GET"])
    # Correlation engine surfaces (spec 6.4/6.3/7). Ring + enriched stall are
    # FOLDED into /broker/snapshot (no /ring or /stall endpoints); only the
    # composite-risk and discrete-contention surfaces get their own routes.
    broker_router.add_api_route(
        "/correlation/risk", _handle_correlation_risk, methods=["GET"]
    )
    broker_router.add_api_route(
        "/correlation/contentions",
        _handle_correlation_contentions,
        methods=["GET"],
    )

    # ── A2A Interface Routes ────────────────────────────────────────
    # SSE encoding is the shared module-level _sse_wrapper (deduped, spec 5.6).

    @a2a_router.get("/stats")
    async def a2a_stats():
        """Task store statistics for monitoring."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        return _a2a_handler._store.stats()

    @a2a_router.post("/tasks")
    async def a2a_create_task(request: Request):
        """Create a new A2A task."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        body = await request.json()
        result = await _a2a_handler.create_task(body)
        return JSONResponse(result, status_code=201)

    @a2a_router.get("/tasks/{task_id}")
    async def a2a_get_task(task_id: str, request: Request):
        """Get task status and results."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        result = await _a2a_handler.get_task(task_id)
        if result is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return result

    @a2a_router.get("/tasks/{task_id}/stream")
    async def a2a_stream_task(task_id: str, request: Request):
        """SSE stream for task status/artifact updates."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        if not _a2a_handler._store.has_task(task_id):
            return JSONResponse({"error": "Task not found"}, status_code=404)
        generator = _a2a_handler.subscribe_task(task_id, request=request)
        return StreamingResponse(
            _sse_wrapper(generator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @a2a_router.delete("/tasks/{task_id}")
    async def a2a_cancel_task(task_id: str, request: Request):
        """Cancel a running task."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        success = await _a2a_handler.cancel_task(task_id)
        if not success:
            if _a2a_handler.is_task_terminal(task_id):
                return JSONResponse(
                    {"error": "Task already in terminal state (not cancelable)"},
                    status_code=409,
                )
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return {"status": "canceled", "task_id": task_id}

    @a2a_router.post("/leases/{lease_id}/heartbeat")
    async def a2a_lease_heartbeat(lease_id: str, request: Request):
        """Touch a lease to keep it alive."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        body = await request.json()
        fencing_token = body.get("fencing_token")
        if fencing_token is None:
            return JSONResponse({"error": "Missing fencing_token"}, status_code=400)
        valid, reason = _a2a_handler.validate_lease(lease_id, fencing_token)
        if not valid:
            return JSONResponse({"error": reason}, status_code=409)
        lease = _a2a_handler._leases.get(lease_id)
        if lease:
            lease.touch()
            return {
                "lease_id": lease_id,
                "remaining_requests": lease.remaining_requests,
                "state": lease.state.value,
            }
        return JSONResponse({"error": "Lease not found"}, status_code=404)

    @a2a_router.delete("/leases/{lease_id}")
    async def a2a_release_lease(lease_id: str, request: Request):
        """Explicitly release a model lease."""
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        success = _a2a_handler.release_lease(lease_id)
        if not success:
            return JSONResponse({"error": "Lease not found"}, status_code=404)
        return {"status": "released", "lease_id": lease_id}

    # ── Agent Card (Three-Tier Disclosure) ───────────────────────────

    @app.get("/.well-known/agent-card.json")
    async def agent_card():
        """Tier 1: Public Agent Card — no auth required.

        Returns a stripped-down card with generic info only.
        NO model names, NO VRAM data, NO queue depth, NO GPU info.
        """
        if _a2a_handler:
            return _a2a_handler.build_public_card()
        admin_port = config.server.admin_port or config.server.port
        return {
            "name": "BASTION GPU Inference Broker",
            "description": "GPU inference broker with scheduling, batching, and model management",
            "version": bastion.__version__,
            "serviceEndpoint": f"http://localhost:{admin_port}/a2a",
            "protocolVersion": "0.1",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
            },
            "skills": [
                "text-generation",
                "embeddings",
            ],
            "securitySchemes": {
                "BearerToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A2A bearer token for task and extended card endpoints",
                }
            },
            "security": [{"BearerToken": []}],
        }

    # ── Tier 2: Extended Card (authenticated A2A agents) ────────────

    @a2a_router.get("/extended-card")
    async def a2a_extended_card():
        """Tier 2: Extended Agent Card — A2A auth required.

        Returns detailed capability info for authenticated agents.
        """
        if not _a2a_handler:
            return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
        return await _a2a_handler.build_extended_card()

    # ── Include routers ─────────────────────────────────────────────
    app.include_router(broker_router)
    app.include_router(a2a_router)

    return app
