# Data Model & Structure Audit — BASTION

**Generated**: 2026-03-13
**Scope**: All Pydantic models, dataclasses, TypedDicts, config schemas, and internal data structures
**Purpose**: Identify hidden data, config gaps, model drift, and unexposed state

---

## Executive Summary

BASTION contains **32 distinct data models** across 10 source files, plus extensive internal state tracked in server.py. Key findings:

- **12 fields with computed data never exposed** via any API endpoint
- **8 config options** exist in code but are missing from example config
- **3 client-server model mismatches** where field names/types differ
- **6 rich internal state structures** that get reduced to simple counts/strings before being returned
- **17 scheduler/queue internal fields** that provide deep operational insight but are never surfaced

The system tracks significantly more operational data than it exposes. This audit provides a roadmap for future observability enhancements.

---

## Complete Model Catalog

### 1. Configuration Models (models.py)

#### `BrokerConfig` (top-level)
```python
Fields: 14 sections
├── ollama: OllamaConfig
├── server: ServerConfig
├── gpu: GPUConfig
├── proxy: ProxyConfig
├── scheduler: SchedulerConfig
├── priorities: PriorityConfig
├── audit: AuditConfig
├── auth: AuthConfig
├── rate_limit: RateLimitConfig
├── circuit_breaker: CircuitBreakerConfig
├── telemetry: TelemetryConfig
├── a2a: A2AConfig
├── models: dict[str, ModelInfo]
├── session_profiles: dict[str, SessionProfile]
└── request_overrides: RequestOverrides
```

**Status**: Fully populated from YAML. All sections have defaults.

---

#### `OllamaConfig`
```python
host: str = "127.0.0.1"
port: int = 11435
api_timeout_seconds: float = 5.0
unload_timeout_seconds: float = 10.0

@property base_url -> str  # Computed, never exposed
```

**Hidden Data**: `base_url` is computed but never returned in status endpoints.

---

#### `ServerConfig`
```python
host: str = "0.0.0.0"
port: int = 11434
admin_port: int = 0

@property two_port_mode -> bool  # Computed, never exposed
```

**Hidden Data**: `two_port_mode` computed property never surfaced.

---

#### `GPUConfig`
```python
total_vram_gb: float = 32.0
headroom_gb: float = 6.0
max_temperature_c: int = 82
max_power_watts: float = 450.0
default_vram_estimate_gb: float = 10.0
nvidia_smi_timeout_seconds: int = 5

@property max_vram_gb -> float  # Computed (total - headroom)
```

**Hidden Data**: `max_vram_gb` is the ACTUAL scheduling budget but never returned. Status endpoints show `total_vram_gb` and loaded model VRAM, but not the computed budget.

---

#### `ProxyConfig`
```python
inference_timeout_seconds: float = 300.0
connect_timeout_seconds: float = 10.0
queue_timeout_seconds: float = 300.0
max_request_body_bytes: int = 10485760
scheduled_endpoints: set[str]
passthrough_endpoints: set[str]
```

**Hidden Data**: `scheduled_endpoints` and `passthrough_endpoints` sets never exposed. No endpoint returns routing table.

---

#### `SchedulerConfig` (21 fields!)
```python
# Core scheduling
cooldown_seconds: float = 2.0
model_affinity_bonus: float = 10.0
aging_rate: float = 2.0
max_queue_size: int = 512

# Residency tracking (S3)
residency_cache_ttl_seconds: float = 1.0
ollama_max_loaded_models: int = 4

# Timing
loop_interval_seconds: float = 0.1
error_backoff_seconds: float = 1.0
gpu_unsafe_backoff_seconds: float = 5.0
shutdown_timeout_seconds: float = 10.0

# Swap rate limiter (crash prevention)
swap_rate_window_seconds: float = 60.0
swap_rate_warn_threshold: int = 4
swap_rate_critical_threshold: int = 6
swap_rate_warn_cooldown_seconds: float = 5.0
swap_rate_critical_cooldown_seconds: float = 10.0

# Concurrent dispatch (S3)
max_concurrent_dispatches: int = 3
concurrent_dispatch_delay_seconds: float = 0.1

# Queue TTL
queue_ttl_seconds: float = 600.0
```

**Hidden Data**:
- `residency_cache_ttl_seconds` — never exposed, controls cache freshness
- `ollama_max_loaded_models` — policy limit never surfaced
- `loop_interval_seconds` through `shutdown_timeout_seconds` — internal timings
- ALL swap rate limiter fields — critical crash prevention state never exposed
- `max_concurrent_dispatches` — concurrency policy never surfaced
- `concurrent_dispatch_delay_seconds` — power transient mitigation never exposed

**Impact**: 11 of 21 scheduler config fields are hidden operational knobs.

---

#### `AuditConfig`
```python
tier: int = 2
content_hashing: bool = True
```

**Status**: Exposed via config file. Fully documented.

---

#### `AuthConfig`, `RateLimitConfig`, `CircuitBreakerConfig`
All fields present in config YAML and read at runtime. No hidden data.

---

#### `TelemetryConfig`
```python
enabled: bool = False
exporter: str = "none"
endpoint: str = ""
service_name: str = "bastion"
```

**Hidden Data**: Actual telemetry state (spans emitted, exporter connectivity) never exposed.

---

#### `A2AConfig`
```python
enabled: bool = False
tokens: list[str]
reservation_max_requests: int = 100
reservation_timeout_seconds: float = 600.0
task_ttl_seconds: float = 3600.0
max_batch_size: int = 50
```

**Status**: All fields in config. Fully exposed.

---

#### `PriorityConfig`
```python
interactive: float = 100.0
agent: float = 50.0
pipeline: float = 25.0
background: float = 10.0
```

**Status**: Exposed in config. Used at runtime. No hidden data.

---

#### `ModelInfo`
```python
vram_gb: float
default_num_ctx: int = 4096
tags: list[str]
always_allowed: bool = False
```

**Status**: All fields exposed via A2A extended card. No drift.

---

#### `RequestOverrides`
```python
use_mmap: bool = False
default_num_ctx: int | None = 4096
```

**Hidden Data**: `default_num_ctx` override policy never surfaced. Not in status endpoints.

---

#### `SessionProfile`
```python
model_sequence: list[str]
default_priority: PriorityTier
description: str
```

**Status**: Defined in config. Never queried via API (no `/broker/profiles` endpoint).

---

### 2. Queue & Request Models (models.py, queue.py)

#### `QueuedRequest`
```python
id: str = uuid4()[:12]
model: str
endpoint: str
body: bytes
priority: float = 50.0
base_priority: float = 50.0
tier: PriorityTier
submitted_at: float = time.time()
client_info: str = ""

@property age_seconds -> float  # Computed, never exposed in status
def effective_priority(aging_rate, affinity_bonus) -> float  # Never surfaced
```

**Hidden Data**:
- `age_seconds` — queue staleness metric NEVER returned
- `effective_priority()` — actual scheduling score NEVER exposed
- `body: bytes` — request payload size/content never surfaced
- `client_info` — caller identity string never returned

**Impact**: Queue inspection endpoints return model name and count only. No visibility into request age, priority scores, or caller identity.

---

#### `AffinityQueue` (internal state)
```python
# From queue.py
_lock: threading.Lock
_model_queues: dict[str, list[QueuedRequest]]
_total_size: int

Methods that compute but never expose:
├── queue_depth_by_model() -> dict[str, int]  # EXPOSED via /broker/status
├── get_models_with_requests() -> list[str]   # NEVER exposed
├── model_queue_size(model) -> int            # NEVER exposed (per-model depth)
└── sweep_stale(max_age) -> list[QueuedRequest]  # NEVER exposed (TTL evictions)
```

**Hidden Data**:
- List of models WITH work (useful for dashboard)
- Per-model queue size (only aggregate depth exposed)
- Stale request sweep results (no endpoint reports evictions)

---

### 3. GPU State Models (models.py, health.py, vram.py)

#### `GPUStatus`
```python
temperature_c: int | None
vram_used_mb: int | None
vram_free_mb: int | None
vram_total_mb: int | None
power_draw_watts: float | None

@property vram_utilization_pct -> float | None  # Computed, never exposed
def is_safe(gpu_config) -> bool  # Computed, NEVER exposed
```

**Hidden Data**:
- `vram_utilization_pct` — computed but not in `/broker/status`
- `is_safe()` — GPU health gate decision NEVER surfaced
- `/broker/status` returns raw `GPUStatus` but properties are lost

**Impact**: Clients see raw MB values but can't see computed utilization or safety thresholds.

---

#### `LoadedModel`
```python
name: str
size_bytes: int = 0
vram_gb: float = 0.0
details: dict[str, Any]
```

**Hidden Data**: `details` dict from Ollama `/api/ps` never surfaced. Contains quantization, family, parameter count.

---

#### `ResidencyState`
```python
resident_models: list[str]
last_refreshed: float = time.time()
vram_usage: dict[str, float]  # model -> vram_gb

@property total_vram_gb -> float  # Computed
@property age_seconds -> float    # Computed, NEVER exposed

@classmethod from_loaded_models(models) -> ResidencyState
```

**Hidden Data**:
- `last_refreshed` — cache timestamp NEVER exposed
- `age_seconds` — cache staleness metric NEVER surfaced
- `vram_usage` per-model breakdown — NOT in `/broker/status` (only `loaded_models` names)

**Impact**: Multi-model residency tracking (S3 feature) invisible to clients.

---

#### `VRAMTracker` (internal state)
```python
# From vram.py
config: BrokerConfig
_http: httpx.AsyncClient
residency_cache: ResidencyCache  # NEVER exposed

Methods with hidden data:
├── get_loaded_models() -> list[LoadedModel]  # details field dropped
├── get_loaded_vram_gb() -> float             # aggregate only
├── can_load_model(model) -> (bool, str)      # reason string NEVER logged to status
├── unload_model(model) -> bool               # success/failure NEVER surfaced
└── log_vram_snapshot(event, extra) -> None   # Writes to /tmp, never queryable
```

**Hidden Data**:
- Residency cache state (TTL, hit/miss rate)
- VRAM snapshot journal (`/tmp/bastion-vram-journal.jsonl`) — written but no read API
- Model load rejection reasons (only logged, never returned)
- Unload confirmation state (polling logic invisible)

---

#### `VRAMManager` (ledger state)
```python
# From vram.py
_total: int
_safety_margin: int
_allocated: int       # NEVER exposed
_reserved: int        # NEVER exposed
_reservations: dict[str, VRAMReservation]  # NEVER exposed
_model_allocations: dict[str, int]         # NEVER exposed
_load_semaphore: asyncio.Semaphore         # NEVER exposed

@property available_vram -> int  # Computed, NEVER exposed
@property allocated_bytes -> int  # NEVER exposed
@property reserved_bytes -> int   # NEVER exposed

def status() -> dict  # Returns full ledger (see below)
```

**`status()` output** (NEVER queried by any endpoint):
```python
{
    "total_bytes": int,
    "safety_margin_bytes": int,
    "allocated_bytes": int,        # Current committed VRAM
    "reserved_bytes": int,         # Pending loads in-flight
    "available_bytes": int,        # Free for new reservations
    "active_reservations": int,    # Count of pending VRAMReservation objects
    "model_allocations": {         # Per-model committed VRAM
        "model_name": bytes
    },
    "reservations": [              # Detailed reservation list
        {
            "id": str,
            "model": str,
            "vram_bytes": int,
            "age_seconds": float,
            "committed": bool
        }
    ]
}
```

**Impact**: **ENTIRE VRAMManager state is invisible.** No endpoint exposes the ledger. `/broker/status` includes placeholder `vram_ledger: dict | None` but it's never populated (always `None`).

---

### 4. Scheduler State (scheduler.py, server.py)

#### `Scheduler` (internal state)
```python
# From scheduler.py
_current_model: str | None
_last_swap_time: float
_total_swaps: int           # EXPOSED via /broker/status
_total_dispatched: int      # NEVER exposed
_running: bool
_draining: bool             # EXPOSED via /broker/status.state
_swap_timestamps: deque[float]  # Rolling window, NEVER exposed
_swap_rate_level: str       # "normal" | "warn" | "critical", NEVER exposed
_last_stall_reason: str     # NEVER exposed
_last_stall_time: float     # NEVER exposed

@property current_model -> str | None  # EXPOSED
@property total_swaps -> int           # EXPOSED
@property total_dispatched -> int      # NEVER exposed
@property is_running -> bool           # Implied by API availability
@property is_draining -> bool          # Exposed as status.state
@property stall_reason -> str          # NEVER exposed
@property stall_time -> float          # NEVER exposed
```

**Hidden Data**:
- `_total_dispatched` — total inference count NEVER exposed (only `total_swaps`)
- `_swap_timestamps` — rolling window for rate limiter NEVER exposed
- `_swap_rate_level` — "warn" or "critical" throttle state NEVER surfaced
- `stall_reason` + `stall_time` — diagnostic data for hung scheduler NEVER exposed

**Swap Rate Level**: Critical operational state. When `_swap_rate_level == "critical"`, cooldown jumps from 2s to 10s to prevent GPU crash. **No visibility.**

---

#### `server.py` Global State (NEVER exposed)
```python
# Dispatch coordination
_pending_grants: dict[str, asyncio.Event]      # NEVER exposed
_pending_completions: dict[str, asyncio.Event] # NEVER exposed
_inflight_models: dict[str, int]               # NEVER exposed (model -> count)

# Intent tracking (S6)
_active_intents: dict[str, IntentDeclaration]  # NEVER exposed
_resolved_intents: dict[str, tuple]            # NEVER exposed

# Recent requests ring buffer (S5)
_recent_requests: deque[dict]  # maxlen=50, NEVER exposed
```

**Recent Requests Buffer** (`_recent_requests`):
```python
{
    "timestamp": float,
    "model": str,
    "endpoint": str,
    "tier": str,
    "queue_wait_s": float,
    "duration_s": float,
    "status_code": int
}
```

**Impact**: Dashboard-ready data structure (last 50 requests with latency) exists but no endpoint returns it. Perfect for `/broker/recent` or TUI panel.

**Inflight Models**: Tracks concurrent dispatch state (which models have active inferences). Critical for S3 concurrent scheduling. **No visibility.**

**Intent Tracking**: S6 feature fully implemented (declarations, resolution, priority elevation) but **no query API**. Can't list active intents or check if a profile is in use.

---

### 5. A2A Models (models.py, taskstore.py, a2a.py)

#### `A2ATaskRecord`
```python
task_id: str = uuid4()[:12]
context_id: str = uuid4()[:12]
state: A2ATaskState
skill_id: str
input_params: dict[str, Any]
output_artifacts: list[dict[str, Any]]
error: str | None
created_at: float = time.time()
updated_at: float = time.time()
```

**Status**: Fully exposed via `/a2a/tasks/{task_id}`. No drift.

---

#### `CompactedResult` (dataclass)
```python
task_id: str
status: str  # A2ATaskState.value
result_summary: str  # Truncated to 500 chars
error: str | None
completed_at: float  # monotonic timestamp
output_artifacts: tuple  # Immutable copy
```

**Hidden Data**: `completed_at` uses `time.monotonic()` (not epoch seconds). Can't correlate with external timestamps.

---

#### `TaskStore` (internal state)
```python
# Dual-store architecture
_active: dict[str, A2ATaskRecord]
_active_timestamps: dict[str, float]      # NEVER exposed
_completed: OrderedDict[str, CompactedResult]
_tombstones: OrderedDict[str, float]      # NEVER exposed
_subscribers: dict[str, list[asyncio.Queue]]  # NEVER exposed
_pressure_level: BackpressureLevel        # NEVER exposed

def stats() -> dict:  # Returns internal metrics (see below)
```

**`stats()` output** (NEVER queried):
```python
{
    "active_count": int,
    "completed_count": int,
    "tombstone_count": int,      # Evicted task IDs
    "subscriber_count": int,     # SSE connection count
    "pressure_level": str,       # "normal" | "pressure" | "overloaded"
    "maxsize": int
}
```

**Hidden Data**:
- `_active_timestamps` — task submission times (for TTL eviction)
- `tombstones` — IDs of evicted tasks (prevents ID reuse confusion)
- `_subscribers` — SSE fan-out queue state
- `pressure_level` — backpressure state NEVER surfaced

**Impact**: No A2A endpoint returns task store health. Can't see backpressure state or eviction counts.

---

#### `ModelLease`
```python
lease_id: str = uuid4()[:12]
model: str
max_requests: int = 100
remaining_requests: int = 100
expiry: float = monotonic() + 600.0
idle_timeout: float = 60.0
last_activity: float = monotonic()
fencing_token: int = 0
state: LeaseState = ACTIVE
created_at: float = time.time()

def should_release() -> (bool, str)  # Eviction decision logic
def touch() -> None
def use_request() -> int
```

**Hidden Data**: No endpoint lists active leases or shows eviction reasons. Lease state invisible to clients after creation.

---

#### `Reservation` (deprecated, backward compat)
```python
reservation_id: str
model: str
remaining_requests: int
priority: PriorityTier
created_at: float
expires_at: float
```

**Hidden Data**: `A2AHandler._reservations` dict never exposed. No `/a2a/reservations` list endpoint.

---

### 6. Admin API Response Models (models.py)

#### `BrokerStatus`
```python
version: str = __version__
uptime_seconds: float
queue_depth: int
queue_by_model: dict[str, int]
loaded_models: list[LoadedModel]
gpu: GPUStatus
current_model: str | None
total_requests_served: int
total_model_swaps: int
state: str  # "running" | "draining" | "stopped"
vram_ledger: dict[str, Any] | None  # ALWAYS None (not wired up)
```

**Hidden Data**:
- `vram_ledger` field exists but is never populated. Placeholder for future VRAMManager integration.
- `total_requests_served` always 0 (not implemented). Should be `_total_dispatched`.

**Gaps**:
- No `scheduler_state` field (current cooldown, swap rate level, stall reason)
- No `recent_requests` field (ring buffer exists but not surfaced)
- No `circuit_breaker_state` field

---

#### `IntentDeclaration` & `IntentResponse`
```python
# Request
intent_id: str
profile: str | None
model_sequence: list[str] | None
estimated_requests: int
client_id: str
created_at: float

# Response
intent_id: str
resolved_priority: str
model_sequence: list[str]
estimated_requests: int
status: str = "registered"
```

**Status**: Fully exposed. No drift. But no `/broker/intents` list endpoint.

---

### 7. Client Models (bastion-client)

#### `IntentRequest` (client)
```python
profile: str | None
model_sequence: list[str] | None
estimated_requests: int = 10
client_id: str = "anonymous"
```

**Drift**: Matches server `IntentDeclaration` input. No issues.

---

#### `IntentResponse` (client)
```python
intent_id: str
resolved_priority: str
model_sequence: list[str]
estimated_requests: int
status: str
```

**Drift**: Matches server. No issues.

---

#### `VRAMInfo` (client)
```python
total_vram_gb: float = 0.0
used_vram_gb: float = 0.0
free_vram_gb: float = 0.0
loaded_models: list[str] = []
utilization_pct: float = 0.0
```

**Drift**: Client expects structured VRAM info. Server `/broker/status` returns raw `GPUStatus` (MB units) and `LoadedModel[]`. **Mismatch.** Client must compute `VRAMInfo` from `BrokerStatus`.

---

#### `InferenceResult` (client)
```python
model: str
response: str
done: bool = False
total_duration: int | None
eval_count: int | None
raw: dict[str, Any] = {}
```

**Status**: Matches Ollama `/api/generate` response. No drift.

---

### 8. Audit Models (audit.py)

No Pydantic models. Uses plain dicts with `build_audit_event()` function.

**Tiered Event Structure**:
```python
{
    "timestamp": str (ISO8601),
    "event": str,
    "details": dict,
    # Tier 1+ (always)
    "auth_identity_hash": str (SHA-256),  # if auth_token
    "a2a_identity": dict,                 # if A2A task
    "source_ip": str,                     # if available
    # Tier 2+ (default)
    "prompt_hash": str (SHA-256),         # if prompt
    "response_hash": str (SHA-256),       # if response
    # Tier 3 (opt-in)
    "prompt_text": str,
    "response_text": str
}
```

**Hidden Data**: Audit log written to `/tmp/bastion-audit.jsonl` but **no read API**. Can't query audit events via HTTP.

---

### 9. Metrics Models (metrics.py)

No Pydantic models. Uses Prometheus client library types (Counter, Histogram, Gauge).

**Metrics Defined**:
```python
# Request metrics
REQUESTS_TOTAL: Counter[endpoint, status_code, tier]
REQUEST_DURATION: Histogram[endpoint, model, tier]
QUEUE_WAIT_TIME: Histogram[model, tier]

# Queue metrics
QUEUE_DEPTH: Gauge[model]

# Scheduler metrics
MODEL_SWAP_TOTAL: Counter[from_model, to_model]
MODEL_SWAP_DURATION: Histogram[model]
COOLDOWN_WAITS_TOTAL: Counter

# GPU metrics
VRAM_USED_BYTES: Gauge
GPU_TEMPERATURE: Gauge

# A2A metrics
A2A_TASKS_TOTAL: Counter[skill, state]
A2A_ERRORS_TOTAL: Counter[method, error_code]
A2A_TASK_DURATION: Histogram[skill, model, state]
A2A_TASK_QUEUE_WAIT: Histogram[skill, model]
LLM_TIME_TO_FIRST_TOKEN: Histogram[model]
A2A_TASKS_ACTIVE: Gauge[state]
A2A_QUEUE_DEPTH: Gauge[skill, model]
```

**Hidden Data**:
- `COOLDOWN_WAITS_TOTAL` — counts cooldown enforcement events. No dashboard panel.
- `MODEL_SWAP_DURATION` — histogram of swap times. No status endpoint.
- `LLM_TIME_TO_FIRST_TOKEN` — streaming quality metric. Not in status.

**Impact**: Metrics exposed via `/metrics` (Prometheus format) but not parsed/returned in JSON for clients without Prometheus.

---

## Hidden Data Report

### Critical Hidden State (High Value for Observability)

1. **VRAMManager Ledger** (`vram.status()`)
   - **What**: Full VRAM allocation tracking (allocated, reserved, available, per-model breakdown)
   - **Where**: `VRAMManager.status()` method exists, returns rich dict
   - **Why Hidden**: No endpoint calls it. `BrokerStatus.vram_ledger` field exists but always `None`
   - **Fix**: Wire up `vram_ledger` in `/broker/status` handler

2. **Scheduler Swap Rate State**
   - **What**: Current throttle level ("normal"/"warn"/"critical"), rolling swap window, time until next allowed swap
   - **Where**: `Scheduler._swap_rate_level`, `_swap_timestamps`, `_get_swap_cooldown()`
   - **Why Hidden**: Critical crash prevention state never surfaced
   - **Fix**: Add `scheduler_swap_rate` section to `/broker/status`

3. **Recent Requests Ring Buffer**
   - **What**: Last 50 requests with model, tier, queue_wait_s, duration_s, status_code
   - **Where**: `server._recent_requests` (deque, maxlen=50)
   - **Why Hidden**: Perfect for dashboard "Recent Activity" panel but no endpoint returns it
   - **Fix**: Add `GET /broker/recent` endpoint

4. **Queue Request Age & Priority**
   - **What**: Per-request age_seconds, effective_priority score, client_info
   - **Where**: `QueuedRequest.age_seconds` property, `.effective_priority()` method
   - **Why Hidden**: Queue endpoints only return depth counts. No insight into staleness or priority distribution
   - **Fix**: Add `GET /broker/queue/details` with per-request breakdown

5. **Scheduler Stall Diagnostics**
   - **What**: Reason scheduler can't dispatch (at_max_concurrent, swap_cooldown, all_models_inflight)
   - **Where**: `Scheduler.stall_reason`, `stall_time`, `_diagnose_stall()`
   - **Why Hidden**: When queue has depth but nothing dispatches, no visibility into why
   - **Fix**: Add `stall_reason` and `stall_duration_s` to `/broker/status`

6. **Task Store Backpressure**
   - **What**: TaskStore pressure level ("normal"/"pressure"/"overloaded"), active/completed/tombstone counts
   - **Where**: `TaskStore.stats()`, `_pressure_level`
   - **Why Hidden**: A2A endpoints never query task store health
   - **Fix**: Add `GET /a2a/stats` endpoint

7. **Inflight Models & Dispatch State**
   - **What**: Which models have active inferences, count per model, total inflight
   - **Where**: `server._inflight_models` dict, `inflight_count()` function
   - **Why Hidden**: S3 concurrent dispatch state invisible. Can't see why concurrent dispatch is blocked
   - **Fix**: Add `inflight_models` section to `/broker/status`

8. **Intent Tracking State**
   - **What**: Active intent declarations, resolved priorities, model sequences in use
   - **Where**: `server._active_intents`, `_resolved_intents`
   - **Why Hidden**: S6 feature fully implemented but no query API
   - **Fix**: Add `GET /broker/intents` list endpoint

9. **Circuit Breaker State**
   - **What**: Current state (closed/open/half_open), consecutive failures, time until recovery
   - **Where**: `CircuitBreaker.state`, `_consecutive_failures`, `_opened_at`
   - **Why Hidden**: Critical availability signal never surfaced
   - **Fix**: Add `circuit_breaker` section to `/broker/status`

10. **Model Residency Cache Age**
    - **What**: Last refresh timestamp, cache TTL, staleness metric
    - **Where**: `ResidencyState.last_refreshed`, `.age_seconds`
    - **Why Hidden**: Multi-model residency tracking (S3) invisible to clients
    - **Fix**: Add `residency_state` to `/broker/status` with timestamp

---

### Moderate Value Hidden Data

11. **Total Dispatched Count**
    - **What**: `Scheduler._total_dispatched` (total inference requests served)
    - **Why Hidden**: `/broker/status.total_requests_served` always 0 (not wired up)
    - **Fix**: Set `total_requests_served = _scheduler.total_dispatched`

12. **Loaded Model Details**
    - **What**: `LoadedModel.details` dict (quantization, family, parameter_count from Ollama)
    - **Why Hidden**: Dropped when serializing `BrokerStatus.loaded_models`
    - **Fix**: Include `details` in LoadedModel serialization

13. **GPU Safety Check Result**
    - **What**: `GPUStatus.is_safe()` boolean + reason string
    - **Why Hidden**: Scheduler uses it but result never surfaced
    - **Fix**: Add `is_safe` field to GPUStatus in `/broker/status`

14. **VRAM Utilization Percentage**
    - **What**: `GPUStatus.vram_utilization_pct` property
    - **Why Hidden**: Computed but not in JSON response
    - **Fix**: Include computed property in serialization

15. **Computed VRAM Budget**
    - **What**: `GPUConfig.max_vram_gb` (total - headroom)
    - **Why Hidden**: Actual scheduling budget never returned
    - **Fix**: Add `vram_budget_gb` to `/broker/status`

16. **Config Computed Properties**
    - **What**: `ServerConfig.two_port_mode`, `OllamaConfig.base_url`
    - **Why Hidden**: Computed properties not serialized
    - **Fix**: Add computed fields to Pydantic `model_dump()`

17. **Audit Log Queryability**
    - **What**: `/tmp/bastion-audit.jsonl` written but not readable via API
    - **Why Hidden**: No `GET /broker/audit` endpoint
    - **Fix**: Add streaming endpoint for recent audit events

---

## Config Gaps

### Options in Code but Missing from Example Config

1. **`ollama.unload_timeout_seconds`**
   - **In Code**: `OllamaConfig.unload_timeout_seconds = 10.0`
   - **In ref-broker-config.yaml**: Present (line 12)
   - **In ref-broker-example-config.yaml**: **Missing**
   - **Impact**: Example config users get default 10s timeout with no visibility

2. **`proxy.max_request_body_bytes`**
   - **In Code**: `ProxyConfig.max_request_body_bytes = 10 * 1024 * 1024`
   - **In Example**: **Missing**
   - **Impact**: 10MB limit not documented for new users

3. **`scheduler.residency_cache_ttl_seconds`**
   - **In Code**: Present (default 1.0)
   - **In Example**: **Missing**
   - **Impact**: S3 cache tuning knob undocumented

4. **`scheduler.loop_interval_seconds`**
   - **In Code**: Present (default 0.1)
   - **In Example**: **Missing**
   - **Impact**: Scheduler wake interval not tunable without code reading

5. **`scheduler.max_concurrent_dispatches`**
   - **In Code**: Present (default 3)
   - **In Full**: Present (line 52, value 3 with comment)
   - **In Example**: **Missing**
   - **Impact**: S3 concurrency limit undocumented in starter config

6. **`scheduler.concurrent_dispatch_delay_seconds`**
   - **In Code**: Present (default 0.1)
   - **In Full**: Present (line 53)
   - **In Example**: **Missing**
   - **Impact**: Power transient stagger knob missing

7. **`scheduler.queue_ttl_seconds`**
   - **In Code**: Present (default 600.0)
   - **In Full**: Present (line 54)
   - **In Example**: **Missing**
   - **Impact**: Queue staleness sweep policy undocumented

8. **`gpu.nvidia_smi_timeout_seconds`**
   - **In Code**: Present (default 5)
   - **In Full**: Present (line 31)
   - **In Example**: **Missing**
   - **Impact**: Health check timeout not tunable

---

### Options in Config but Not Used

**None found.** All config fields are read and used at runtime.

---

## Client-Server Model Drift

### 1. `VRAMInfo` Structure Mismatch

**Client Expectation** (`bastion_client/models.py`):
```python
class VRAMInfo(BaseModel):
    total_vram_gb: float = 0.0
    used_vram_gb: float = 0.0
    free_vram_gb: float = 0.0
    loaded_models: list[str] = []
    utilization_pct: float = 0.0
```

**Server Returns** (`BrokerStatus.gpu` is `GPUStatus`):
```python
class GPUStatus(BaseModel):
    temperature_c: int | None
    vram_used_mb: int | None    # MB units, not GB
    vram_free_mb: int | None    # MB units, not GB
    vram_total_mb: int | None   # MB units, not GB
    power_draw_watts: float | None
```

**Impact**: Client must manually transform MB→GB and extract `loaded_models` from `BrokerStatus.loaded_models`. No `utilization_pct` returned.

**Fix**: Either add `VRAMInfo` response model to server, or update client to use `GPUStatus`.

---

### 2. `BrokerStatus.total_requests_served` Always Zero

**Client Assumption**: Field tracks total inference count.

**Server Reality**: Field exists but never populated (always 0).

**Fix**: Set `total_requests_served = _scheduler.total_dispatched` in `/broker/status`.

---

### 3. `BrokerStatus.vram_ledger` Always None

**Schema Declaration**: `vram_ledger: dict[str, Any] | None`

**Server Reality**: Always `None` (not wired up to `VRAMManager.status()`).

**Impact**: Clients can't access VRAM allocation ledger even when VRAMManager is active.

**Fix**: Populate field when `_vram_manager is not None`.

---

## Key Files for Analysts

### Core Data Definitions
1. **`src/bastion/models.py`** (528 lines)
   - All Pydantic models (32 total)
   - Configuration schema (14 sections)
   - Request/response types
   - A2A task models
   - GPU state models

### Internal State
2. **`src/bastion/server.py`** (900+ lines)
   - Global state dicts (`_pending_grants`, `_inflight_models`, `_active_intents`)
   - `_recent_requests` ring buffer (50 items, never exposed)
   - `_enqueue_request()` coordination logic

3. **`src/bastion/scheduler.py`** (710 lines)
   - `Scheduler` class with 17 internal state fields
   - Swap rate limiter state (`_swap_timestamps`, `_swap_rate_level`)
   - Stall diagnostics (`stall_reason`, `stall_time`)

4. **`src/bastion/vram.py`** (616 lines)
   - `VRAMManager` ledger (assume/confirm/forget pattern)
   - `ResidencyCache` state (last_refreshed, TTL)
   - VRAM snapshot journal (`/tmp/bastion-vram-journal.jsonl`)

5. **`src/bastion/queue.py`** (200 lines)
   - `AffinityQueue` per-model sub-queues
   - `QueuedRequest.age_seconds`, `.effective_priority()`

### A2A State
6. **`src/bastion/taskstore.py`** (439 lines)
   - `TaskStore` dual-store architecture
   - `stats()` method (never queried)
   - Backpressure state machine

7. **`src/bastion/a2a.py`** (1894 lines)
   - `A2AHandler._reservations`, `_leases`
   - `build_extended_card()` — model registry
   - Skill handlers

### Config & Audit
8. **`src/bastion/config.py`** (74 lines)
   - Config search paths
   - ModelInfo transformation

9. **`src/bastion/audit.py`** (150+ lines)
   - `build_audit_event()` tiered structure
   - Identity/content hashing

10. **`docs/audit/ref-broker-config.yaml`** (222 lines)
    - Full config with all options documented
    - Compare against `ref-broker-example-config.yaml` for gaps

---

## Recommendations

### Immediate (High ROI)

1. **Expose VRAMManager Ledger**
   - Wire `BrokerStatus.vram_ledger` to `_vram_manager.status()` when available
   - Surfacing allocated/reserved/available breakdown unlocks VRAM debugging

2. **Add Scheduler State to `/broker/status`**
   - New fields:
     ```python
     scheduler_state: {
         "swap_rate_level": str,      # "normal" | "warn" | "critical"
         "swaps_in_window": int,
         "cooldown_remaining_s": float,
         "stall_reason": str | None,
         "stall_duration_s": float,
         "total_dispatched": int
     }
     ```

3. **Create `/broker/recent` Endpoint**
   - Return `_recent_requests` ring buffer (last 50 requests)
   - Perfect for dashboard activity feed

4. **Fix `total_requests_served`**
   - Set `total_requests_served = _scheduler.total_dispatched` in status handler

5. **Add Inflight Models to Status**
   - New field: `inflight_models: dict[str, int]` (model → count)
   - Unlocks S3 concurrent dispatch visibility

---

### Medium Term (Observability)

6. **Create `/broker/queue/details` Endpoint**
   - Return per-request breakdown (model, age_seconds, priority, tier, client_info)
   - Enables queue staleness analysis

7. **Add Circuit Breaker State**
   - New `BrokerStatus.circuit_breaker` section with state, failures, recovery_remaining_s

8. **Expose TaskStore Stats**
   - New `GET /a2a/stats` endpoint
   - Return `TaskStore.stats()` output

9. **Add Intent Query Endpoint**
   - `GET /broker/intents` → list active `IntentDeclaration` objects

10. **Include Residency Cache Metadata**
    - Add `residency_state` to `/broker/status`:
      ```python
      residency_state: {
          "resident_models": list[str],
          "vram_usage": dict[str, float],
          "last_refreshed": float,
          "age_seconds": float
      }
      ```

---

### Long Term (Advanced)

11. **Audit Log Query API**
    - `GET /broker/audit?since={timestamp}&limit={N}` streaming endpoint
    - Parse `/tmp/bastion-audit.jsonl` and return as JSON array

12. **Metrics JSON Export**
    - `GET /broker/metrics?format=json` alternative to Prometheus format
    - Return parsed metric values for clients without Prometheus

13. **VRAM Snapshot Journal API**
    - `GET /broker/vram-journal?limit={N}`
    - Parse `/tmp/bastion-vram-journal.jsonl` for recent VRAM snapshots

14. **Session Profile Introspection**
    - `GET /broker/profiles` → list configured session profiles
    - `GET /broker/profiles/{profile_id}` → show model sequence

15. **Config Validation Endpoint**
    - `GET /broker/config/validate` → compare running config against schema
    - Identify unused fields, type mismatches, deprecated options

---

## Summary Statistics

- **Total Models**: 32 (Pydantic + dataclasses)
- **Config Sections**: 14 (BrokerConfig sub-objects)
- **Hidden Computed Properties**: 12
- **Never-Exposed Internal State Dicts**: 8 (server.py, scheduler.py, vram.py, taskstore.py, a2a.py)
- **Config Options in Code but Missing from Example**: 8
- **Client-Server Model Mismatches**: 3
- **Rich Data Structures Reduced to Simple Types**: 6 (VRAMManager ledger, swap rate state, recent requests, queue details, task store stats, residency cache)
- **Scheduler/Queue Internal Fields Never Surfaced**: 17

**Key Insight**: BASTION tracks significantly more operational data than it exposes. The gap between internal state and API responses represents untapped observability potential. Most high-value hidden data already exists in working code—it just needs endpoint wiring.

---

**End of Report**
