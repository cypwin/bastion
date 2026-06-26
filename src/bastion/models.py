"""Pydantic models for BASTION configuration, requests, and queue state."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, computed_field

# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------

class OllamaConfig(BaseModel):
    """Ollama backend connection settings."""
    host: str = "127.0.0.1"
    port: int = 11435  # Where Ollama actually listens (moved from default 11434)
    api_timeout_seconds: float = 5.0  # Timeout for /api/ps queries
    unload_timeout_seconds: float = 10.0  # Timeout for model unload requests

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class ServerConfig(BaseModel):
    """BASTION server settings."""
    host: str = "0.0.0.0"
    port: int = 11434  # Standard Ollama port — clients connect here
    admin_port: int = 0  # Separate admin+A2A port (0 = disabled, same port as proxy)
    public_url: str | None = None  # Full external URL for A2A agent card advertisement

    @property
    def two_port_mode(self) -> bool:
        """True when admin traffic should be on a separate port."""
        return self.admin_port > 0 and self.admin_port != self.port

    @property
    def external_url(self) -> str:
        """Return the URL to advertise in agent cards.

        If ``public_url`` is set (e.g. when running behind a reverse proxy),
        it is used verbatim (trailing slash stripped).  Otherwise falls back to
        ``http://localhost:<port>`` — using ``localhost`` even when the bind
        address is ``0.0.0.0`` or ``::`` since the actual listener address is
        not known at config time.
        """
        if self.public_url:
            return self.public_url.rstrip("/")
        host = "localhost" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"


class GPUConfig(BaseModel):
    """GPU safety thresholds.

    Set ``total_vram_gb`` to ``0`` (the default) to auto-detect from
    ``nvidia-smi`` at startup.  See :func:`bastion.config.resolve_gpu_defaults`.
    """
    total_vram_gb: float = 0.0  # 0 = auto-detect from nvidia-smi
    headroom_gb: float = 6.0
    max_temperature_c: int = 83
    max_power_watts: float = 300.0  # Conservative default; auto-detect overrides
    default_vram_estimate_gb: float = 10.0  # VRAM estimate for unknown models
    nvidia_smi_timeout_seconds: int = 5  # nvidia-smi subprocess timeout
    # F5 — physical-VRAM hardware gate (best-effort cross-check, NOT the crash boundary).
    hardware_margin_gb: float = 2.0  # nvidia-smi free-VRAM safety margin; raise to 3-4 for multi-monitor GPUs
    non_ollama_reserve_gb: float = 0.0  # subtract compositor/framebuffer VRAM from the budget
    hardware_gate_fail_mode: Literal["open", "closed_on_swap"] = "closed_on_swap"
    hardware_gate_miss_degrade_after: int = 3  # consecutive cold-swap misses before degrade-to-open
    # F6 — optional steady-state power headroom trip (0 = disabled; the brake is the transient backstop).
    power_headroom_pct: float = 0.0

    @property
    def max_vram_gb(self) -> float:
        """Usable VRAM budget (total minus headroom)."""
        return self.total_vram_gb - self.headroom_gb


class ProxyConfig(BaseModel):
    """Proxy routing and timeout settings."""
    inference_timeout_seconds: float = 300.0  # HTTP timeout for Ollama inference
    connect_timeout_seconds: float = 10.0  # HTTP connect timeout
    queue_timeout_seconds: float = 300.0  # Max wait in queue before 504
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MB default
    scheduled_endpoints: set[str] = Field(
        default_factory=lambda: {"/api/generate", "/api/chat", "/api/embed"}
    )
    passthrough_endpoints: set[str] = Field(
        default_factory=lambda: {
            "/api/pull", "/api/show", "/api/tags", "/api/ps",
            "/api/delete", "/api/copy", "/api/create", "/api/blobs",
        }
    )


class SwapBrakeConfig(BaseModel):
    """Swap-velocity circuit breaker — the sensor-independent crash backstop (F1/F2).

    Counts BASTION's OWN residency transitions on a monotonic clock, so it keeps
    working when every nvidia-smi / ``/api/ps`` sensor is dark — which is when the
    host is most likely to die. Defaults are conservative portable floors for an
    unknown card; calibrate down via ``--stress-test``. With ``count_evictions``
    each swap spends ~2 tokens (evict + load), so ``refill_per_minute=5.0`` ⇒
    ~2.5 sustained swaps/min — below the >8/min crash zone. See
    docs/design/specs/2026-06-26-swap-velocity-circuit-breaker-design.md.

    Defined ABOVE SchedulerConfig deliberately: ``default_factory=SwapBrakeConfig``
    is a live name lookup at SchedulerConfig class-body execution time, so the
    factory target must already exist (``from __future__ import annotations`` only
    defers the annotation string, not the default_factory argument).
    """
    enabled: bool = True               # hard to disable; backstop for fail-open gates
    min_spacing_seconds: float = 8.0   # cold-LOAD floor; 7.5/min instantaneous ceiling
    bucket_capacity: float = 3.0       # burst tolerance (calibrated: safe_burst_depth)
    refill_per_minute: float = 5.0     # sustained safe velocity (calibrated: safe_swap_rate_per_min)
    count_evictions: bool = True       # BASTION-initiated unloads debit a token (2 events/swap)
    cooloff_seconds: float = 30.0      # base OPEN hold
    cooloff_backoff_max_seconds: float = 60.0  # exponential 30→60 cap (forgiving)
    min_state_hold_seconds: float = 5.0        # anti tick-flap (loop runs at 0.1s)
    release_rate_per_minute: float = 3.0       # hysteresis (< refill): anti-flap band
    shed_when_infeasible: bool = True          # 503 doomed swaps; do not stall them
    infeasible_evict_reload_threshold: int = 3
    infeasible_window_seconds: float = 120.0
    degraded_refill_factor: float = 0.5        # tighten refill when hardware gate blind (F5)


class PinDetectionConfig(BaseModel):
    """Ollama ``keep_alive=-1`` pin detection (F4).

    The behavioral evict↔reload oscillation signature is the PRIMARY,
    version-independent detector; ``expires_at`` parsing is an additive proactive
    hint. Absent/unparseable ``expires_at`` degrades to the behavioral signature,
    never to "no protection".
    """
    enabled: bool = True
    expires_horizon_seconds: float = 3600.0  # expires_at beyond now+this ⇒ externally pinned


class SchedulerConfig(BaseModel):
    """Scheduling algorithm parameters."""
    cooldown_seconds: float = 2.0
    model_affinity_bonus: float = 10.0
    aging_rate: float = 2.0  # Priority points gained per second of waiting
    max_queue_size: int = 512
    residency_cache_ttl_seconds: float = 1.0  # TTL for model residency cache
    ollama_max_loaded_models: int = 4  # Max models Ollama should keep loaded (3 council + embed)
    loop_interval_seconds: float = 0.1  # Scheduler wake-up interval
    error_backoff_seconds: float = 1.0  # Backoff after scheduler loop error
    gpu_unsafe_backoff_seconds: float = 5.0  # Backoff when GPU health check fails
    shutdown_timeout_seconds: float = 10.0  # Max time to wait for scheduler stop
    # Swap rate limiter — prevents PCIe power transient crashes
    # Crash forensics: ~55-60 swaps in ~7 min (8-9/min) with mmap=true crashed GPU.
    # Thresholds must be BELOW crash rate to prevent reaching it.
    # With use_mmap:false now enforced, per-swap stress is lower, but the crash
    # mechanism (VRM thermal / PSU transient / CUDA fragmentation) is cumulative
    # and not fully understood — stay well clear of 8-9/min.
    swap_rate_window_seconds: float = 60.0  # Rolling window for swap counting
    swap_rate_warn_threshold: int = 4  # Start throttling early (half of crash rate)
    swap_rate_critical_threshold: int = 6  # Hard brake — 2-3 swaps below crash rate
    swap_rate_warn_cooldown_seconds: float = 5.0  # Cooldown at warn level
    swap_rate_critical_cooldown_seconds: float = 10.0  # Cooldown at critical level
    max_concurrent_dispatches: int = 3  # Max concurrent inferences (different models)
    # Stagger concurrent dispatches to reduce power transients
    concurrent_dispatch_delay_seconds: float = 0.1
    queue_ttl_seconds: float = 600.0  # Max age for queued requests (10 min); swept every 60s
    # Swap-velocity circuit breaker (F1/F2) + Ollama keep_alive pin detection (F4).
    swap_brake: SwapBrakeConfig = Field(default_factory=SwapBrakeConfig)
    pin_detection: PinDetectionConfig = Field(default_factory=PinDetectionConfig)


class AuditConfig(BaseModel):
    """Audit logging configuration.

    Controls the level of detail captured in audit events.

    Tiers:
      1 — Always on: timestamps, request_id, identity hashes, model, operation,
          token counts, latency, status.
      2 — Default: Tier 1 + SHA-256 content hashes of prompt/response.
      3 — Opt-in: Tier 2 + full prompt/response text (debugging only).
    """
    tier: int = 2
    content_hashing: bool = True


class AuthConfig(BaseModel):
    """Authentication configuration."""
    enabled: bool = False
    api_keys: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    enabled: bool = False
    requests_per_minute: int = 60
    burst: int = 10
    trusted_proxies: list[str] = Field(default_factory=list)


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker configuration."""
    enabled: bool = True
    failure_threshold: int = 5  # consecutive failures to trip open
    recovery_timeout: float = 30.0  # seconds before half-open probe


class PersistenceConfig(BaseModel):
    """Optional SQLite persistence configuration."""
    enabled: bool = False
    database_path: str = ""        # empty = auto (XDG data_dir / "bastion.db")
    persist_audit: bool = True
    persist_tasks: bool = True
    persist_queue: bool = False     # opt-in, most users don't need this
    queue_recovery_ttl: int = 300   # seconds, entries older than this discarded on startup


class PriorityConfig(BaseModel):
    """Base priority values for each tier."""
    interactive: float = 100.0
    agent: float = 50.0
    pipeline: float = 25.0
    background: float = 10.0


class ModelInfo(BaseModel):
    """Known model metadata."""
    vram_gb: float
    default_num_ctx: int = 4096  # Default context window for this model
    tags: list[str] = Field(default_factory=list)
    always_allowed: bool = False


class RequestOverrides(BaseModel):
    """Safety overrides injected into ALL Ollama requests."""
    use_mmap: bool = False  # GPU crash prevention — see CRASH_ROOT_CAUSE.md
    default_num_ctx: int | None = 4096  # Global fallback context window


class ComplexityRoutingConfig(BaseModel):
    """Complexity-based model routing configuration (M58).

    When enabled, reads X-Task-Complexity header and routes the request
    to the configured route model.

    When ``override_explicit`` is False (default), an explicit ``model``
    in the request body wins and the route only fills in for requests
    that omit the model. Set True to restore the original M58
    force-route behavior (route model replaces the client's choice).
    """
    enabled: bool = True
    routes: dict[str, str] = Field(default_factory=dict)  # "simple" -> model name
    override_explicit: bool = False  # honor explicit client model by default
    complex_action: str = "reject"  # always "reject" -> HTTP 422


class ThrashingDetectionConfig(BaseModel):
    """Per-agent swap thrashing detection (M58).

    Tracks swap patterns per agent and warns or halts when swap ratio
    exceeds thresholds. Conservative defaults based on consumer-GPU crash
    forensics; server-GPU operators (A100/H100) should tune halt_swap_ratio
    and swap_rate_critical_threshold down to reflect their swap-stress
    profiles.
    """
    enabled: bool = True
    mode: str = "warn"  # "warn" or "strict"
    window_size: int = 12
    warn_swap_ratio: float = 0.5  # ~4 swaps/min equivalent
    halt_swap_ratio: float = 0.75  # ~6 swaps/min (matches global critical)
    cooloff_seconds: int = 30
    min_requests_before_eval: int = 6


# ---------------------------------------------------------------------------
# Observability configuration (spec 4.8 — the `observability:` block)
# ---------------------------------------------------------------------------

class CorrelationConfig(BaseModel):
    """Correlation-engine thresholds and weights (spec 4.8).

    Nested under ``observability.correlation:``.  Every threshold is a
    documented config key so server-GPU operators and slow-IO/container hosts
    can tune the contention legs and the RiskIndex weights without code
    changes.  The block-device write threshold governs **all** discovered base
    devices (``nvme*/sd*/vd*/mmcblk*``), not NVMe specifically.
    """
    ring_maxlen: int = 512  # Correlation ring capacity
    ring_tail_in_snapshot: int = 32  # Last-N ring events embedded in /broker/snapshot
    # Block-device write-throughput contention threshold (MB/s). Device-dependent:
    # tune to ~50-70% of the drive's observed sustained write rate. Startup INFO
    # logs the active value; dynamic idle-calibration is the long-term default.
    contention_block_write_mb_s_threshold: float = 200.0
    contention_psi_threshold: float = 20.0  # mem_psi_some_avg10 threshold
    contention_cpu_psi_threshold: float = 60.0  # cpu_psi_some_avg10 threshold
    contention_hysteresis_ticks: int = 2  # ticks above threshold before emitting
    # CPU thermal ceiling for the headroom formula (6.5). Fallback that differs
    # by CPU (Ryzen 7000 Tjmax 95, EPYC 90, Cortex-A 105); startup INFO logs it.
    cpu_safe_ceiling_c: float = 85.0
    # GPU thermal ceiling for the headroom formula. None => use
    # gpu.max_temperature_c (itself auto-detected from tlimit/shutdown); if that
    # is unset/0 (no-GPU) the GPU headroom term is skipped (CPU-only headroom).
    gpu_safe_ceiling_c: float | None = None
    # Per-component RiskIndex weights (spec 6.4): VRAM headroom 25%, thermal
    # headroom 20%, swap-rate 25%, thrashing 20%, memory-PSI 10% (sum 1.0).
    risk_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "vram_headroom": 0.25,
            "thermal_headroom": 0.20,
            "swap_rate": 0.25,
            "thrashing": 0.20,
            "memory_psi": 0.10,
        }
    )


class ObservabilityConfig(BaseModel):
    """The ``observability:`` config block (spec 4.8).

    Greenfield block in ``broker.yaml``; optional on ``BrokerConfig`` via a
    default factory, so an absent block produces working defaults.  Sources
    that the device/host can report (CPU sensor, RAPL domain, block devices,
    disk mounts) are auto-discovered when the corresponding key is ``None``;
    pinning a key overrides discovery.  See the spec table for the full
    discovery strategy per key.
    """
    # Fast-tick cadence (seconds) for the broker-side _machine_snapshot_loop
    # (spec 4.9). Monotonic-anchored: a slow nvidia-smi does not compound drift.
    snapshot_interval_s: float = 2.0
    # Slow-tick cadence (seconds) for subprocess-heavy / rarely-changing GPU
    # signals — throttle reasons, PCIe tx/rx, Xid scan (spec 4.9 slow path).
    # The loop derives an integer tick-modulo from
    # round(slow_tick_interval_s / snapshot_interval_s); the most recent slow
    # result is cached and reused on the intervening fast ticks so the 2s fast
    # path is never blocked by 30s-stale subprocess work.
    slow_tick_interval_s: float = 30.0
    # Whether GET /broker/snapshot/stream serves the SSE push surface (spec
    # 5.6). Default on: the stream supersedes the older 2026-03-13
    # /broker/status/stream and is a first-class external surface. When False
    # the endpoint returns 501 (the TUI never depends on it — it polls).
    snapshot_stream_enabled: bool = True
    # List of process names or `pid:NNN` always shown in the attribution panel.
    process_watchlist: list[str] = Field(default_factory=list)
    churn_threshold: int = 5  # New-PID count per slow tick that fires a churn event
    ecc_enabled: bool = False  # Opt-in slow-poll of GPU ECC counters (Tier 4)
    cpu_sensor_name: str | None = None  # Pin a hwmon `name`; None => discover
    rapl_domain_path: str | None = None  # Pin a RAPL energy path; None => probe
    storage_device_filter: list[str] | None = None  # Allow-list base devices; None => regex
    disk_mount_labels: dict[str, str] | None = None  # mount->label; None => discover
    psi_io_full_warn_pct: float = 5.0  # PSI io_full avg10 TUI warn threshold
    psi_io_full_crit_pct: float = 25.0  # PSI io_full avg10 TUI critical threshold
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)


# ---------------------------------------------------------------------------
# S7: A2A models
# ---------------------------------------------------------------------------

class TelemetryConfig(BaseModel):
    """OpenTelemetry instrumentation configuration.

    When enabled and opentelemetry packages are installed, BASTION emits
    trace spans for task submission, queue wait, model swaps, and Ollama
    inference calls following OTel GenAI semantic conventions.

    When disabled (default) or when opentelemetry is not installed, all
    instrumentation calls become no-ops with zero overhead.
    """
    enabled: bool = False
    exporter: str = "none"  # "none", "console", "otlp"
    endpoint: str = ""  # OTLP endpoint (e.g. "http://localhost:4317")
    service_name: str = "bastion"


class A2AConfig(BaseModel):
    """A2A interface configuration."""
    enabled: bool = False
    tokens: list[str] = Field(default_factory=list)
    reservation_max_requests: int = 100
    reservation_timeout_seconds: float = 600.0  # 10 minutes
    task_ttl_seconds: float = 3600.0  # Completed tasks kept for 1 hour
    max_batch_size: int = 50


class A2ATaskState(StrEnum):
    """A2A task lifecycle states."""
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class A2ATaskRecord(BaseModel):
    """Internal record for an A2A task."""
    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    context_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: A2ATaskState = A2ATaskState.SUBMITTED
    skill_id: str
    input_params: dict[str, Any] = Field(default_factory=dict)
    output_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    model_config = {"arbitrary_types_allowed": True}


class BrokerConfig(BaseModel):
    """Top-level BASTION configuration."""
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    gpu: GPUConfig = Field(default_factory=GPUConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    priorities: PriorityConfig = Field(default_factory=PriorityConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    a2a: A2AConfig = Field(default_factory=lambda: A2AConfig())
    models: dict[str, ModelInfo] = Field(default_factory=dict)
    session_profiles: dict[str, SessionProfile] = Field(default_factory=dict)
    request_overrides: RequestOverrides = Field(default_factory=RequestOverrides)
    complexity_routing: ComplexityRoutingConfig = Field(default_factory=ComplexityRoutingConfig)
    thrashing_detection: ThrashingDetectionConfig = Field(default_factory=ThrashingDetectionConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    # Resolved path of the broker.yaml that loaded this config. Set by
    # ``bastion.config.load_config`` post-construction. ``None`` when the
    # config was built from defaults (no file found) or in tests that
    # bypass ``load_config``. Exposed via ``/broker/catalog`` as a string;
    # callers should use ``str(cfg.loaded_from) if cfg.loaded_from else "<unknown>"``.
    _loaded_from: Path | None = PrivateAttr(default=None)

    @property
    def loaded_from(self) -> Path | None:
        """Resolved path of the source broker.yaml, or ``None``."""
        return self._loaded_from


# ---------------------------------------------------------------------------
# Priority tier enum
# ---------------------------------------------------------------------------

class PriorityTier(StrEnum):
    """Request priority tiers (highest to lowest)."""
    INTERACTIVE = "interactive"
    AGENT = "agent"
    PIPELINE = "pipeline"
    BACKGROUND = "background"

    def base_priority(self, config: PriorityConfig) -> float:
        return getattr(config, self.value)


# ---------------------------------------------------------------------------
# Queue models
# ---------------------------------------------------------------------------

class QueuedRequest(BaseModel):
    """A request waiting in the affinity queue."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str
    endpoint: str  # /api/generate, /api/chat, /api/embed
    body: bytes = b""
    priority: float = 50.0  # Current effective priority (increases with age)
    base_priority: float = 50.0
    tier: PriorityTier = PriorityTier.AGENT
    submitted_at: float = Field(default_factory=time.time)
    client_info: str = ""

    model_config = {"arbitrary_types_allowed": True}

    @property
    def age_seconds(self) -> float:
        return time.time() - self.submitted_at

    def effective_priority(
        self,
        aging_rate: float,
        affinity_bonus: float = 0.0,
        now: float | None = None,
    ) -> float:
        """Compute current priority with aging and optional affinity bonus.

        ``now`` lets callers snapshot the clock once and rank a batch of
        requests against the same reference — required to preserve the
        FIFO tie-break when several requests share an identical
        ``submitted_at`` (otherwise two ``time.time()`` reads inside the
        comparison loop give the second-checked request a fractional
        edge and silently violate the "equal priority -> first-in-first-
        out" contract).
        """
        age = (now if now is not None else time.time()) - self.submitted_at
        return self.base_priority + (age * aging_rate) + affinity_bonus


# ---------------------------------------------------------------------------
# GPU state models
# ---------------------------------------------------------------------------

class LoadedModel(BaseModel):
    """A model currently loaded in Ollama's VRAM."""
    name: str
    size_bytes: int = 0  # disk size from /api/ps (NOT runtime VRAM — see size_vram)
    vram_gb: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
    # F4 fields parsed from /api/ps (both currently dropped by get_loaded_models):
    expires_at: str | None = None  # RFC3339 keep_alive expiry; far-future ⇒ keep_alive=-1 pin
    size_vram: int = 0  # actual GPU-resident bytes Ollama measured; preferred over disk size_bytes


class ResidencyState(BaseModel):
    """Snapshot of currently resident models in VRAM.

    Used by admin API to report multi-model residency state and by
    scheduler to make co-residency decisions.
    """
    resident_models: list[str] = Field(
        default_factory=list,
        description="Names of all models currently loaded in VRAM"
    )
    last_refreshed: float = Field(
        default_factory=time.time,
        description="Timestamp when residency was last queried (seconds since epoch)"
    )
    vram_usage: dict[str, float] = Field(
        default_factory=dict,
        description="Per-model VRAM usage in GB (model_name -> vram_gb)"
    )

    @property
    def total_vram_gb(self) -> float:
        """Total VRAM used by all resident models."""
        return sum(self.vram_usage.values())

    @property
    def age_seconds(self) -> float:
        """How stale this snapshot is (seconds since last refresh)."""
        return time.time() - self.last_refreshed

    @classmethod
    def from_loaded_models(cls, models: list[LoadedModel]) -> ResidencyState:
        """Create ResidencyState from a list of LoadedModel instances.

        Parameters
        ----------
        models : List[LoadedModel]
            Models currently loaded (from VRAMTracker.get_loaded_models()).

        Returns
        -------
        ResidencyState
            Snapshot with names, VRAM usage, and current timestamp.
        """
        return cls(
            resident_models=[m.name for m in models],
            last_refreshed=time.time(),
            vram_usage={m.name: m.vram_gb for m in models}
        )


# ---------------------------------------------------------------------------
# S6: Session profiles and intent declarations
# ---------------------------------------------------------------------------

class SessionProfile(BaseModel):
    """Named session profile pre-declaring a model sequence.

    Used by client pipelines to signal upcoming model usage
    so the scheduler can pre-plan transitions.
    """
    model_sequence: list[str] = Field(
        description="Ordered list of models used in this pipeline"
    )
    default_priority: PriorityTier = PriorityTier.AGENT
    description: str = ""


class IntentDeclaration(BaseModel):
    """Client declaration of upcoming model usage.

    Sent via POST /broker/intent to inform the scheduler about
    planned model transitions.
    """
    intent_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    profile: str | None = Field(
        default=None,
        description="Name of a session profile from broker.yaml"
    )
    model_sequence: list[str] | None = Field(
        default=None,
        description="Ad-hoc model sequence (used if profile is None)"
    )
    estimated_requests: int = Field(
        default=10,
        description="Estimated number of requests in this session"
    )
    client_id: str = Field(
        default="anonymous",
        description="Identifier for the requesting client"
    )
    created_at: float = Field(default_factory=time.time)


class IntentResponse(BaseModel):
    """Response to a successful intent declaration."""
    intent_id: str
    resolved_priority: str
    model_sequence: list[str]
    estimated_requests: int
    status: str = "registered"


class GPUStatus(BaseModel):
    """Current GPU hardware state.

    The eleven optional ``*_pct`` / ``*_clock_mhz`` / ``pcie_link_*`` fields and
    ``memory_junction_temp_c`` are fast-path signals populated by the
    ``GPUBackend`` extended status query (``NvidiaBackend`` from one
    ``nvidia-smi`` call; ``StubBackend`` leaves them ``None``).  They are all
    ``Optional``/``None``-default so non-NVIDIA / no-GPU hosts (and pre-Ampere
    cards for ``memory_junction_temp_c``, fanless server GPUs for
    ``fan_speed_pct``) yield ``None`` as the *correct complete* value, never a
    misleading ``0``.  See observability spec Section 4.2.
    """
    temperature_c: int | None = None
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    vram_total_mb: int | None = None
    power_draw_watts: float | None = None
    # --- Observability fast-path extensions (spec 4.2) ----------------------
    gpu_index: int = 0  # which GPU this row describes (multi-GPU seam)
    compute_utilization_pct: int | None = None  # utilization.gpu (NvidiaBackend)
    memory_bandwidth_utilization_pct: int | None = None  # utilization.memory
    sm_clock_mhz: int | None = None  # clocks.sm
    gr_clock_mhz: int | None = None  # clocks.gr
    mem_clock_mhz: int | None = None  # clocks.mem
    fan_speed_pct: int | None = None  # fan.speed (READ; distinct from write-path)
    memory_junction_temp_c: int | None = None  # temperature.memory (GDDR junction)
    pcie_link_gen_current: int | None = None
    pcie_link_gen_max: int | None = None
    pcie_link_width_current: int | None = None
    pcie_link_width_max: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vram_utilization_pct(self) -> float | None:
        """VRAM utilization as a percentage (0-100).

        Exposed via ``@computed_field`` so that ``model_dump()`` and JSON
        serialization include the value automatically.
        """
        if self.vram_used_mb and self.vram_total_mb:
            return (self.vram_used_mb / self.vram_total_mb) * 100
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pcie_downgraded(self) -> bool:
        """True iff the PCIe link is running below its negotiated maximum.

        Returns ``True`` only when **all four** link fields are non-``None``
        (partial-data guard) **and** the current generation or width is below
        the maximum.  On partial data and on all non-NVIDIA / no-GPU hardware
        (where the fields are ``None``) it returns ``False`` — never a false
        "downgraded" alarm on hardware that does not expose PCIe link state.
        See observability spec Section 4.2.
        """
        fields = (
            self.pcie_link_gen_current,
            self.pcie_link_gen_max,
            self.pcie_link_width_current,
            self.pcie_link_width_max,
        )
        if any(f is None for f in fields):
            return False
        return (
            self.pcie_link_gen_current < self.pcie_link_gen_max  # type: ignore[operator]
            or self.pcie_link_width_current < self.pcie_link_width_max  # type: ignore[operator]
        )

    def is_safe(self, gpu_config: GPUConfig | None = None) -> bool:
        """Check temperature and VRAM within safe limits.

        Parameters
        ----------
        gpu_config : GPUConfig, optional
            If provided, uses configured thresholds. Otherwise falls back
            to conservative defaults (82°C, 95% VRAM).
        """
        temp_limit = gpu_config.max_temperature_c if gpu_config else 82
        if self.temperature_c and self.temperature_c > temp_limit:
            return False
        return not (self.vram_utilization_pct and self.vram_utilization_pct > 95)


class BrokerStatus(BaseModel):
    """Full broker status for admin API."""
    version: str = Field(default_factory=lambda: __import__("bastion").__version__)
    uptime_seconds: float = 0.0
    queue_depth: int = 0
    queue_by_model: dict[str, int] = Field(default_factory=dict)
    loaded_models: list[LoadedModel] = Field(default_factory=list)
    gpu: GPUStatus = Field(default_factory=GPUStatus)
    current_model: str | None = None
    total_requests_served: int = 0
    total_model_swaps: int = 0
    state: str = "running"  # running, draining, stopped
    vram_state: str = Field(
        default="ok",
        description=(
            "'ok' when loaded_models reflects a live /api/ps read; 'unknown' "
            "when Ollama was unreachable and loaded_models is an empty "
            "placeholder, not a verified empty"
        ),
    )
    vram_ledger: dict[str, Any] | None = Field(
        default=None,
        description="VRAM ledger status from VRAMManager (if available)",
    )
    # --- Observability fields (Phase 1) ------------------------------------
    total_dispatched: int = 0
    swap_rate_level: str | None = None
    stall_reason: str | None = None
    stall_duration_seconds: float | None = None
    inflight_models: dict[str, int] | None = None
    circuit_breaker: dict | None = None
    gpu_is_safe: bool | None = None
    max_vram_gb: float | None = None
    # --- Dashboard observability fields (Phase 2) ------------------
    a2a_summary: dict[str, int] | None = None
    a2a_tasks: list[dict] = Field(default_factory=list)
    active_leases: list[dict] = Field(default_factory=list)
    recent_audit_events: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified observability data model — MachineSnapshot (spec Section 4)
#
# One canonical, surface-independent Pydantic container populated per
# collection tick.  All new fields are Optional/None-default for backward
# compatibility; a partial snapshot with None fields is valid and still
# emitted (graceful degradation).  GPU sub-data is empty/None on
# StubBackend / non-NVIDIA hosts — the correct complete value there, never a
# misleading 0.
# ---------------------------------------------------------------------------

class XidEvent(BaseModel):
    """A single GPU device error event (spec 4.3).

    ``xid_code`` is an NVIDIA Xid code today but documented as a *generic
    device error-code* field, so a future AMD backend can map amdgpu reset
    events onto the same structure without a schema change.
    """
    timestamp: str
    xid_code: int
    raw_message: str


class GPUExtendedStatus(BaseModel):
    """Slow-path GPU signals (spec 4.3).

    Not embedded in ``BrokerStatus`` to keep the 2s fast path free of 30s-stale
    data.  Every field is populated by ``NvidiaBackend`` slow-path methods and
    is empty/``None`` from ``StubBackend``; on non-NVIDIA hardware the empty
    lists are the *correct complete* value.
    """
    throttle_reasons: list[str] = Field(default_factory=list)
    pcie_tx_kb_s: int | None = None
    pcie_rx_kb_s: int | None = None
    recent_xids: list[XidEvent] = Field(default_factory=list)  # bounded maxlen=20 at collection
    xid_count_since_start: int = 0
    last_polled_at: float = 0.0


class BlockDeviceIOStats(BaseModel):
    """Per-device block-IO stats (spec 4.4).

    ``device`` carries a discovered base device name (``nvme0n1`` / ``sda`` /
    ``vdb`` / ``mmcblk0``), never a partition.  Covers ``nvme*/sd*/vd*/mmcblk*``,
    not NVMe only.
    """
    device: str
    util_pct: float  # busy_time delta / elapsed (the canonical device util%)
    read_await_ms: float | None = None
    write_await_ms: float | None = None
    read_rate_mb_s: float
    write_rate_mb_s: float


class ContentionSnapshot(BaseModel):
    """Host pressure snapshot (spec 4.4).

    PSI fields are ``None`` on kernels without CONFIG_PSI (< 4.20 / many
    containers); ``cpu_package_watts`` is host RAPL (Intel **or** AMD) and
    ``None`` without powercap; ``gpu_board_watts`` is backend-provided and
    stays ``None`` until a non-NVIDIA backend fills it (NVIDIA fills
    ``GPUStatus.power_draw_watts`` instead).  No degradation path emits a
    misleading ``0``.
    """
    psi_cpu_some_avg10: float | None = None
    psi_cpu_full_avg10: float | None = None
    psi_mem_some_avg10: float | None = None
    psi_mem_full_avg10: float | None = None
    psi_io_some_avg10: float | None = None
    psi_io_full_avg10: float | None = None
    swap_in_rate_mb_s: float | None = None
    swap_out_rate_mb_s: float | None = None
    block_devices: list[BlockDeviceIOStats] = Field(default_factory=list)
    cpu_package_watts: float | None = None  # host RAPL — Intel OR AMD source
    gpu_board_watts: float | None = None  # Tier-4: backend-provided; None until filled
    oom_kill_total: int | None = None
    oom_kill_rate: float | None = None
    sampled_at: float = Field(default_factory=time.time)


class ProcessGPURow(BaseModel):
    """Per-process GPU utilization row (spec 4.5, pmon-derived)."""
    pid: int
    name: str
    vram_mb: int | None = None
    sm_pct: int | None = None
    mem_pct: int | None = None
    enc_pct: int | None = None
    dec_pct: int | None = None
    is_inference_owned: bool = False
    role: str | None = None


class ProcessRow(BaseModel):
    """Per-process attribution row (spec 4.5; TUI + JSON only, never a label)."""
    pid: int
    name: str
    cpu_pct: float | None = None
    rss_mb: float | None = None
    io_read_bytes_s: float | None = None
    io_write_bytes_s: float | None = None
    is_inference_owned: bool = False
    role: str | None = None
    watchlisted: bool = False
    gpu_row: ProcessGPURow | None = None


class ProcessChurnEvent(BaseModel):
    """A burst of process creation/exit between slow ticks (spec 4.5)."""
    timestamp: float
    new_count: int
    exited_count: int
    new_names: list[str]


class ProcessSnapshot(BaseModel):
    """Per-process attribution (spec 4.5; TUI + JSON only, never Prometheus labels).

    ``gpu_processes`` is populated through the ``GPUBackend`` and is **empty on
    ``StubBackend``** — on non-NVIDIA / no-GPU hosts the panel shows
    CPU/IO/watchlist/churn but no GPU rows, with no error.
    """
    top_processes: list[ProcessRow] = Field(default_factory=list)
    gpu_processes: list[ProcessGPURow] = Field(default_factory=list)
    own_pids: dict[int, str] = Field(default_factory=dict)  # pid -> role ('bastion'|'ollama')
    watchlist_hits: list[ProcessRow] = Field(default_factory=list)
    recent_churn_events: list[ProcessChurnEvent] = Field(default_factory=list)
    collected_at: float
    gpu_collected_at: float | None = None  # slow-tick GPU sub-data age


class InferenceThroughputState(BaseModel):
    """Stream-tapped LLM throughput aggregate (spec 4.6).

    Model-agnostic: reads whatever model name and token accounting Ollama emits
    for any model the user runs.  Divide-by-zero on cache-hit yields ``None``.
    """
    decode_tps_p50: float | None = None
    prefill_tps_p50: float | None = None
    ttft_p50_s: float | None = None
    ctx_utilization_p50: float | None = None
    last_model: str | None = None
    sampled_at: float = Field(default_factory=time.time)


class RiskIndexResult(BaseModel):
    """Composite forward-looking risk gauge output (spec 6.4)."""
    score: float  # [0, 1]
    level: Literal["nominal", "elevated", "high", "critical"]
    component_scores: dict[str, float] = Field(default_factory=dict)
    dominant_factor: str


class ThermalCoupling(BaseModel):
    """CPU<->GPU thermal coupling (spec 6.5).

    All inputs are ``None``-tolerant: ``gpu_temp_c``/``fan_speed_pct`` are
    ``None`` on non-NVIDIA / no-GPU / fanless-server-GPU hosts; ``cpu_temp_c``
    is ``None`` when no CPU sensor is discovered.  A missing input yields a
    partial value (present terms only), never an exception and never a
    misleading ``0``.
    """
    cpu_temp_c: float | None = None
    gpu_temp_c: float | None = None
    fan_speed_pct: int | None = None
    coupling_active: bool = False
    thermal_headroom_min_c: float | None = None


class CorrelationEvent(BaseModel):
    """One event on the correlation ring (spec 6.1)."""
    ts_monotonic: float
    ts_wall: float
    domain: Literal["gpu", "system", "inference", "scheduler"]
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ContentionEvent(CorrelationEvent):
    """A discrete contention event joined to an inference stall (spec 6.3).

    Extends ``CorrelationEvent`` with attribution + the simultaneous-stall
    confirmation that is the correlation moat.
    """
    attribution: str
    inference_was_stalled: bool = False
    stall_reason_at_time: str | None = None
    latency_spike_ratio: float | None = None


class CorrelationState(BaseModel):
    """Correlation-engine outputs surfaced per snapshot (spec 4.7)."""
    risk_index: RiskIndexResult | None = None
    thermal_coupling: ThermalCoupling | None = None
    recent_contentions: list[ContentionEvent] = Field(default_factory=list)
    enriched_stall_reason: str | None = None
    ring_size: int = 0
    recent_ring_events: list[CorrelationEvent] = Field(default_factory=list)  # bounded tail


class MachineSnapshot(BaseModel):
    """Unified, fully-correlated per-tick container (spec 4.1).

    ``model_dump()`` round-trips through JSON identically to ``BrokerStatus``
    and is the payload of ``GET /broker/snapshot``.  ``gpu`` is a single
    ``GPUStatus`` reporting the configured GPU (single-GPU now, list-extensible
    later via ``GPUStatus.gpu_index``).  Every sub-model field is optional so a
    partial snapshot with ``None`` fields is valid and still emitted.
    """
    snapshot_ts: float  # time.time() at collection
    broker: BrokerStatus | None = None  # existing model, promoted in
    gpu: GPUStatus = Field(default_factory=GPUStatus)  # existing model, EXTENDED (4.2)
    gpu_extended: GPUExtendedStatus | None = None  # slow-path GPU signals (4.3)
    contention: ContentionSnapshot | None = None  # host pressure (4.4)
    process: ProcessSnapshot | None = None  # per-process attribution (4.5)
    inference: InferenceThroughputState | None = None  # stream-tapped LLM rates (4.6)
    correlation: CorrelationState | None = None  # engine outputs (4.7)


# ---------------------------------------------------------------------------
# S7: A2A models (continued — these depend on PriorityTier)
# ---------------------------------------------------------------------------

class BrokerCounters(BaseModel):
    """Cumulative broker counters since the last process start.

    ``reset_epoch`` is set once at broker startup and is identical across all
    calls within one process lifetime.  Consumers use it to detect a broker
    restart: any change in ``reset_epoch`` means all counter deltas must be
    discarded (or treated as a full-window reset) to avoid negative-delta rates.
    """

    reset_epoch: str  # ISO-8601 UTC timestamp of broker start
    total_requests_served: int
    total_dispatched: int
    model_swap_total: int
    thrashing_halt_total: int


ThrashingVerdictLabel = Literal["OK", "WARNED", "HALTED"]


class BrokerThrashingAgent(BaseModel):
    """Per-agent thrashing state returned by GET /broker/thrashing."""

    agent_id: str
    verdict: ThrashingVerdictLabel
    cooloff_remaining_s: float
    swap_ratio: float
    last_run_s: float


class BrokerThrashing(BaseModel):
    """Response body for GET /broker/thrashing.

    ``detector_state`` is the worst verdict across all tracked agents
    (HALTED > WARNED > OK).  Empty agent list yields "OK".
    """

    detector_state: ThrashingVerdictLabel
    agents: list[BrokerThrashingAgent]


# ---------------------------------------------------------------------------
# WT-C-A-06: Latency endpoint
# ---------------------------------------------------------------------------


class LatencyBucket(BaseModel):
    """Latency percentiles for one model over the rolling window.

    ``sample_count`` is the number of in-window requests counted toward this
    bucket. Percentile fields are ``None`` only when ``sample_count == 0``;
    callers should treat ``None`` as "no signal" rather than "zero latency".
    """

    model: str
    sample_count: int = Field(
        ge=0,
        description="Number of samples in the window for this model",
    )
    p50_s: float | None = Field(
        default=None,
        description="50th percentile end-to-end duration in seconds; null if sample_count == 0",
    )
    p95_s: float | None = None
    p99_s: float | None = None
    queue_wait_p50_s: float | None = Field(
        default=None,
        description="Queue-wait p50 (time from request arrival to dispatch)",
    )
    queue_wait_p95_s: float | None = None
    error_count: int = 0
    error_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="error_count / sample_count, 0.0 when sample_count == 0",
    )


class BrokerLatency(BaseModel):
    """Response body for GET /broker/latency.

    ``window_s`` reflects the actual age of the oldest sample considered,
    NOT the requested window. If fewer than ``min_samples_per_model`` are
    present for a model, that model's bucket is omitted from ``per_model``.
    """

    window_s: float = Field(
        ge=0.0,
        description="Actual time span of samples in the window, in seconds",
    )
    requested_window_s: float = Field(
        description="The ?window_s query param that was applied (default 300)",
    )
    sample_total: int = Field(ge=0)
    per_model: list[LatencyBucket] = Field(default_factory=list)
    overall: LatencyBucket | None = Field(
        default=None,
        description="Aggregate bucket across all models (model='__overall__')",
    )


# ---------------------------------------------------------------------------
# WT-C-A-07: Catalog endpoint
# ---------------------------------------------------------------------------


class CatalogEntry(BaseModel):
    """One model from broker.yaml's models registry, enriched with runtime state."""

    name: str
    vram_gb: float = Field(
        description="Declared VRAM footprint in GB from broker.yaml",
    )
    default_num_ctx: int = 4096
    tags: list[str] = Field(default_factory=list)
    always_allowed: bool = False
    currently_loaded: bool = Field(
        description="True if VRAMTracker reports this model in Ollama right now",
    )
    actual_vram_gb: float | None = Field(
        default=None,
        description="Measured VRAM at last residency snapshot; null if not loaded",
    )
    is_evictable: bool = Field(
        description=(
            "True if model is loaded AND not currently the scheduler's "
            "current_model AND not always_allowed"
        ),
    )


class BrokerCatalog(BaseModel):
    """Response body for GET /broker/catalog."""

    models: list[CatalogEntry] = Field(default_factory=list)
    total: int = Field(ge=0)
    loaded_count: int = Field(ge=0)
    evictable_count: int = Field(ge=0)
    registry_source: str = Field(
        description=(
            "Path to the broker.yaml that sourced this registry "
            "(home directory redacted to '~')"
        ),
    )
    snapshot_age_s: float = Field(
        ge=0.0,
        description="Seconds since the VRAM tracker's last residency snapshot",
    )
    residency_state: str = Field(
        default="ok",
        description=(
            "'ok' when residency fields reflect a live /api/ps read; "
            "'unknown' when Ollama was unreachable and currently_loaded/"
            "loaded_count collapsed to not-loaded placeholders"
        ),
    )


class BatchInferRequest(BaseModel):
    """Parameters for the batch_infer skill."""
    model: str
    prompts: list[str]
    system_prompt: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    priority: PriorityTier = PriorityTier.AGENT


class BatchInferResult(BaseModel):
    """Result of a batch_infer task."""
    results: list[dict[str, Any]]  # Per-prompt results (index-aligned)
    total: int
    succeeded: int
    failed: int


class ReservationRequest(BaseModel):
    """Parameters for the preload/reservation skill."""
    model: str
    num_requests: int = 10
    timeout_seconds: float | None = None  # Falls back to config default
    priority: PriorityTier = PriorityTier.INTERACTIVE


class Reservation(BaseModel):
    """Active model reservation."""
    reservation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str
    remaining_requests: int
    priority: PriorityTier = PriorityTier.INTERACTIVE
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0  # Set from config


class LeaseState(StrEnum):
    """States for a model lease."""
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"


class ModelLease(BaseModel):
    """Production-grade model lease with hybrid eviction triggers.

    Replaces simple Reservation for A2A model reservations.
    Uses multiple eviction signals: request count, absolute TTL,
    idle timeout, and fencing tokens for zombie prevention.
    """
    lease_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str
    max_requests: int = 100
    remaining_requests: int = 100
    expiry: float = Field(default_factory=lambda: time.monotonic() + 600.0)  # 10 min default
    idle_timeout: float = 60.0  # Seconds since last activity
    last_activity: float = Field(default_factory=time.monotonic)
    fencing_token: int = 0  # Monotonic counter for zombie prevention
    state: LeaseState = LeaseState.ACTIVE
    created_at: float = Field(default_factory=time.time)

    model_config = {"arbitrary_types_allowed": True}

    def should_release(self) -> tuple[bool, str]:
        """Check if the lease should be released.

        Returns (should_release, reason) tuple.
        Checks in priority order:
        1. Request count exhausted
        2. Absolute TTL expired
        3. Idle timeout exceeded
        """
        if self.state != LeaseState.ACTIVE:
            return True, f"LEASE_{self.state.value.upper()}"
        if self.remaining_requests <= 0:
            return True, "REQUEST_LIMIT"
        if time.monotonic() > self.expiry:
            return True, "TTL_EXPIRED"
        if time.monotonic() - self.last_activity > self.idle_timeout:
            return True, "IDLE"
        return False, ""

    def touch(self) -> None:
        """Update last activity timestamp (implicit heartbeat)."""
        self.last_activity = time.monotonic()

    def use_request(self) -> int:
        """Decrement remaining requests. Returns remaining count."""
        self.remaining_requests = max(0, self.remaining_requests - 1)
        self.touch()
        return self.remaining_requests
