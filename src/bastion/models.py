"""Pydantic models for BASTION configuration, requests, and queue state."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field

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

    @property
    def two_port_mode(self) -> bool:
        """True when admin traffic should be on a separate port."""
        return self.admin_port > 0 and self.admin_port != self.port


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

    def effective_priority(self, aging_rate: float, affinity_bonus: float = 0.0) -> float:
        """Compute current priority with aging and optional affinity bonus."""
        return self.base_priority + (self.age_seconds * aging_rate) + affinity_bonus


# ---------------------------------------------------------------------------
# GPU state models
# ---------------------------------------------------------------------------

class LoadedModel(BaseModel):
    """A model currently loaded in Ollama's VRAM."""
    name: str
    size_bytes: int = 0
    vram_gb: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


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
    """Current GPU hardware state."""
    temperature_c: int | None = None
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    vram_total_mb: int | None = None
    power_draw_watts: float | None = None

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


# ---------------------------------------------------------------------------
# S7: A2A models (continued — these depend on PriorityTier)
# ---------------------------------------------------------------------------

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
