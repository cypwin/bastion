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
import json
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import bastion
from bastion import audit
from bastion.auth import make_a2a_token_dependency, make_admin_key_dependency
from bastion.circuitbreaker import CircuitBreakerTransport
from bastion.health import check_gpu_safe, query_gpu_status
from bastion.metrics import CONTENT_TYPE_LATEST, PROMETHEUS_AVAILABLE, get_metrics_text
from bastion.middleware import MetricsMiddleware
from bastion.models import (
    BrokerConfig,
    BrokerStatus,
    IntentDeclaration,
    IntentResponse,
    PriorityTier,
    QueuedRequest,
)
from bastion.proxy import OllamaProxy
from bastion.queue import AffinityQueue
from bastion.ratelimit import RateLimitMiddleware
from bastion.scheduler import Scheduler
from bastion.thrashing import ThrashingDetector
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
_queue: AffinityQueue | None = None
_scheduler: Scheduler | None = None
_a2a_handler: Any | None = None  # A2AHandler (avoid circular import at module level)
_a2a_http_client: httpx.AsyncClient | None = None  # Shared httpx client for A2A (CB transport)
_config: BrokerConfig | None = None
_process_monitor: ProcessMonitor | None = None
_sweep_task: asyncio.Task | None = None
_start_time: float = 0.0
_thrashing_detector: ThrashingDetector | None = None

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
_recent_requests: deque[dict] = deque(maxlen=50)


def record_recent_request(
    model: str,
    endpoint: str,
    tier: str,
    queue_wait_s: float,
    duration_s: float,
    status_code: int,
) -> None:
    """Record a completed request in the recent requests ring buffer."""
    _recent_requests.appendleft({
        "timestamp": time.time(),
        "model": model,
        "endpoint": endpoint,
        "tier": tier,
        "queue_wait_s": round(queue_wait_s, 3),
        "duration_s": round(duration_s, 3),
        "status_code": status_code,
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


async def _queue_sweep_loop(ttl_seconds: float) -> None:
    """Background task that sweeps stale requests every 60 seconds.

    For each swept request: unblocks any waiting proxy handler by setting
    grant and completion events, and logs an audit event.
    """
    while True:
        await asyncio.sleep(60.0)
        if not _queue:
            continue
        swept = _queue.sweep_stale(ttl_seconds)
        for req in swept:
            grant_evt = _pending_grants.pop(req.id, None)
            if grant_evt:
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
            if original_done_event is not None:
                timeout = (_config.proxy.inference_timeout_seconds if _config else 300.0) + 60.0
                try:
                    await asyncio.wait_for(original_done_event.wait(), timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "Completion event timed out for non-blocking request %s (%.0fs)",
                        request.id, timeout,
                    )
                    _pending_completions.pop(request.id, None)
            # Clean up inflight tracking
            if _inflight_lock is not None:
                async with _inflight_lock:
                    count = _inflight_models.get(request.model, 1) - 1
                    if count <= 0:
                        _inflight_models.pop(request.model, None)
                    else:
                        _inflight_models[request.model] = count
            # Wake the scheduler so it can dispatch queued same-model requests
            # immediately instead of waiting for the next loop_interval timeout.
            # (Fix for issue #3: see reference/QUEUE_STALENESS_INVESTIGATION.md)
            if _scheduler:
                _scheduler.notify()

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
    global _proxy, _vram_tracker, _vram_manager, _queue, _scheduler, _a2a_handler, _a2a_http_client, _start_time

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
        except ImportError:
            logger.error(
                "Persistence requires aiosqlite. "
                "Install with: pip install bastion[persistence]"
            )
            raise SystemExit(1)

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
    if _db_manager and config.persistence.enabled and config.persistence.persist_audit:
        if audit._audit_logger is not None:
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
    _start_time = time.time()

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

        _a2a_task_store = _TaskStore(
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
        loaded = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        gpu = await query_gpu_status()
        status = BrokerStatus(
            uptime_seconds=time.time() - _start_time,
            queue_depth=_queue.total_size if _queue else 0,
            queue_by_model=_queue.queue_depth_by_model() if _queue else {},
            loaded_models=loaded,
            gpu=gpu,
            current_model=_scheduler.current_model if _scheduler else None,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_model_swaps=_scheduler.total_swaps if _scheduler else 0,
            state="draining" if (_scheduler and _scheduler.is_draining) else "running",
            vram_ledger=_vram_manager.status() if _vram_manager else None,
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

        # T1-06: gpu_is_safe computed from GPUStatus.is_safe(config.gpu)
        result["gpu_is_safe"] = gpu.is_safe(config.gpu)

        # M58: thrashing detection stats
        if _thrashing_detector:
            result["thrashing_warnings"] = _thrashing_detector.total_warnings
            result["thrashing_halts"] = _thrashing_detector.total_halts

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
        return _vram_manager.status()

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
        """Pre-load a model into VRAM."""
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)

        if not _vram_tracker:
            return JSONResponse({"error": "VRAM tracker not initialized"}, status_code=503)

        can_load, reason = await _vram_tracker.can_load_model(model)
        if not can_load:
            return JSONResponse({"error": reason}, status_code=409)

        # Trigger model load via a minimal generate request
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
        return {"status": "loaded", "model": model}

    @broker_router.post("/unload")
    async def broker_unload(request: Request):
        """Force-unload a model from VRAM."""
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)

        if _vram_tracker:
            success = await _vram_tracker.unload_model(model)
            return {"status": "unloaded" if success else "failed", "model": model}
        return JSONResponse({"error": "VRAM tracker not initialized"}, status_code=503)

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

    @broker_router.get("/metrics")
    async def broker_metrics():
        """Prometheus metrics endpoint (text exposition format).

        Returns 501 Not Implemented if prometheus-client is not installed.
        To enable: pip install bastion[metrics]
        """
        if not PROMETHEUS_AVAILABLE:
            return JSONResponse(
                {
                    "error": "Metrics not available",
                    "details": (
                        "prometheus-client not installed."
                        " Install with: pip install bastion[metrics]"
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

    # ── A2A Interface Routes ────────────────────────────────────────

    async def _sse_wrapper(generator: AsyncGenerator[dict, None]) -> AsyncGenerator[bytes, None]:
        """Wrap A2A events as SSE-formatted bytes.

        Handles three event types:
        - Regular events: formatted as "data: {json}\\n\\n"
        - Heartbeats: formatted as SSE comment ": heartbeat\\n\\n"
        - Sentinels (None): ignored (generator should stop)
        """
        async for event in generator:
            if event is None:
                break
            if isinstance(event, dict) and event.get("_heartbeat"):
                yield b": heartbeat\n\n"
                continue
            data = json.dumps(event)
            yield f"data: {data}\n\n".encode()

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
            return JSONResponse({"error": "Task not found or not cancelable"}, status_code=404)
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

    # ── Ollama proxy routes ─────────────────────────────────────────

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
    )
    async def proxy_ollama(request: Request, path: str) -> Response:
        """Proxy all /api/* requests to Ollama backend."""
        if not _proxy:
            return JSONResponse({"error": "Proxy not initialized"}, status_code=503)
        return await _proxy.handle_request(request)

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD"],
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
        loaded = await _vram_tracker.get_loaded_models() if _vram_tracker else []
        gpu = await query_gpu_status()
        status = BrokerStatus(
            uptime_seconds=time.time() - _start_time,
            queue_depth=_queue.total_size if _queue else 0,
            queue_by_model=_queue.queue_depth_by_model() if _queue else {},
            loaded_models=loaded,
            gpu=gpu,
            current_model=_scheduler.current_model if _scheduler else None,
            total_requests_served=_proxy._requests_served if _proxy else 0,
            total_model_swaps=_scheduler.total_swaps if _scheduler else 0,
            state="draining" if (_scheduler and _scheduler.is_draining) else "running",
            vram_ledger=_vram_manager.status() if _vram_manager else None,
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

        # T1-06: gpu_is_safe computed from GPUStatus.is_safe(config.gpu)
        result["gpu_is_safe"] = gpu.is_safe(config.gpu)

        # M58: thrashing detection stats
        if _thrashing_detector:
            result["thrashing_warnings"] = _thrashing_detector.total_warnings
            result["thrashing_halts"] = _thrashing_detector.total_halts

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
        return _vram_manager.status()

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
        """Pre-load a model into VRAM."""
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)
        if not _vram_tracker:
            return JSONResponse({"error": "VRAM tracker not initialized"}, status_code=503)
        can_load, reason = await _vram_tracker.can_load_model(model)
        if not can_load:
            return JSONResponse({"error": reason}, status_code=409)
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{config.ollama.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": -1,
                    "options": {"use_mmap": False},
                },
            )
        return {"status": "loaded", "model": model}

    @broker_router.post("/unload")
    async def broker_unload(request: Request):
        """Force-unload a model from VRAM."""
        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse({"error": "Missing 'model' field"}, status_code=400)
        if _vram_tracker:
            success = await _vram_tracker.unload_model(model)
            return {"status": "unloaded" if success else "failed", "model": model}
        return JSONResponse({"error": "VRAM tracker not initialized"}, status_code=503)

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

    @broker_router.get("/metrics")
    async def broker_metrics():
        """Prometheus metrics endpoint."""
        if not PROMETHEUS_AVAILABLE:
            return JSONResponse(
                {
                    "error": "Metrics not available",
                    "details": (
                        "prometheus-client not installed."
                        " Install with: pip install bastion[metrics]"
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

    # ── A2A Interface Routes ────────────────────────────────────────

    async def _sse_wrapper_admin(
        generator: AsyncGenerator[dict, None],
    ) -> AsyncGenerator[bytes, None]:
        """Wrap A2A events as SSE-formatted bytes."""
        async for event in generator:
            if event is None:
                break
            if isinstance(event, dict) and event.get("_heartbeat"):
                yield b": heartbeat\n\n"
                continue
            data = json.dumps(event)
            yield f"data: {data}\n\n".encode()

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
            _sse_wrapper_admin(generator),
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
            return JSONResponse({"error": "Task not found or not cancelable"}, status_code=404)
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
