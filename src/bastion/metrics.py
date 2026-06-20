"""Prometheus metrics registry for BASTION.

Provides counters, histograms, and gauges for request telemetry, queue state,
GPU health, model swap tracking, and A2A task lifecycle. Gracefully degrades
if prometheus-client is not installed — all metrics become no-ops, allowing
instrumentation code to remain unconditional.

Metrics exposed:
  - bastion_requests_total: Total requests by endpoint, status, tier
  - bastion_request_duration_seconds: Request latency histogram
  - bastion_request_queue_wait_seconds: Time spent waiting in queue (priority/model)
  - bastion_queue_depth: Current queue size per model
  - bastion_model_swap_total: Model transitions (from_model -> to_model)
  - bastion_model_swap_duration_seconds: Model swap time histogram
  - bastion_cooldown_waits_total: Scheduler cooldown enforcement count
  - bastion_vram_used_bytes: Current VRAM usage
  - bastion_gpu_temperature_celsius: GPU temperature
  - bastion_a2a_tasks_total: A2A task submissions and terminal outcomes
  - bastion_a2a_errors_total: A2A error codes
  - bastion_a2a_task_duration_seconds: End-to-end A2A task duration
  - bastion_a2a_task_queue_wait_seconds: A2A queue wait time
  - bastion_llm_time_to_first_token_seconds: Streaming quality (TTFT)
  - bastion_a2a_tasks_active: Current active A2A task count by state
  - bastion_a2a_queue_depth: A2A queue depth per skill/model

Label cardinality management:
  Tier 1 (always safe): skill (5-20), model (5-50), state (bounded enum),
                         error_code (bounded set), method (bounded set)
  Never use: task_id, request_id, context_id (unbounded cardinality)

Usage:
    from bastion.metrics import (
        REQUEST_DURATION,
        REQUESTS_TOTAL,
        record_request,
        emit_a2a_task,
    )

    # Record a completed request
    record_request(
        endpoint="/api/generate",
        status_code=200,
        duration=1.23,
        model="qwen3:8b",
        tier="interactive"
    )

    # Record an A2A task state transition
    emit_a2a_task(skill="infer", state="submitted")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Try to import prometheus_client — if unavailable, create no-op stubs
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    PROMETHEUS_AVAILABLE = True
    logger.info("prometheus-client imported successfully")
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning(
        "prometheus-client not installed — metrics will be no-ops. "
        "Install with: pip install bastion-broker[metrics]"
    )

    # No-op stubs that match the prometheus_client API
    class _NoOpMetric:
        """Base no-op metric that ignores all method calls."""
        def labels(self, **kwargs: Any) -> _NoOpMetric:
            return self
        def inc(self, amount: float = 1.0) -> None:
            pass
        def observe(self, amount: float) -> None:
            pass
        def set(self, value: float) -> None:
            pass

    class Counter(_NoOpMetric):  # type: ignore[no-redef]  # conditional stub for optional prometheus_client
        """No-op Counter stub."""
        def __init__(self, name: str, documentation: str, labelnames: list[str] | None = None):
            pass

    class Histogram(_NoOpMetric):  # type: ignore[no-redef]  # conditional stub for optional prometheus_client
        """No-op Histogram stub."""
        def __init__(
            self,
            name: str,
            documentation: str,
            labelnames: list[str] | None = None,
            buckets: tuple[float, ...] | None = None,
        ):
            pass

    class Gauge(_NoOpMetric):  # type: ignore[no-redef]  # conditional stub for optional prometheus_client
        """No-op Gauge stub."""
        def __init__(self, name: str, documentation: str, labelnames: list[str] | None = None):
            pass

    def generate_latest() -> bytes:  # type: ignore[misc]  # conditional stub for optional prometheus_client
        """No-op exposition generator."""
        return b""

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

# Request counters and histograms
REQUESTS_TOTAL = Counter(
    "bastion_requests_total",
    "Total number of requests processed by BASTION",
    labelnames=["endpoint", "status_code", "tier"],
)

REQUEST_DURATION = Histogram(
    "bastion_request_duration_seconds",
    "Request processing duration in seconds",
    labelnames=["endpoint", "model", "tier"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

# Queue state
QUEUE_DEPTH = Gauge(
    "bastion_queue_depth",
    "Current number of requests waiting in queue",
    labelnames=["model"],
)

# Scheduler metrics
#
# Vision C schema-frozen metric: bastion_model_swap_total
# Cardinality formula: |models| × |models| × |reason enum (3)|
# For 17 registered models: 17 × 17 × 3 = 867 series worst case. See Risk R1.
MODEL_SWAP_TOTAL = Counter(
    "bastion_model_swap_total",
    "Total model transitions; reason in {scheduler_pick, affinity_miss, eviction}",
    labelnames=["from_model", "to_model", "reason"],
)

COOLDOWN_WAITS_TOTAL = Counter(
    "bastion_cooldown_waits_total",
    "Total number of cooldown waits enforced by scheduler",
)

# Vision C schema-frozen metric: bastion_request_queue_wait_seconds
# This is the single canonical queue-wait histogram for v0.4+. The legacy
# bastion_queue_wait_seconds (tier label) was dropped before schema-freeze;
# /metrics had no public contract before v0.4 so there is nothing to preserve.
REQUEST_QUEUE_WAIT = Histogram(
    "bastion_request_queue_wait_seconds",
    "Time a request waited in the affinity queue before dispatch",
    labelnames=["priority", "model"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# GPU metrics
VRAM_USED_BYTES = Gauge(
    "bastion_vram_used_bytes",
    "Current VRAM usage in bytes",
)

# Vision C schema-frozen metric: bastion_vram_used_mb
# Reports the same value as VRAM_USED_BYTES but in MB units, matching the
# Grafana dashboard panel and Prometheus rule expressions.
VRAM_USED_MB = Gauge(
    "bastion_vram_used_mb",
    "VRAM used in megabytes as reported by nvidia-smi / Ollama fusion",
    labelnames=["gpu_index"],
)

GPU_TEMPERATURE = Gauge(
    "bastion_gpu_temperature_celsius",
    "GPU temperature in degrees Celsius",
)

# Vision C schema-frozen metric: bastion_thrashing_detector_halt_total
# IMPORTANT: agent_id MUST be a registered agent name (X-Agent-Id header value)
# OR a source IP truncated to /24 prefix. NEVER a task UUID — that would
# unbound the label cardinality and OOM Prometheus. See Risk R3.
THRASHING_DETECTOR_HALT_TOTAL = Counter(
    "bastion_thrashing_detector_halt_total",
    "Cumulative thrashing verdict transitions per agent (WARNED, HALTED)",
    labelnames=["agent_id", "verdict"],
)

# Vision C schema-frozen metric: bastion_concurrent_requests_active
# Pure gauge — no labels. Represents currently in-flight inference requests.
CONCURRENT_REQUESTS_ACTIVE = Gauge(
    "bastion_concurrent_requests_active",
    "Number of inference requests currently inflight to Ollama",
)

# Model swap duration histogram
MODEL_SWAP_DURATION = Histogram(
    "bastion_model_swap_duration_seconds",
    "Time taken to perform a model swap (load/unload cycle)",
    labelnames=["model"],
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0),
)

# ---------------------------------------------------------------------------
# VRAM ledger reconciliation / drift (observability expansion, spec 5.4)
# ---------------------------------------------------------------------------

# NET-NEW objects (NOT Tier-0 activations). The reconcile() path in vram.py
# emits audit events for stale-removal and import; these counters meter the
# same events for Prometheus. Both are deliberately LABEL-LESS: the natural
# discriminator would be the model name, which is unbounded (any model a user
# runs) — Section 3 rule #2 forbids that cardinality. The aggregate rate is
# what matters operationally (how often Ollama auto-unloads / clients bypass
# the broker), not a per-model breakdown.
VRAM_RECONCILE_STALE_TOTAL = Counter(
    "bastion_vram_reconcile_stale_total",
    "Ledger allocations dropped because Ollama no longer reports the model "
    "(stale-removal during reconcile)",
)

VRAM_RECONCILE_IMPORT_TOTAL = Counter(
    "bastion_vram_reconcile_import_total",
    "Resident-but-untracked models imported into the ledger during reconcile "
    "(loaded outside a BASTION swap)",
)

# Signed gauge: measured VRAM (backend) minus tracked VRAM (allocated+reserved).
# Growing positive = the ledger under-counts actual residency (unsafe). The
# single bounded label is gpu_index so multi-GPU is a non-breaking future
# extension; single-GPU deployments emit gpu_index="0". Emitted on the SLOW
# snapshot tick only (it needs a backend VRAM read) and SKIPPED — never set to
# 0 — when the backend returns None (StubBackend / non-NVIDIA).
VRAM_LEDGER_DRIFT_MB = Gauge(
    "bastion_vram_ledger_drift_mb",
    "Signed drift: measured VRAM (backend) − tracked VRAM (allocated+reserved), MB",
    labelnames=["gpu_index"],
)

# ---------------------------------------------------------------------------
# Correlation-engine metrics (observability expansion, spec 6.4/6.3/6.5 / 7)
# ---------------------------------------------------------------------------

# CARDINALITY DISCIPLINE (Constraint #2): the correlation engine is fed by
# per-process attribution data, but NONE of that may become a Prometheus label.
# These five metrics use ONLY bounded labels — ``factor`` (5 RiskIndex component
# names) and ``kind`` (4 contention kinds) — or no labels at all. Process names,
# PIDs, device VALUES, etc. stay on the TUI/JSON surfaces only.

# Composite forward-looking RiskIndex (spec 6.4). Pure gauge, NO labels.
RISK_INDEX = Gauge(
    "bastion_risk_index",
    "Composite forward-looking risk score in [0, 1] (higher = closer to a "
    "VRAM/thermal/swap/thrashing crash)",
)

# Rising-edge counter for the dominant RiskIndex factor each tick. The single
# bounded label is the component NAME (5 fixed enum values), matching the
# thrashing-counter convention — never a per-PID/per-model label.
RISK_DOMINANT_FACTOR_TOTAL = Counter(
    "bastion_risk_dominant_factor_total",
    "Count of ticks each RiskIndex component was the dominant risk factor",
    labelnames=["factor"],
)

# Discrete contention-event counter (spec 6.3). Bounded ``kind`` enum:
# nvme_burst / mem_pressure / cpu_contention / combined. The human-readable
# attribution string (which may name a device/process) is JSON-only — never a
# label.
CONTENTION_EVENTS_TOTAL = Counter(
    "bastion_contention_events_total",
    "Discrete host-contention events joined to an inference stall, by kind",
    labelnames=["kind"],
)

# CPU<->GPU thermal coupling (spec 6.5). Both gauges are LABEL-LESS.
THERMAL_COUPLING_ACTIVE = Gauge(
    "bastion_thermal_coupling_active",
    "1 when CPU heat is driving the shared cooling (fan curve engaged), else 0",
)

THERMAL_HEADROOM_CELSIUS = Gauge(
    "bastion_thermal_headroom_celsius",
    "Minimum thermal headroom (C) over the computable CPU/GPU ceiling terms",
)

# ---------------------------------------------------------------------------
# A2A task lifecycle metrics
# ---------------------------------------------------------------------------

# Task lifecycle counters — tracks submissions and terminal outcomes
# Label cardinality: skill (5-20 values), state (bounded enum: 5 values)
A2A_TASKS_TOTAL = Counter(
    "bastion_a2a_tasks_total",
    "A2A task submissions and terminal outcomes",
    labelnames=["skill", "state"],
)

# A2A error counter — tracks JSON-RPC and skill-level errors
# Label cardinality: method (bounded: ~5), error_code (bounded: ~10)
A2A_ERRORS_TOTAL = Counter(
    "bastion_a2a_errors_total",
    "A2A protocol and skill error counts",
    labelnames=["method", "error_code"],
)

# End-to-end A2A task duration (created_at -> terminal state)
A2A_TASK_DURATION = Histogram(
    "bastion_a2a_task_duration_seconds",
    "End-to-end A2A task duration from creation to terminal state",
    labelnames=["skill", "model", "state"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

# A2A queue wait time (submitted -> working transition)
A2A_TASK_QUEUE_WAIT = Histogram(
    "bastion_a2a_task_queue_wait_seconds",
    "Time from A2A task submitted to working state",
    labelnames=["skill", "model"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# LLM time to first token (streaming quality metric)
LLM_TIME_TO_FIRST_TOKEN = Histogram(
    "bastion_llm_time_to_first_token_seconds",
    "Time from request dispatch to first token received (streaming)",
    labelnames=["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0),
)

# Active A2A task gauge by state
A2A_TASKS_ACTIVE = Gauge(
    "bastion_a2a_tasks_active",
    "Current number of active A2A tasks by state",
    labelnames=["state"],
)

# A2A queue depth per skill/model
A2A_QUEUE_DEPTH = Gauge(
    "bastion_a2a_queue_depth",
    "A2A task queue depth per skill and model",
    labelnames=["skill", "model"],
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def record_request(
    endpoint: str,
    status_code: int,
    duration: float,
    model: str | None = None,
    tier: str = "agent",
) -> None:
    """Record a completed request in all relevant metrics.

    Parameters
    ----------
    endpoint : str
        Request path (e.g., "/api/generate", "/broker/status").
    status_code : int
        HTTP status code (200, 503, etc.).
    duration : float
        Total request processing time in seconds.
    model : str | None
        Model name if applicable (None for admin endpoints).
    tier : str
        Priority tier: interactive, agent, pipeline, background.
    """
    REQUESTS_TOTAL.labels(
        endpoint=endpoint,
        status_code=str(status_code),
        tier=tier,
    ).inc()

    if model:
        REQUEST_DURATION.labels(
            endpoint=endpoint,
            model=model,
            tier=tier,
        ).observe(duration)


def record_queue_wait(model: str, priority: str, wait_seconds: float) -> None:
    """Record time a request spent waiting in the queue.

    Vision C schema-frozen metric. Writes to
    ``bastion_request_queue_wait_seconds`` (priority/model labels) — the single
    canonical queue-wait histogram for v0.4+.

    Parameters
    ----------
    model : str
        Model name the request was targeting.
    priority : str
        Priority tier (interactive, agent, pipeline, background).
    wait_seconds : float
        Time from enqueue to dispatch.
    """
    REQUEST_QUEUE_WAIT.labels(priority=priority, model=model).observe(wait_seconds)


def update_queue_depth(model: str, depth: int) -> None:
    """Update the queue depth gauge for a specific model.

    Parameters
    ----------
    model : str
        Model name.
    depth : int
        Number of requests waiting for this model.
    """
    QUEUE_DEPTH.labels(model=model).set(depth)


def record_model_swap(
    from_model: str | None,
    to_model: str,
    reason: str = "scheduler_pick",
) -> None:
    """Record a model swap event.

    Vision C schema-frozen metric. The ``reason`` enum is one of
    ``scheduler_pick``, ``affinity_miss``, ``eviction``.

    Parameters
    ----------
    from_model : str | None
        Previously loaded model (None or "_none" if loading from idle).
    to_model : str
        Model being loaded.
    reason : str
        Why the swap happened. Bounded enum to cap cardinality.
    """
    MODEL_SWAP_TOTAL.labels(
        from_model=from_model or "_none",
        to_model=to_model,
        reason=reason,
    ).inc()


def record_cooldown_wait() -> None:
    """Record that the scheduler enforced a cooldown wait."""
    COOLDOWN_WAITS_TOTAL.inc()


def update_vram_usage(bytes_used: int) -> None:
    """Update the VRAM usage gauge.

    Parameters
    ----------
    bytes_used : int
        Current VRAM usage in bytes.
    """
    VRAM_USED_BYTES.set(bytes_used)


def update_vram_used_mb(gpu_index: str, mb: float) -> None:
    """Update the per-GPU VRAM-used-in-MB gauge.

    Vision C schema-frozen metric. Single-GPU deployments use
    ``gpu_index="0"``. The value is the same quantity already computed by the
    VRAM ledger refresh — no new GPU queries should be issued for this metric.

    Parameters
    ----------
    gpu_index : str
        GPU identifier (string form so it round-trips through Prometheus labels).
    mb : float
        Current VRAM usage in megabytes.
    """
    VRAM_USED_MB.labels(gpu_index=gpu_index).set(mb)


def record_thrashing_verdict(agent_id: str, verdict: str) -> None:
    """Record a thrashing detector verdict for an agent.

    Vision C schema-frozen metric. The ``verdict`` enum is one of ``WARNED``,
    ``HALTED``. The ``agent_id`` MUST be a registered agent name OR a source
    IP truncated to /24 prefix — NEVER a task UUID. See Risk R3 in the
    Vision C plan: task-UUID values would unbound the label cardinality and
    OOM Prometheus. Enforcement is the caller's responsibility; this helper
    accepts whatever string it is given.

    Parameters
    ----------
    agent_id : str
        Stable per-agent identifier (header value or /24 IP prefix).
    verdict : str
        Detector verdict ("WARNED" or "HALTED").
    """
    THRASHING_DETECTOR_HALT_TOTAL.labels(
        agent_id=agent_id,
        verdict=verdict,
    ).inc()


def set_concurrent_requests_active(count: int) -> None:
    """Update the gauge of in-flight inference requests.

    Vision C schema-frozen metric. Pure gauge with no labels.

    Parameters
    ----------
    count : int
        Current number of concurrent inflight requests.
    """
    CONCURRENT_REQUESTS_ACTIVE.set(count)


def update_gpu_temperature(celsius: float) -> None:
    """Update the GPU temperature gauge.

    Parameters
    ----------
    celsius : float
        Current temperature in degrees Celsius.
    """
    GPU_TEMPERATURE.set(celsius)


def record_model_swap_duration(model: str, duration: float) -> None:
    """Record the time taken for a model swap.

    Parameters
    ----------
    model : str
        The model being loaded during the swap.
    duration : float
        Swap duration in seconds.
    """
    MODEL_SWAP_DURATION.labels(model=model).observe(duration)


def record_vram_reconcile_stale(count: int = 1) -> None:
    """Count stale ledger allocations dropped during ``reconcile()``.

    Called at the stale-removal site in :meth:`VRAMManager.reconcile` when one
    or more model allocations are released because Ollama no longer reports
    them. Label-less by design (model name = unbounded cardinality).

    Parameters
    ----------
    count : int
        Number of stale models removed in this reconcile pass (default 1).
    """
    VRAM_RECONCILE_STALE_TOTAL.inc(count)


def record_vram_reconcile_import(count: int = 1) -> None:
    """Count resident-but-untracked models imported during ``reconcile()``.

    Called at the import site in :meth:`VRAMManager.reconcile` when one or more
    models present in Ollama ``/api/ps`` but absent from the ledger are
    accounted into the budget. Label-less by design (model name = unbounded).

    Parameters
    ----------
    count : int
        Number of models imported in this reconcile pass (default 1).
    """
    VRAM_RECONCILE_IMPORT_TOTAL.inc(count)


def update_vram_ledger_drift(gpu_index: str, mb: float) -> None:
    """Publish the signed VRAM ledger-drift gauge for a GPU.

    ``mb`` is measured VRAM (backend ``query_status``) minus tracked VRAM
    (``allocated + reserved`` from the ledger). The caller (the slow snapshot
    tick) MUST skip this call entirely when the backend reports no measured
    value — publishing ``0`` would falsely claim the ledger is perfectly in
    sync on a host that simply cannot read VRAM (StubBackend / non-NVIDIA).

    Parameters
    ----------
    gpu_index : str
        GPU identifier (string form so it round-trips through Prometheus labels).
    mb : float
        Signed drift in megabytes (positive = ledger under-counts residency).
    """
    VRAM_LEDGER_DRIFT_MB.labels(gpu_index=gpu_index).set(mb)


# ---------------------------------------------------------------------------
# Correlation-engine helpers (observability expansion, spec 6.4/6.3/6.5)
# ---------------------------------------------------------------------------

def update_risk_index(score: float) -> None:
    """Set the composite RiskIndex gauge (spec 6.4).

    Parameters
    ----------
    score : float
        Composite risk in [0, 1]. The caller already clamps; this is a pure set.
    """
    RISK_INDEX.set(score)


def record_risk_dominant_factor(factor: str) -> None:
    """Increment the dominant-factor counter for this tick (spec 6.4).

    ``factor`` is always one of the five bounded RiskIndex component names
    (``vram_headroom``/``thermal_headroom``/``swap_rate``/``thrashing``/
    ``memory_psi``), so the label cardinality is fixed. Never a per-PID/model
    label.
    """
    RISK_DOMINANT_FACTOR_TOTAL.labels(factor=factor).inc()


def record_contention_event(kind: str) -> None:
    """Increment the discrete-contention-event counter by kind (spec 6.3).

    ``kind`` is one of the bounded enum values
    (``nvme_burst``/``mem_pressure``/``cpu_contention``/``combined``). The event's
    human-readable attribution (which may name a device) is JSON-only — it never
    becomes a label.
    """
    CONTENTION_EVENTS_TOTAL.labels(kind=kind).inc()


def update_thermal_coupling_active(active: bool) -> None:
    """Set the thermal-coupling gauge to 1 (engaged) or 0 (spec 6.5)."""
    THERMAL_COUPLING_ACTIVE.set(1.0 if active else 0.0)


def update_thermal_headroom_celsius(headroom_c: float) -> None:
    """Set the minimum-thermal-headroom gauge in C (spec 6.5).

    The caller MUST skip this entirely when no headroom term is computable
    (``thermal_headroom_min_c is None`` on a no-GPU + no-CPU-sensor host) —
    publishing ``0`` would falsely claim zero headroom on a host that simply
    cannot read either temperature.
    """
    THERMAL_HEADROOM_CELSIUS.set(headroom_c)


# ---------------------------------------------------------------------------
# A2A emit helpers
# ---------------------------------------------------------------------------

def emit_a2a_task(skill: str, state: str) -> None:
    """Increment the A2A task counter for a skill/state transition.

    Parameters
    ----------
    skill : str
        Skill identifier (e.g., "infer", "batch_infer", "status", "preload").
    state : str
        Task state (e.g., "submitted", "working", "completed", "failed", "canceled").
    """
    A2A_TASKS_TOTAL.labels(skill=skill, state=state).inc()


def emit_a2a_error(method: str, error_code: str) -> None:
    """Increment the A2A error counter.

    Parameters
    ----------
    method : str
        The A2A method that generated the error (e.g., "create_task", "infer").
    error_code : str
        Error code string (e.g., "-32050", "queue_full", "timeout").
    """
    A2A_ERRORS_TOTAL.labels(method=method, error_code=error_code).inc()


def observe_a2a_task_duration(
    skill: str,
    model: str,
    state: str,
    duration: float,
) -> None:
    """Record end-to-end A2A task duration.

    Parameters
    ----------
    skill : str
        Skill identifier.
    model : str
        Model used (or "none" if not applicable).
    state : str
        Terminal state ("completed", "failed", "canceled").
    duration : float
        Total task duration in seconds (updated_at - created_at).
    """
    A2A_TASK_DURATION.labels(skill=skill, model=model, state=state).observe(duration)


def observe_a2a_queue_wait(skill: str, model: str, wait_seconds: float) -> None:
    """Record A2A task queue wait time (submitted -> working).

    Parameters
    ----------
    skill : str
        Skill identifier.
    model : str
        Target model (or "none" if not applicable).
    wait_seconds : float
        Time from task creation to working state.
    """
    A2A_TASK_QUEUE_WAIT.labels(skill=skill, model=model).observe(wait_seconds)


def observe_llm_ttft(model: str, ttft_seconds: float) -> None:
    """Record LLM time to first token.

    Parameters
    ----------
    model : str
        Model name.
    ttft_seconds : float
        Time from request dispatch to first token received.
    """
    LLM_TIME_TO_FIRST_TOKEN.labels(model=model).observe(ttft_seconds)


def update_a2a_tasks_active(state: str, count: int) -> None:
    """Set the active A2A task count for a given state.

    Parameters
    ----------
    state : str
        Task state ("submitted", "working").
    count : int
        Current number of tasks in this state.
    """
    A2A_TASKS_ACTIVE.labels(state=state).set(count)


def update_a2a_queue_depth(skill: str, model: str, depth: int) -> None:
    """Set the A2A queue depth for a skill/model combination.

    Parameters
    ----------
    skill : str
        Skill identifier.
    model : str
        Model name.
    depth : int
        Number of tasks waiting.
    """
    A2A_QUEUE_DEPTH.labels(skill=skill, model=model).set(depth)


def get_metrics_text() -> bytes:
    """Generate Prometheus exposition format text.

    Returns
    -------
    bytes
        Metrics in Prometheus text format, or empty bytes if unavailable.
    """
    return generate_latest()


__all__ = [
    "PROMETHEUS_AVAILABLE",
    "CONTENT_TYPE_LATEST",
    # Existing proxy/scheduler metrics
    "REQUESTS_TOTAL",
    "REQUEST_DURATION",
    "QUEUE_DEPTH",
    "MODEL_SWAP_TOTAL",
    "MODEL_SWAP_DURATION",
    "COOLDOWN_WAITS_TOTAL",
    "VRAM_USED_BYTES",
    "GPU_TEMPERATURE",
    # VRAM reconcile / ledger-drift (observability expansion, spec 5.4)
    "VRAM_RECONCILE_STALE_TOTAL",
    "VRAM_RECONCILE_IMPORT_TOTAL",
    "VRAM_LEDGER_DRIFT_MB",
    # Vision C schema-frozen metrics (do not rename — public contract)
    "REQUEST_QUEUE_WAIT",
    "VRAM_USED_MB",
    "THRASHING_DETECTOR_HALT_TOTAL",
    "CONCURRENT_REQUESTS_ACTIVE",
    # A2A metrics
    "A2A_TASKS_TOTAL",
    "A2A_ERRORS_TOTAL",
    "A2A_TASK_DURATION",
    "A2A_TASK_QUEUE_WAIT",
    "LLM_TIME_TO_FIRST_TOKEN",
    "A2A_TASKS_ACTIVE",
    "A2A_QUEUE_DEPTH",
    # Existing helpers
    "record_request",
    "record_queue_wait",
    "update_queue_depth",
    "record_model_swap",
    "record_model_swap_duration",
    "record_cooldown_wait",
    "update_vram_usage",
    "update_gpu_temperature",
    # VRAM reconcile / ledger-drift helpers (observability expansion, spec 5.4)
    "record_vram_reconcile_stale",
    "record_vram_reconcile_import",
    "update_vram_ledger_drift",
    # Correlation-engine helpers (observability expansion, spec 6.3/6.4/6.5)
    "update_risk_index",
    "record_risk_dominant_factor",
    "record_contention_event",
    "update_thermal_coupling_active",
    "update_thermal_headroom_celsius",
    # Vision C schema-frozen helpers
    "update_vram_used_mb",
    "record_thrashing_verdict",
    "set_concurrent_requests_active",
    # A2A helpers
    "emit_a2a_task",
    "emit_a2a_error",
    "observe_a2a_task_duration",
    "observe_a2a_queue_wait",
    "observe_llm_ttft",
    "update_a2a_tasks_active",
    "update_a2a_queue_depth",
    "get_metrics_text",
]
