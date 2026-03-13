# BASTION Audit Synthesis -- Definitive Single-Source-of-Truth

**Generated**: 2026-03-13
**Synthesized by**: Master Synthesizer (Claude Opus 4.6)
**Input**: 3 scout reports, 8 analyst reports, 3 reference documents
**Scope**: Complete audit of 21 source modules, ~11,000 LOC, 29 test files, ~450+ tests

---

## 1. Executive Summary

BASTION (Batch Affinity Scheduler for Throttled Inference on Ollama Networks) is a GPU/LLM broker that sits as a transparent HTTP proxy on port 11434, forwarding to Ollama on port 11435. Born from a crash investigation on an RTX 5090 (32 GB VRAM), it prevents GPU crashes from concurrent model loading through four defense layers: `use_mmap: false` injection, VRAM budget enforcement, serialized scheduling with affinity-aware dispatch, and dynamic cooldown escalation. The system exposes 58 HTTP endpoints across three API layers (proxy, admin, A2A), a 15-panel Textual TUI dashboard, and supports optional Prometheus metrics and OpenTelemetry tracing.

This audit mobilized 11 specialist agents across two phases. The code cartography scout mapped 21 modules with zero dead code, zero circular dependencies, and excellent modularity. The API surface scout cataloged all 58 endpoints and found zero undocumented routes. The data model scout inventoried 32 Pydantic models and discovered 12 hidden computed properties, 8 config gaps, and 3 client-server model mismatches. Eight domain analysts then deep-dived into scheduling algorithms, VRAM management, A2A protocol implementation, metrics/telemetry, dashboard UI, security/resilience, configuration/integration, and test coverage.

The overarching finding is that BASTION is **architecturally sound and feature-complete for its primary mission** -- single-GPU crash prevention and inference scheduling. However, the system **computes significantly more data than it exposes**. The gap between internal state richness and API/UI surface represents the largest unrealized opportunity. Specifically: 9 of 17 Prometheus metrics are defined but never emitted, 3 of 5 OpenTelemetry span types exist but are never used, the VRAMManager ledger is fully implemented but invisible via API, the scheduler's crash-prevention swap rate state is never surfaced, and 17 scheduler/queue internal fields that provide deep operational insight are hidden from operators. The infrastructure for full observability exists -- the gap is purely in wiring, making most improvements low-effort and high-impact.

---

## 2. Unrealized Capabilities

### Tier 1: Low-Hanging Fruit (data exists, just needs an endpoint or config option)

These items require fewer than 20 lines of code each. The data is already computed; it just needs to be returned.

| # | Capability | What Exists | What Is Missing | Files | Effort |
|---|-----------|------------|-----------------|-------|--------|
| T1-01 | **VRAMManager ledger in status** | `VRAMManager.status()` returns full ledger (allocated, reserved, available, per-model). `BrokerStatus.vram_ledger` field exists. | Field is always `None` -- never wired to `VRAMManager.status()`. | `server.py` (status handler), `vram.py` (VRAMManager.status) | 1 line |
| T1-02 | **`total_requests_served` fix** | `Scheduler._total_dispatched` counts all dispatched requests. `BrokerStatus.total_requests_served` field exists. | Field always returns 0 -- not wired to `_total_dispatched`. | `server.py` (status handler) | 1 line |
| T1-03 | **Swap rate level in status** | `Scheduler._swap_rate_level` tracks "normal"/"warn"/"critical". | Never returned in any API response. The primary crash-prevention mechanism is invisible to operators. | `server.py` (status handler), `scheduler.py` | 3-5 lines |
| T1-04 | **Scheduler stall diagnostics** | `Scheduler.stall_reason` and `Scheduler.stall_time` compute why the scheduler is blocked. | Partially exposed via `/broker/queue` but not in `/broker/status`. No dedicated diagnostics endpoint. | `server.py` | 5-10 lines |
| T1-05 | **Inflight models in status** | `server._inflight_models` dict tracks which models have active inferences. | Never returned in `/broker/status`. S3 concurrent dispatch state is invisible. | `server.py` | 2 lines |
| T1-06 | **GPU computed properties** | `GPUStatus.vram_utilization_pct` and `GPUStatus.is_safe()` exist as computed properties. | Lost during Pydantic serialization (`@property` not in `model_dump()`). | `models.py` | 5 lines (add computed fields) |
| T1-07 | **LoadedModel details exposure** | `LoadedModel.details` dict captured from Ollama `/api/ps` (quantization, family, parameter count). | Dropped during serialization of `BrokerStatus.loaded_models`. | `vram.py`, `models.py` | 2 lines |
| T1-08 | **Circuit breaker state in status** | `CircuitBreaker.state`, `._consecutive_failures`, `._opened_at` track availability. | Never returned in `/broker/status`. Critical availability signal invisible. | `server.py`, `circuitbreaker.py` | 5 lines |
| T1-09 | **TaskStore stats endpoint** | `TaskStore.stats()` returns active/completed/tombstone counts, subscriber count, pressure level. | No endpoint calls this method. No A2A health visibility. | `server.py`, `a2a.py` | 5-10 lines |
| T1-10 | **VRAM budget in status** | `GPUConfig.max_vram_gb` (total - headroom) is the actual scheduling budget. | Never returned. Dashboard hardcodes `VRAM_BUDGET_GB = 26.0`. | `server.py`, `dashboard.py` | 3 lines |

### Tier 2: Moderate Effort (infrastructure exists, needs wiring)

These items require 15-50 lines of code. The supporting infrastructure is in place.

| # | Capability | What Exists | What Is Missing | Files | Effort |
|---|-----------|------------|-----------------|-------|--------|
| T2-01 | **Wire 9 dormant Prometheus metrics** | `metrics.py` defines 17 metrics with helper functions. Only 7 are called from production code. | `record_model_swap()`, `record_cooldown_wait()`, `update_queue_depth()`, `update_vram_usage()`, `update_gpu_temperature()`, `record_model_swap_duration()`, `record_queue_wait()`, `update_a2a_queue_depth()` -- all defined with helpers, never called. | `scheduler.py`, `queue.py`, `proxy.py`, `server.py` | ~15 lines across 3-4 files |
| T2-02 | **Wire 3 dormant OTel spans** | `telemetry.py` defines `bastion.scheduler.queue_wait`, `bastion.scheduler.model_swap`, `bastion.ollama.inference` span types. | Never used. Proxy pipeline has zero tracing -- only A2A path has spans. | `scheduler.py`, `proxy.py` | ~10 lines |
| T2-03 | **Periodic gauge update task** | Scheduler loop already runs periodic ticks. GPU/VRAM data queried every tick. | No background task updates Prometheus gauges for VRAM, temperature, queue depth, circuit breaker state. | `server.py` (new background task) | ~30 lines |
| T2-04 | **RequestID middleware** | FastAPI middleware pattern well-established in codebase (`MetricsMiddleware`, `RateLimitMiddleware`). | No `X-Request-ID` injection. Cannot correlate requests across audit events, metrics, and traces. | New middleware class, `server.py` | ~20 lines |
| T2-05 | **Audit log query endpoint** | Audit log written to JSONL with rotating file handler. Rich structured events with identity hashing. | No read API. File-based only. Dashboard synthesizes fake audit events from recent requests. | `server.py`, `audit.py` | ~30 lines |
| T2-06 | **Queue detail endpoint** | `QueuedRequest.age_seconds`, `.effective_priority()`, `.client_info`, `.tier` all computed per request. | `/broker/queue` returns only depth counts. No per-request visibility into age, priority scores, or caller identity. | `server.py` | ~20 lines |
| T2-07 | **Config dump endpoint** | `BrokerConfig` loaded at startup, all defaults resolved. | No way to inspect effective running config via API. No reload mechanism. | `server.py` | ~15 lines |
| T2-08 | **Rate limiting on admin routes (two-port)** | `RateLimitMiddleware` exists and works on proxy app. | Not applied to `create_admin_app()`. Admin endpoints like `/broker/preload` and `/broker/drain` are unprotected in two-port mode. | `server.py` line ~1172 | 1 line |
| T2-09 | **A2A client library** | Server has complete A2A endpoints. Client library (`bastion-client`) wraps only admin API (3 methods). | No `create_task()`, `get_task()`, `stream_task()`, `preload()`, `heartbeat_lease()`, `release_lease()` methods. | `clients/bastion-client/bastion_client/client.py` | ~100 lines |
| T2-10 | **Lease listing endpoint** | `A2AHandler._leases` dict tracks all active leases with state, fencing tokens, remaining requests. | No `/a2a/leases` endpoint exists. Lease state invisible after creation. | `server.py`, `a2a.py` | ~15 lines |

### Tier 3: Strategic Enhancements (requires new code but builds on existing patterns)

These items require 50-200+ lines of new code but leverage existing architectural patterns.

| # | Capability | Foundation | Enhancement | Effort |
|---|-----------|-----------|-------------|--------|
| T3-01 | **Intent-aware predictive pre-loading** | `IntentDeclaration` with `model_sequence` fully implemented. Session profiles define model sequences. Scheduler has swap infrastructure. | When intent declares sequence [A, B, C] and A is being served, pre-load B in background if VRAM permits. Halves swap latency. | Medium-High |
| T3-02 | **Lease-aware inference** | `ModelLease` with fencing tokens, `use_request()`, `remaining_requests` fully implemented. | `infer` skill ignores leases. Add optional `lease_id` parameter to validate, decrement, and reject expired. | Medium |
| T3-03 | **SSE status streaming** | SSE infrastructure solid in A2A (`subscribe_task()`, `_sse_wrapper()`, heartbeats). | No `GET /broker/status/stream` for push-based dashboard updates. Dashboard polls every 2 seconds. | Medium |
| T3-04 | **Dashboard SSE consumption** | Dashboard is HTTP-isolated. `httpx.AsyncClient.stream()` compatible with Textual workers. A2A SSE endpoint exists. | Dashboard does not consume any SSE streams. Could show live batch_infer progress. | Medium |
| T3-05 | **Per-client fairness** | `QueuedRequest.client_info` captures User-Agent. Priority system supports per-request scoring. | No per-client quotas or fairness tracking. One aggressive agent can monopolize scheduling. | Medium |
| T3-06 | **Environment variable config overrides** | `config.py` loads YAML with Pydantic models. Convention of `BASTION_<SECTION>_<KEY>` straightforward. | Zero environment variable overrides exist. Blocks container-native deployment (12-factor). | Medium |
| T3-07 | **Config validation with constraints** | All config models use Pydantic v2. | Zero `Field(ge=, le=)` constraints, zero `@model_validator` decorators. Negative cooldowns, headroom > total VRAM accepted silently. | Medium |
| T3-08 | **Cooperative task cancellation** | `cancel_task()` changes state. Background `asyncio.Task` created per A2A task. | No actual task cancellation. GPU resources continue to be consumed after "cancel". Need to store task refs and call `.cancel()`. | Medium |
| T3-09 | **Multi-GPU per-device tracking** | nvidia-smi naturally outputs one line per GPU. `GPUConfig` extensible. `VRAMManager` tracks per-model. | Single GPU deeply embedded: `health.py` parses only first line, scheduler has single `_current_model`, VRAMManager has single pool. | High |
| T3-10 | **Grafana dashboard templates** | `/broker/metrics` endpoint returns Prometheus format. 35+ metrics defined. | No example dashboard JSON, no scrape config snippet, no alerting rules. | Low (config-only) |

---

## 3. Hidden Data Inventory

Every piece of data the system computes but does not expose, organized by source module.

### `scheduler.py` -- Scheduling Engine Internals

| Data | Location | Type | Value to Operators |
|------|----------|------|-------------------|
| `_total_dispatched` | `Scheduler._total_dispatched` | `int` | Total inference requests served since startup. `BrokerStatus.total_requests_served` exists but returns 0. |
| `_swap_rate_level` | `Scheduler._swap_rate_level` | `str` ("normal"/"warn"/"critical") | Current throttle state. When "critical", cooldown jumps to 10s. Primary crash-prevention signal. |
| `_swap_timestamps` | `Scheduler._swap_timestamps` | `deque[float]` | Rolling window of swap times within 60s. Raw data for swap rate visualization. |
| `stall_reason` | `Scheduler.stall_reason` property | `str` | Why the scheduler cannot dispatch ("at_max_concurrent", "swap_cooldown", "all_models_inflight"). |
| `stall_time` | `Scheduler.stall_time` property | `float` | When the current stall began. Enables stall duration calculation. |
| Effective cooldown | `_get_swap_cooldown()` return | `float` (2.0/5.0/10.0) | The actual cooldown in effect (differs from base `cooldown_seconds`). |

### `vram.py` -- VRAM Ledger and Tracking

| Data | Location | Type | Value to Operators |
|------|----------|------|-------------------|
| `VRAMManager._allocated` | VRAMManager internal | `int` (bytes) | Committed VRAM for loaded models. |
| `VRAMManager._reserved` | VRAMManager internal | `int` (bytes) | Pending VRAM for models being loaded. |
| `VRAMManager.available_vram` | Computed property | `int` (bytes) | Free VRAM for new reservations. |
| `VRAMManager._model_allocations` | VRAMManager internal | `dict[str, int]` | Per-model committed VRAM in bytes. |
| `VRAMManager._reservations` | VRAMManager internal | `dict[str, VRAMReservation]` | Active VRAM reservations with model, bytes, age, committed status. |
| `VRAMManager.status()` | Method (never called by endpoints) | `dict` | Complete ledger: total, safety_margin, allocated, reserved, available, reservations, model_allocations. |
| `ResidencyState.last_refreshed` | Model property | `float` | When the residency cache was last refreshed. |
| `ResidencyState.age_seconds` | Computed property | `float` | Staleness of residency cache. |
| `ResidencyState.vram_usage` | Model field | `dict[str, float]` | Per-model VRAM breakdown (not returned in `/broker/status`). |
| VRAM journal | `/tmp/bastion-vram-journal.jsonl` | JSONL file | Timestamped snapshots of GPU state during model operations. No read API, ephemeral storage, no rotation. |
| `LoadedModel.details` | Captured from Ollama `/api/ps` | `dict` | Quantization, family, parameter count. Dropped during serialization. |
| Ollama `expires_at` | Discarded in `get_loaded_models()` | `str` (ISO datetime) | When Ollama will auto-unload the model. Could inform proactive scheduling. |

### `queue.py` -- Queue Internal State

| Data | Location | Type | Value to Operators |
|------|----------|------|-------------------|
| `QueuedRequest.age_seconds` | Computed property | `float` | How long each request has been waiting. |
| `QueuedRequest.effective_priority()` | Method | `float` | Actual scheduling score including aging and affinity. Reveals priority inversions. |
| `QueuedRequest.client_info` | Field | `str` | User-Agent string identifying the caller. |
| `QueuedRequest.tier` | Field | `PriorityTier` enum | Which priority tier the request was classified as. |
| `get_models_with_requests()` | Method | `list[str]` | Models that have pending work (used internally). |
| `sweep_stale()` return | Method return | `list[QueuedRequest]` | Evicted requests from TTL sweep. Count/ages never surfaced. |

### `server.py` -- Orchestration Layer State

| Data | Location | Type | Value to Operators |
|------|----------|------|-------------------|
| `_inflight_models` | Module global | `dict[str, int]` | Which models have active inferences and count per model. |
| `_pending_grants` | Module global | `dict[str, asyncio.Event]` | Request IDs waiting for scheduler grant. |
| `_pending_completions` | Module global | `dict[str, asyncio.Event]` | Request IDs awaiting Ollama completion. |
| `_active_intents` | Module global | `dict[str, IntentDeclaration]` | Registered intent declarations. |
| `_resolved_intents` | Module global | `dict[str, tuple]` | Resolved intent priorities and sequences. |

### `taskstore.py` -- A2A Task Store Internals

| Data | Location | Type | Value to Operators |
|------|----------|------|-------------------|
| `_active_timestamps` | TaskStore internal | `dict[str, float]` | Task submission times (for TTL eviction). |
| `_tombstones` | TaskStore internal | `OrderedDict[str, float]` | IDs of evicted tasks. No API distinguishes "evicted" from "never existed". |
| `_pressure_level` | TaskStore internal | `BackpressureLevel` enum | "normal"/"pressure"/"overloaded". Invisible to A2A clients. |
| `stats()` output | Method (never called) | `dict` | active_count, completed_count, tombstone_count, subscriber_count, pressure_level, maxsize. |
| `CompactedResult.completed_at` | Dataclass field | `float` (monotonic) | Uses `time.monotonic()` instead of `time.time()` -- cannot correlate with external timestamps. |

### `models.py` -- Computed Properties Lost in Serialization

| Data | Location | Value |
|------|----------|-------|
| `GPUConfig.max_vram_gb` | `@property` | Actual VRAM scheduling budget (total - headroom). Never returned by any endpoint. |
| `GPUStatus.vram_utilization_pct` | `@property` | Computed VRAM percentage. Not in JSON responses. |
| `GPUStatus.is_safe()` | Method | GPU health gate decision. Used by scheduler but never surfaced. |
| `ServerConfig.two_port_mode` | `@property` | Whether two-port mode is active. |
| `OllamaConfig.base_url` | `@property` | Computed Ollama target URL. |
| `ProxyConfig.scheduled_endpoints` | `set[str]` | Which endpoints go through the queue. No endpoint returns the routing table. |
| `SessionProfile` | Config model | Model sequences with priorities. No query API (`/broker/profiles`). |

### `metrics.py` -- Defined but Never Emitted

| Prometheus Metric | Helper Function | Status |
|-------------------|----------------|--------|
| `bastion_queue_wait_seconds` | `record_queue_wait()` at line 279 | DEAD -- never called |
| `bastion_queue_depth` | `update_queue_depth()` at line 294 | DEAD -- never called |
| `bastion_model_swap_total` | `record_model_swap()` at line 307 | DEAD -- never called |
| `bastion_model_swap_duration_seconds` | `record_model_swap_duration()` at line 350 | DEAD -- never called |
| `bastion_cooldown_waits_total` | `record_cooldown_wait()` at line 323 | DEAD -- never called |
| `bastion_vram_used_bytes` | `update_vram_usage()` at line 328 | DEAD -- never called |
| `bastion_gpu_temperature_celsius` | `update_gpu_temperature()` at line 339 | DEAD -- never called |
| `bastion_a2a_queue_depth` | `update_a2a_queue_depth()` at line 456 | DEAD -- never called |
| `bastion_llm_time_to_first_token_seconds` | `observe_llm_ttft()` at line 430 | PARTIAL -- only A2A streaming, not proxy streaming |

### `telemetry.py` -- Defined but Never Used Spans

| Span Name | Kind | Status |
|-----------|------|--------|
| `bastion.scheduler.queue_wait` | INTERNAL (line 351) | DEAD -- never wrapped around `grant_event.wait()` |
| `bastion.scheduler.model_swap` | INTERNAL (line 391) | DEAD -- never wrapped around swap logic |
| `bastion.ollama.inference` | CLIENT (line 426) | DEAD -- never wrapped around httpx calls to Ollama |

---

## 4. Missing API Endpoints

Endpoints that should exist based on available internal data, organized by priority.

### High Priority

| Endpoint | Method | Data Source | Response Shape |
|----------|--------|-------------|----------------|
| `GET /broker/scheduler/diagnostics` | GET | `Scheduler` internals | `{ current_model, stall_reason, stall_duration_seconds, swap_rate_level, swaps_in_window, effective_cooldown_seconds, cooldown_remaining_seconds, total_dispatched, loop_iteration }` |
| `GET /broker/queue/details` | GET | `AffinityQueue` per-request data | `{ requests: [{ id, model, tier, age_seconds, effective_priority, endpoint, client_info }] }` |
| `GET /broker/inflight` | GET | `server._inflight_models`, `_pending_grants`, `_pending_completions` | `{ inflight_models: { model: count }, inflight_total, pending_grants: [req_ids], pending_completions: [req_ids] }` |
| `GET /a2a/stats` | GET | `TaskStore.stats()` | `{ active_count, completed_count, tombstone_count, subscriber_count, pressure_level, maxsize }` |
| `GET /a2a/leases` | GET | `A2AHandler._leases` | `{ leases: [{ lease_id, model, remaining_requests, ttl_remaining, idle_timeout, fencing_token, state }] }` |

### Medium Priority

| Endpoint | Method | Data Source | Response Shape |
|----------|--------|-------------|----------------|
| `GET /broker/residency` | GET | `VRAMTracker.residency_cache` | `{ resident_models, vram_usage: { model: gb }, last_refreshed, cache_age_seconds, stale }` |
| `GET /broker/config` | GET | `BrokerConfig` (running config) | Full resolved config with defaults applied (redact auth tokens) |
| `GET /broker/audit` | GET | `/tmp/bastion-audit.jsonl` | `{ events: [{ timestamp, event_type, details }], total, truncated }` with `?limit=N&since=T&event_type=X` filters |
| `GET /broker/intents` | GET | `server._active_intents` | Already exists but poorly documented. Verify behavior. |
| `GET /broker/profiles` | GET | `BrokerConfig.session_profiles` | `{ profiles: { name: { model_sequence, default_priority, description } } }` |

### Lower Priority

| Endpoint | Method | Data Source |
|----------|--------|-------------|
| `POST /broker/config/reload` | POST | Re-read `broker.yaml`, update mutable config values |
| `GET /broker/metrics?format=json` | GET | Prometheus metrics parsed into JSON for non-Prometheus clients |
| `GET /broker/vram-journal` | GET | Parse `/tmp/bastion-vram-journal.jsonl` with `?limit=N` |
| `GET /broker/status/stream` | GET (SSE) | Push-based status updates for real-time dashboards |

---

## 5. Dashboard Enhancement Map

### Data the TUI Could Show but Does Not

| Panel | Missing Data | Source | Impact |
|-------|-------------|--------|--------|
| **GPU Panel** | Fan speed, GPU clock, PCIe link gen, P-state | nvidia-smi (additional query flags) | Richer GPU health picture at near-zero query cost |
| **Queue Panel** | Per-request age, effective priority, client info | `QueuedRequest` properties | Reveals staleness and priority inversions |
| **Queue Panel** | Stall duration (not just reason) | `Scheduler.stall_time` | Shows how long the scheduler has been blocked |
| **Scheduler Panel** | Swap rate level (normal/warn/critical) | `Scheduler._swap_rate_level` | Critical crash-prevention visibility |
| **Scheduler Panel** | Total dispatched (not always 0) | `Scheduler._total_dispatched` | Actual throughput counter |
| **VRAM Ledger Panel** | VRAM budget from config (not hardcoded 26 GB) | `GPUConfig.max_vram_gb` | Correct budget display when config changes |
| **A2A Task Panel** | Live SSE streaming for batch_infer progress | `/a2a/tasks/{id}/stream` | Real-time per-prompt results instead of polling |
| **A2A Task Panel** | TaskStore backpressure level | `TaskStore.stats()` | Warn before hitting capacity |
| **Audit Panel** | Real audit events (not synthesized) | `/broker/audit` (needs endpoint) | Genuine audit trail instead of faked events |
| **Lease Panel** | Lease data from API (currently always empty) | `/broker/status` (needs `leases` field) | Lease state visibility |
| **(New) Intent Panel** | Active intent declarations | `/broker/intents` | Pipeline stage awareness |
| **(New) Swap Rate Panel** | Swap rate visualization, cooldown state timeline | `Scheduler._swap_timestamps` | Crash prevention monitoring |

### Alert Conditions That Should Exist

| Condition | Source | Current Status |
|-----------|--------|---------------|
| Circuit breaker OPEN | `/broker/health` | Not alerted |
| Watchdog: Ollama unhealthy | `/broker/watchdog` | Not alerted |
| Watchdog: GPU timeout | `/broker/watchdog` | Not alerted |
| Swap rate at critical level | `Scheduler._swap_rate_level` | Not alerted (data not exposed) |
| A2A task store at backpressure | `TaskStore._pressure_level` | Not alerted (data not exposed) |
| Model load failure | Circuit breaker failure count | Not alerted |

### Dashboard Architecture Improvements

| Improvement | Current State | Impact |
|-------------|--------------|--------|
| Read alert thresholds from config | Hardcoded in `AlertPanel` (85%, 82C, etc.) | Config-driven alerting |
| Parallelize supplemental fetches | Sequential after main poll | Faster refresh cycle |
| Persist sparkline data | In-memory `deque(maxlen=60)` lost on restart | Cross-restart trend continuity |
| Textual Web (`textual-serve`) | TUI-only, requires SSH | Browser access with minimal effort |
| Desktop notifications for critical alerts | TUI-only visibility | Alert when terminal not focused |

---

## 6. Observability Roadmap

### Phase 1: Wire Existing Infrastructure (Effort: LOW, Impact: CRITICAL)

**Action**: Add ~25 lines of code across 4 files to activate 9 dormant Prometheus metrics and 3 dormant OTel spans.

**In `scheduler.py`** (`_handle_swap_dispatch`):
- Call `record_model_swap(from_model, to_model)` after `self._total_swaps += 1`
- Call `record_model_swap_duration(model, duration)` with timing around swap operation
- Call `record_cooldown_wait()` at the cooldown sleep branch

**In `queue.py`**:
- Call `update_queue_depth(model, depth)` after `enqueue()` and `dequeue_for_model()`

**In `proxy.py`** (`_handle_scheduled`):
- Call `record_queue_wait(model, tier, wait_seconds)` after computing `queued.age_seconds`

**In `scheduler.py` / `proxy.py`** (OTel):
- Wrap `grant_event.wait()` in `with record_queue_wait(request_id, model):`
- Wrap swap logic in `with record_model_swap(from_model, to_model):`
- Wrap Ollama httpx call in `with record_inference(model, operation, endpoint):`

### Phase 2: Periodic Gauge Updates (Effort: LOW, Impact: HIGH)

Add a background task in `server.py` (similar to `_queue_sweep_loop`) running every 5 seconds:
- `update_vram_usage()` from `VRAMTracker`
- `update_gpu_temperature()` from `health.query_gpu_status()`
- `update_queue_depth()` for all models
- Update VRAMManager gauges (allocated, reserved, available)
- Update circuit breaker state gauge
- Update resident models count gauge

### Phase 3: New Metrics (Effort: MEDIUM, Impact: HIGH)

Define 11-15 new Prometheus metrics in `metrics.py`:

| Metric | Type | Source |
|--------|------|--------|
| `bastion_vram_allocated_bytes` | Gauge | `VRAMManager._allocated` |
| `bastion_vram_reserved_bytes` | Gauge | `VRAMManager._reserved` |
| `bastion_vram_available_bytes` | Gauge | `VRAMManager.available_vram` |
| `bastion_vram_per_model_bytes` | Gauge (label: model) | `VRAMManager._model_allocations` |
| `bastion_circuit_breaker_state` | Gauge (0/1/2) | `CircuitBreaker.state` |
| `bastion_circuit_breaker_failures_total` | Counter | `CircuitBreaker._consecutive_failures` |
| `bastion_watchdog_ollama_latency_seconds` | Histogram | `ProcessMonitor._status.ollama_latency_ms` |
| `bastion_watchdog_gpu_latency_seconds` | Histogram | `ProcessMonitor._status.gpu_query_latency_ms` |
| `bastion_scheduler_stall_seconds` | Histogram (label: reason) | Stall duration by reason |
| `bastion_swap_rate_level` | Gauge (0/1/2) | `Scheduler._swap_rate_level` |
| `bastion_resident_models_count` | Gauge | Number of models loaded |

### Phase 4: Grafana + Alerting (Effort: LOW, Impact: HIGH)

Build four Grafana dashboards (JSON templates, zero code changes):
1. **BASTION Overview**: Request flow, GPU health, scheduler performance, A2A lifecycle
2. **Crash Prevention**: Swap rate timeline, cooldown state, VRAM headroom, temperature with thresholds
3. **Queue Analytics**: Depth by model, wait time percentiles, age distribution, sweep events
4. **A2A Operations**: Tasks by skill/state, duration, queue wait, TTFT

Define Prometheus Alertmanager rules for: swap rate critical, circuit breaker open, VRAM pressure >90%, GPU temperature >78C, TTFT p95 >5s, queue depth >100 for 2 minutes.

### Phase 5: Full Pipeline Tracing (Effort: MEDIUM, Impact: MEDIUM)

Add `bastion.proxy.request` SERVER span in `proxy._handle_scheduled()`. Capture `prompt_eval_count` and `eval_count` from Ollama responses via `set_inference_tokens()`. Propagate W3C TraceContext through the full proxy pipeline (not just A2A).

---

## 7. Security Hardening Opportunities

### Critical Findings (17 security + 7 resilience findings from Security and Resilience analyst)

#### Immediate Fixes (HIGH severity)

| ID | Finding | Fix | File |
|----|---------|-----|------|
| SEC-01 | No timing-safe token comparison | Replace `token not in valid_keys` with `hmac.compare_digest()` loop | `auth.py` line 61, 96 |
| SEC-02 | No API key rotation mechanism | Add `POST /broker/auth/rotate` or SIGHUP handler | `auth.py`, `server.py` |
| SEC-06 | X-Forwarded-For spoofable | Add `rate_limit.trusted_proxies` config, only parse XFF from trusted IPs | `ratelimit.py` lines 116-118 |
| SEC-10 | Audit log in `/tmp` (world-readable, volatile) | Default to `/var/log/bastion/`, create with `0600` permissions, expose `audit.log_path` in config | `audit.py` line 158, `server.py` line 404 |
| SEC-14 | A2A open access by default (empty tokens = no auth) | Warn on startup when `a2a.enabled: true` and `tokens: []` | `server.py`, `a2a.py` |

#### Short-Term Hardening (MEDIUM severity)

| ID | Finding | Fix |
|----|---------|-----|
| SEC-03 | No RBAC -- all admin tokens have full access | Add read-only vs read-write token scoping |
| SEC-04 | Proxy routes permanently unauthenticated | Add opt-in auth toggle for proxy routes |
| SEC-07 | Unbounded rate limiter bucket growth | Add LRU eviction, cap at 10,000 entries |
| SEC-08 | No rate limiting on admin routes in two-port mode | Add `RateLimitMiddleware` to `create_admin_app()` |
| SEC-11 | Auth failures not audited | Add `audit.emit("auth_failure", ...)` on 401 |
| SEC-12 | Incomplete audit coverage | Add events for rate limits, CB transitions, admin actions, lease lifecycle |
| SEC-15 | No per-agent identity in A2A | Per-token agent identity and task isolation |
| SEC-16 | No prompt injection protection | Optional content filtering hooks |
| RES-01 | Half-open circuit breaker allows unlimited probes | Add `_probing` flag for single-probe enforcement |
| RES-04 | No memory leak detection in watchdog | Track RSS via `/proc/self/status` |
| RES-05 | Watchdog lacks GPU VRAM monitoring | Extend nvidia-smi query in watchdog |

#### Resilience Improvements (LOW severity)

| ID | Finding | Fix |
|----|---------|-----|
| RES-02 | Circuit breaker not fed into scheduler | Scheduler checks CB state before dispatch |
| RES-06 | No systemd watchdog heartbeat emission | Call `notify_watchdog()` in ProcessMonitor loop |
| RES-07 | No model corruption detection | Optional canary prompt mechanism |
| SEC-13 | No audit log integrity protection | HMAC chain for tamper detection |
| SEC-17 | No model name validation on proxy routes | Validate against `config.models` registry |

---

## 8. Multi-Agent / A2A Evolution

### Current State

The A2A implementation delivers a solid single-task inference broker with four skills (infer, batch_infer, preload, status), hybrid model leases with fencing tokens, dual-store TaskStore with compaction and backpressure, SSE streaming with heartbeats, and three-tier agent card disclosure.

### Gaps in Current Implementation

| Gap | Impact | Difficulty |
|-----|--------|-----------|
| Leases disconnected from inference -- `infer` skill ignores `lease_id` | Request accounting impossible, no priority elevation for leased requests | Medium |
| Session profiles disconnected from A2A -- no skill to declare pipelines | A2A agents cannot benefit from intent-based scheduling | Medium |
| `context_id` unused for scheduling -- exists on tasks but ignored | No pipeline affinity or grouped scheduling | Medium |
| All A2A tasks get `AGENT` priority -- no per-token or per-task priority | Cannot differentiate orchestrator vs worker agents | Low |
| Batch inference sequential only -- no concurrent prompt processing | Underutilizes `OLLAMA_NUM_PARALLEL > 1` | Medium |
| No cooperative task cancellation -- state change only, GPU continues | Wasted compute on canceled tasks | Medium |
| Reservation and Lease are dual objects -- redundant creation in preload | State divergence risk if one expires before the other | Low |
| `CompactedResult.completed_at` uses `time.monotonic()` -- inconsistent with `time.time()` elsewhere | Cannot compute cross-process durations | Low |
| Stale module docstring says "batch_infer: stub" and "preload: stub" | Both are fully implemented | Trivial |

### Evolution Roadmap

**Phase A: Wire Existing Primitives**
1. Add `lease_id` + `fencing_token` parameters to `infer` skill
2. Add `declare_pipeline` skill wrapping `/broker/intent`
3. Use `context_id` in scheduler for pipeline affinity
4. Allow per-task priority specification (not just AGENT default)
5. Return HTTP 410 Gone for tombstoned tasks (instead of 404)

**Phase B: Enhanced Capabilities**
1. Parallel batch processing with `concurrency` parameter
2. Cooperative task cancellation via `asyncio.Task.cancel()`
3. SSE `Last-Event-ID` support for reconnection
4. Batch progress aggregation events ("60% complete")
5. Chat endpoint support in batch_infer (`/api/chat` with messages)

**Phase C: Orchestration Primitives**
1. Task chaining with `depends_on: [task_id]` and artifact forwarding
2. Pipeline skill (meta-skill accepting sequence of skills + models)
3. Callback/webhook notifications on task completion
4. Negotiate-session skill (intent + preload + lease in one call)
5. Per-token agent identity with model/skill-level access control

### Orchestration Patterns This Would Enable

| Pattern | Required Capabilities |
|---------|----------------------|
| **Sequential Pipeline**: preload A -> infer x5 -> preload B -> infer x3 | Lease-aware inference, task chaining |
| **Fan-out / Fan-in**: batch 10 prompts, aggregate | Already supported. Enhancement: parallel processing |
| **Model Council (Quorum)**: infer on A, B, C -> vote | Parallel task submission with shared context_id |
| **RAG Pipeline**: embed query -> retrieve -> infer with context | Task chaining, cross-task data passing |
| **Adaptive Routing**: check status -> route to loaded model | Already possible via status skill + client logic |

---

## 9. Configuration Gaps

### Options in Code but Not Documented/Configurable

| # | Config Path | Default | In Code | In Full Config | In Example Config | Impact |
|---|------------|---------|---------|---------------|-------------------|--------|
| 1 | `ollama.unload_timeout_seconds` | `10.0` | Yes | Yes | No | Unload timeout invisible to example users |
| 2 | `ollama.api_timeout_seconds` | `5.0` | Yes | Yes | No | API timeout hidden |
| 3 | `proxy.max_request_body_bytes` | `10485760` | Yes | Yes | No | 10MB limit undocumented |
| 4 | `proxy.connect_timeout_seconds` | `10.0` | Yes | Yes | No | Connect timeout hidden |
| 5 | `scheduler.residency_cache_ttl_seconds` | `1.0` | Yes | Yes | No | Cache freshness not tunable |
| 6 | `scheduler.loop_interval_seconds` | `0.1` | Yes | Yes | No | Scheduler tick rate hidden |
| 7 | `scheduler.max_concurrent_dispatches` | `3` | Yes | Yes | No | Concurrency limit undocumented |
| 8 | `scheduler.concurrent_dispatch_delay_seconds` | `0.1` | Yes | Yes | No | Power transient stagger hidden |
| 9 | `scheduler.queue_ttl_seconds` | `600.0` | Yes | Yes | No | Queue sweep policy missing |
| 10 | `gpu.nvidia_smi_timeout_seconds` | `5` | Yes | Yes | No | Health check timeout missing |
| 11 | `gpu.max_power_watts` | `450.0` | Yes | Yes | No | Power threshold hidden |
| 12 | `gpu.default_vram_estimate_gb` | `10.0` | Yes | Yes | No | VRAM estimation fallback hidden |
| 13 | `request_overrides.default_num_ctx` | `4096` | Yes | **No** | **No** | Missing from ALL config files |

### Hardcoded Values That Should Be Configurable

| # | Value | Location | Current | Recommended Config Path |
|---|-------|----------|---------|------------------------|
| 1 | Audit log path | `server.py:404`, `audit.py:159` | `/tmp/bastion-audit.jsonl` | `audit.log_path` |
| 2 | Audit max bytes | `server.py:405` | 10 MB | `audit.max_bytes` |
| 3 | Audit backup count | `server.py:406` | 5 | `audit.backup_count` |
| 4 | VRAM journal path | `vram.py:303` | `/tmp/bastion-vram-journal.jsonl` | `audit.vram_journal_path` |
| 5 | Recent requests buffer | `server.py:108` | 50 | `server.recent_buffer_size` |
| 6 | Watchdog check interval | `watchdog.py` | 10s | `watchdog.check_interval_seconds` |
| 7 | Watchdog failure threshold | `watchdog.py` | 3 | `watchdog.failure_threshold` |

### Config Validation Deficiencies

Zero `Field(ge=, le=)` constraints, zero `@field_validator`, zero `@model_validator` decorators. Accepted without error:
- `cooldown_seconds: -1.0` (negative cooldown)
- `max_queue_size: 0` (queue immediately full)
- `headroom_gb > total_vram_gb` (negative VRAM budget)
- `swap_rate_warn_threshold > swap_rate_critical_threshold` (inverted levels)
- Unknown YAML keys (typos silently ignored with defaults applied)

### Client-Server Model Drift

| Drift | Client Expects | Server Returns |
|-------|---------------|---------------|
| VRAM units | `VRAMInfo` with GB, `utilization_pct` | `GPUStatus` with MB, no utilization |
| Request count | `total_requests_served` as throughput | Always 0 (not wired) |
| VRAM ledger | `vram_ledger` as dict | Always None (not wired) |
| `InferenceResult` | Defined and exported | Never instantiated by `infer()` |
| Version string | `pyproject.toml` says `0.2.0` | `__init__.__version__` says `0.1.0` |

### Missing Environment Variable Support

Zero `BASTION_*` environment variable overrides for any config option. Only `BASTION_API_KEY` exists (dashboard only, not client library). This blocks 12-factor container deployment. Recommended pattern: `BASTION_OLLAMA_HOST`, `BASTION_SERVER_PORT`, `BASTION_AUTH_API_KEYS`, etc.

---

## 10. Test Coverage Priorities

### Current Coverage Summary

- **29 test files**, **~130 test classes**, **~450+ test functions**
- **17/20** source modules have direct test files (85%)
- Estimated line coverage: **65-75%** (no `pytest-cov` to confirm)
- Strong for: queue, scheduler, VRAM, circuit breaker, A2A, taskstore, auth, audit, health
- Weak for: server routes, CLI entry, dashboard TUI, A2A metrics

### Priority 1: Critical Gaps (Highest ROI)

| Gap | Why Critical | Effort |
|-----|-------------|--------|
| **Server route handler tests (`test_server.py`)** | All 23+ `/broker/*` and `/a2a/*` route handlers have zero direct tests. Only route existence checked in `test_two_port.py`. Behavior, error responses, and response shapes are untested. | Medium |
| **CLI entry point tests (`test_main.py`)** | `main()` with argparse + uvicorn launch is the application entry point with zero coverage. | Low |
| **A2A metric functions** | 8 metric helper functions in `metrics.py` (`emit_a2a_task`, `emit_a2a_error`, `observe_a2a_task_duration`, `observe_a2a_queue_wait`, `observe_llm_ttft`, `update_a2a_tasks_active`, `update_a2a_queue_depth`, `record_model_swap_duration`) are completely untested. | Low |

### Priority 2: Important Gaps (Medium ROI)

| Gap | Context | Effort |
|-----|---------|--------|
| Dashboard panel `render_data()` methods | 14 panels with pure-function rendering -- easy to test with edge case data (None, empty, extreme). | Medium |
| Dashboard utility functions | `format_countdown()`, `format_bytes_gb()`, `cb_state_color()`, `a2a_state_color()`, `lease_state_color()` -- all pure functions. | Low |
| Two-port mode HTTP behavior | Extend beyond route existence to actual request routing and response verification. | Medium |
| `queue.sweep_stale()` | Stale request cleanup with various age thresholds -- zero direct tests. | Low |
| `VRAMManager.reconcile()` and `release_model()` | Reconciliation logic (tracked vs actual state) has no direct tests. | Medium |

### Priority 3: Nice-to-Have

| Gap | Context |
|-----|---------|
| `@pytest.mark.parametrize` adoption | Many test classes repeat patterns with different inputs. |
| Proxy streaming edge cases | NDJSON interruption, connection drops, partial responses. |
| Client error handling | Connection refused, 500, timeout, malformed JSON. |
| Rate limit temporal behavior | Token refill over time, concurrent same-IP requests. |
| Telemetry with real-ish OTel | Context managers with mocked OTel SDK. |
| `pytest-cov` integration | Measure actual line coverage, set 85% threshold. |

---

## 11. Architecture Recommendations

### 11.1 Extract Callback Registry Pattern

**Current**: `server.py` uses ad-hoc module-level globals and lambda callbacks to wire scheduler, proxy, queue, and VRAM components together. 12 module globals manage state.

**Proposed**: Create a `CallbackRegistry` or `AppState` class that manages all scheduler-server callbacks in one place. Benefits: easier to test scheduler in isolation, cleaner dependency injection, eliminates scattered module globals.

### 11.2 Consolidate Observability Emission

**Current**: Metrics, telemetry, and audit are emitted separately at different call sites with no coordination. Many events are audited but not metricated.

**Proposed**: Create an `ObservabilityBus` that accepts structured events and routes to metrics/telemetry/audit simultaneously. Benefits: single emission point per event, easier to add new backends, guaranteed consistent coverage.

### 11.3 Shared GPU Status Cache

**Current**: `health.py` and `watchdog.py` run independent nvidia-smi subprocess calls. Multiple consumers query GPU state at different times, creating data discrepancy windows.

**Proposed**: Create a shared GPU status cache (similar to `ResidencyCache`) with short TTL. All consumers read from the cache. Single nvidia-smi call per TTL period. Benefits: reduced subprocess overhead, consistent GPU state view, single query point for additional nvidia-smi fields (utilization, clocks, fan speed).

### 11.4 VRAMManager as Canonical VRAM Authority

**Current**: Scheduler owns eviction logic, VRAMManager is a helper. Eviction sort order is hardcoded in scheduler.

**Proposed**: VRAMManager becomes the canonical VRAM allocator. Scheduler queries it for eviction candidates. VRAMManager incorporates recency, reload cost, and lease state into eviction scoring. Benefits: single source of truth for VRAM state, cleaner separation of concerns, easier multi-GPU extension.

### 11.5 Unify Reservation and Lease

**Current**: `_handle_preload()` creates BOTH a Reservation AND a Lease. Dual creation for backward compatibility with scheduler's `has_active_reservation()` check.

**Proposed**: Migrate scheduler to use `has_active_lease()` only. Remove `Reservation` model. Benefits: eliminates state divergence risk, simplifies lease lifecycle.

### 11.6 Version String Consistency

**Current**: `pyproject.toml` declares version `0.2.0`, `__init__.__version__` declares `0.1.0`, client `pyproject.toml` declares `0.1.0`. API consumers see `0.1.0` via `BrokerStatus.version`.

**Proposed**: Use single source of truth. Either read version from `pyproject.toml` at runtime via `importlib.metadata.version("bastion")`, or use a build-time version injection. Benefits: prevents version confusion, enables proper semantic versioning.

### 11.7 CORS Middleware for Web Clients

**Current**: No CORS headers set on any response. Browser-based clients from different origins are blocked.

**Proposed**: Add `CORSMiddleware` to both proxy and admin apps. This is a prerequisite for any web-based dashboard or third-party web integrations.

### 11.8 Container Deployment Artifacts

**Current**: Zero Docker/Kubernetes artifacts. Systemd-only deployment.

**Proposed**: Add `Dockerfile` (multi-stage, NVIDIA base image), `docker-compose.yml` (BASTION + Ollama + Prometheus + Grafana), and example Kubernetes manifests (DaemonSet + Services + ConfigMap). The architecture is container-ready -- needs packaging only.

---

## 12. Future Vision

### What BASTION Could Become Based on Its Existing Foundation

BASTION's architecture -- a transparent proxy with scheduling, observability, and agent-to-agent communication -- positions it as more than a crash prevention tool. With the foundation already in place, BASTION could evolve in three directions:

**Direction 1: Production GPU Inference Platform**

With the observability gaps closed (wiring 9 dormant Prometheus metrics, adding Grafana dashboards, implementing alerting rules), BASTION becomes a production-ready GPU inference platform suitable for team deployments. Add environment variable config overrides and Docker/Kubernetes manifests for container-native deployment. Add RBAC and per-agent identity for multi-tenant security. The infrastructure for all of this exists; it needs wiring and packaging.

**Direction 2: Multi-GPU Orchestration Hub**

The multi-GPU plan (`ref-multi-gpu-plan.md`) extends BASTION from managing one GPU to managing 2-4 GPUs on a single machine. The architecture supports this: nvidia-smi naturally outputs multi-GPU data, `VRAMManager`'s per-model tracking extends to per-GPU-per-model, and the config system can absorb `gpus: list[PerGPUConfig]`. The scheduler is the hardest part -- per-GPU affinity, cooldown, and swap rate limits require substantial refactoring. But the patterns (affinity queue, VRAM ledger, dynamic cooldown) are proven and can be replicated per-GPU.

**Direction 3: Agent Infrastructure Layer**

The A2A protocol, model leases, intent system, and session profiles form the building blocks of a multi-agent infrastructure layer. With task chaining, pipeline skills, and per-agent identity, BASTION could become the scheduling backbone for agent ecosystems -- where multiple AI agents declare their model needs, negotiate priorities, and execute inference pipelines through a single GPU broker. The MCP integration planned in the roadmap would complement A2A, providing a second standard protocol for agent-broker communication. The combination of A2A (agent-to-agent task delegation) and MCP (tool provider for LLM agents) covers both sides of the agent interaction model.

**The Unifying Theme**: BASTION already has the right architectural decisions -- HTTP-isolated dashboard, no circular dependencies, graceful degradation, transport-level circuit breaking, tiered audit logging. The path from "crash prevention tool" to "production GPU platform" is primarily a wiring exercise: connecting internal data to external surfaces, adding configuration and deployment artifacts, and building on proven patterns. The code quality and modularity make this evolution low-risk.

---

## Appendix A: File Reference Index

| Module | Path | LOC | Test Coverage | Key Audit Findings |
|--------|------|-----|--------------|-------------------|
| `__init__.py` | `/home/user/BASTION/src/bastion/__init__.py` | 14 | N/A | Version mismatch (0.1.0 vs pyproject 0.2.0) |
| `__main__.py` | `/home/user/BASTION/src/bastion/__main__.py` | 175 | NONE | Zero test coverage for CLI entry point |
| `a2a.py` | `/home/user/BASTION/src/bastion/a2a.py` | 1894 | HIGH | Stale docstring ("stub"), dual Reservation+Lease, no cooperative cancellation |
| `audit.py` | `/home/user/BASTION/src/bastion/audit.py` | 340 | HIGH | Hardcoded `/tmp` path, no read API, incomplete event coverage |
| `auth.py` | `/home/user/BASTION/src/bastion/auth.py` | 105 | HIGH | No `hmac.compare_digest()`, no key rotation, no RBAC |
| `circuitbreaker.py` | `/home/user/BASTION/src/bastion/circuitbreaker.py` | 336 | HIGH | Unlimited half-open probes, no scheduler integration |
| `config.py` | `/home/user/BASTION/src/bastion/config.py` | 74 | MEDIUM | No env var overrides, no unknown-key warnings |
| `dashboard.py` | `/home/user/BASTION/src/bastion/dashboard.py` | 2159 | LOW | Fake audit events, hardcoded VRAM budget, no SSE, 6/23 endpoints consumed |
| `health.py` | `/home/user/BASTION/src/bastion/health.py` | 133 | HIGH | Parses only first GPU line, missing nvidia-smi fields |
| `metrics.py` | `/home/user/BASTION/src/bastion/metrics.py` | 522 | MEDIUM | 9/17 metrics never emitted, 8 A2A helpers untested |
| `middleware.py` | `/home/user/BASTION/src/bastion/middleware.py` | 138 | LOW | No RequestID, no error classification, no streaming distinction |
| `models.py` | `/home/user/BASTION/src/bastion/models.py` | 528 | MEDIUM | Zero validation constraints, 12 hidden properties, 3 client drift |
| `proxy.py` | `/home/user/BASTION/src/bastion/proxy.py` | 442 | MEDIUM | No model name validation, proxy TTFT not captured |
| `queue.py` | `/home/user/BASTION/src/bastion/queue.py` | 200 | HIGH | `sweep_stale()` untested, per-request data never exposed |
| `ratelimit.py` | `/home/user/BASTION/src/bastion/ratelimit.py` | 163 | MEDIUM | XFF spoofable, unbounded buckets, not on admin app |
| `scheduler.py` | `/home/user/BASTION/src/bastion/scheduler.py` | 710 | HIGH | Zero Prometheus emissions, cooldown blocks Phase 1, stall diagnosis untested |
| `server.py` | `/home/user/BASTION/src/bastion/server.py` | 1561 | LOW | All route handlers untested, 12 module globals, two-port route duplication |
| `taskstore.py` | `/home/user/BASTION/src/bastion/taskstore.py` | 439 | HIGH | `stats()` never queried, monotonic/wall clock mix, backpressure invisible |
| `telemetry.py` | `/home/user/BASTION/src/bastion/telemetry.py` | 506 | MEDIUM | 3/5 spans never used, `set_inference_tokens()` never called |
| `vram.py` | `/home/user/BASTION/src/bastion/vram.py` | 616 | HIGH | Ledger invisible, journal ephemeral/unrotated, fuzzy model matching fragile |
| `watchdog.py` | `/home/user/BASTION/src/bastion/watchdog.py` | 326 | HIGH | No VRAM monitoring, no memory leak detection, no sd_notify heartbeat |

## Appendix B: Cross-Reference of All Findings by Report

| Report | Critical | High | Medium | Low | Total |
|--------|----------|------|--------|-----|-------|
| Scout: Code Cartography | 0 | 0 | 5 unrealized connections | 0 | 5 |
| Scout: API Surface | 0 | 3 missing endpoints | 2 middleware gaps | 4 missing endpoints | 9 |
| Scout: Data Models | 0 | 5 hidden state | 7 moderate hidden data | 5 low-value hidden | 17 |
| Analyst: Scheduler/Queue | 0 | 4 algorithm weaknesses | 5 config interactions | 3 alternative algorithms | 12 |
| Analyst: VRAM/GPU | 3 critical (C1-C3) | 5 high value (H1-H5) | 5 medium (M1-M5) | 5 multi-GPU (G1-G5) | 18 |
| Analyst: A2A Protocol | 0 | 4 functional gaps | 4 observability gaps | 6 enhancements | 14 |
| Analyst: Metrics/Telemetry | 4 critical | 4 high | 4 medium | 3 low | 15 |
| Analyst: Dashboard | 0 | 4 high priority | 4 medium priority | 4 low priority | 12 |
| Analyst: Security/Resilience | 5 HIGH (SEC) | 7 MEDIUM (SEC) + 3 MEDIUM (RES) | 3 LOW (SEC) + 4 LOW (RES) | 0 | 22 |
| Analyst: Config/Integration | 5 critical | 7 high | 7 medium | 5 low | 24 |
| Analyst: Test Coverage | 3 Priority 1 | 5 Priority 2 | 5 Priority 3 | 3 infrastructure | 16 |
| **TOTALS** | **20** | **51** | **54** | **39** | **164** |

---

**End of Synthesis**

This document consolidates findings from 11 specialist agents across 14 reports examining 21 source modules (~11,000 LOC), 29 test files (~450+ tests), and 58 HTTP endpoints. Every item is actionable with specific file references, function names, and line numbers where applicable.
