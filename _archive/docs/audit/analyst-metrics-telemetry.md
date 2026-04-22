# Metrics & Telemetry Analyst Report -- BASTION

**Generated**: 2026-03-13
**Analyst**: Metrics & Telemetry Analyst (Claude Opus 4.6)
**Scope**: Prometheus metrics, OpenTelemetry tracing, middleware recording, observability gaps, and capacity planning potential
**Input**: metrics.py, telemetry.py, middleware.py, models.py, scheduler.py, server.py, a2a.py, proxy.py, queue.py, vram.py, circuitbreaker.py, watchdog.py, plus three scout reports

---

## Executive Summary

BASTION has a well-designed observability framework with **17 Prometheus metrics** (counters, histograms, gauges), **5 OpenTelemetry spans**, and a request metrics middleware. However, the system suffers from a **critical emission gap**: only **7 of 17 defined Prometheus metrics are actually emitted** by any code path, and telemetry spans cover only the A2A layer (not the core proxy/scheduler pipeline). The infrastructure for full observability exists -- the gap is purely in wiring.

**Key Findings**:
- **10 Prometheus helper functions defined but never called** from any production code path
- **Scheduler emits zero Prometheus metrics** -- model swaps, cooldowns, queue depths, VRAM, and GPU temperature are tracked only in audit logs
- **Proxy pipeline has zero OTel spans** -- tracing covers only A2A tasks, not the primary request flow (proxy -> queue -> scheduler -> Ollama)
- **No-op stubs work correctly** -- graceful degradation is well-implemented for both prometheus-client and opentelemetry
- **Rich internal state exists** but is not instrumented: swap rate levels, stall reasons, circuit breaker transitions, VRAM ledger changes
- **Middleware captures only basic request metrics** -- no error classification, no request ID correlation, no response size tracking

---

## 1. Prometheus Metrics Inventory

### 1.1 Defined Metrics (17 total)

| Metric Name | Type | Labels | Status |
|---|---|---|---|
| `bastion_requests_total` | Counter | endpoint, status_code, tier | ACTIVE (middleware) |
| `bastion_request_duration_seconds` | Histogram | endpoint, model, tier | ACTIVE (middleware) |
| `bastion_queue_wait_seconds` | Histogram | model, tier | DEFINED, NEVER EMITTED |
| `bastion_queue_depth` | Gauge | model | DEFINED, NEVER EMITTED |
| `bastion_model_swap_total` | Counter | from_model, to_model | DEFINED, NEVER EMITTED |
| `bastion_model_swap_duration_seconds` | Histogram | model | DEFINED, NEVER EMITTED |
| `bastion_cooldown_waits_total` | Counter | (none) | DEFINED, NEVER EMITTED |
| `bastion_vram_used_bytes` | Gauge | (none) | DEFINED, NEVER EMITTED |
| `bastion_gpu_temperature_celsius` | Gauge | (none) | DEFINED, NEVER EMITTED |
| `bastion_a2a_tasks_total` | Counter | skill, state | ACTIVE (a2a.py) |
| `bastion_a2a_errors_total` | Counter | method, error_code | ACTIVE (a2a.py) |
| `bastion_a2a_task_duration_seconds` | Histogram | skill, model, state | ACTIVE (a2a.py) |
| `bastion_a2a_task_queue_wait_seconds` | Histogram | skill, model | ACTIVE (a2a.py) |
| `bastion_llm_time_to_first_token_seconds` | Histogram | model | ACTIVE (a2a.py streaming only) |
| `bastion_a2a_tasks_active` | Gauge | state | ACTIVE (a2a.py) |
| `bastion_a2a_queue_depth` | Gauge | skill, model | DEFINED, NEVER EMITTED |

### 1.2 Emission Status Summary

**Actually emitted (7/17):**
- `bastion_requests_total` -- via `MetricsMiddleware` -> `record_request()`
- `bastion_request_duration_seconds` -- via `MetricsMiddleware` -> `record_request()`
- `bastion_a2a_tasks_total` -- via `a2a.py` -> `emit_a2a_task()`
- `bastion_a2a_errors_total` -- via `a2a.py` -> `emit_a2a_error()`
- `bastion_a2a_task_duration_seconds` -- via `a2a.py` -> `observe_a2a_task_duration()`
- `bastion_a2a_task_queue_wait_seconds` -- via `a2a.py` -> `observe_a2a_queue_wait()`
- `bastion_a2a_tasks_active` -- via `a2a.py` -> `update_a2a_tasks_active()`

**Partially emitted (1/17):**
- `bastion_llm_time_to_first_token_seconds` -- via `a2a.py` -> `observe_llm_ttft()`, but **only for A2A streaming tasks**; proxy streaming (`proxy.py`) does not emit TTFT

**Never emitted (9/17):**
- `bastion_queue_wait_seconds` -- helper `record_queue_wait()` defined but never called
- `bastion_queue_depth` -- helper `update_queue_depth()` defined but never called
- `bastion_model_swap_total` -- helper `record_model_swap()` defined but never called
- `bastion_model_swap_duration_seconds` -- helper `record_model_swap_duration()` defined but never called
- `bastion_cooldown_waits_total` -- helper `record_cooldown_wait()` defined but never called
- `bastion_vram_used_bytes` -- helper `update_vram_usage()` defined but never called
- `bastion_gpu_temperature_celsius` -- helper `update_gpu_temperature()` defined but never called
- `bastion_a2a_queue_depth` -- helper `update_a2a_queue_depth()` defined but never called

### 1.3 Helper Functions: Defined vs. Called

| Helper Function | Defined In | Called From | Status |
|---|---|---|---|
| `record_request()` | metrics.py:243 | middleware.py:64 | ACTIVE |
| `record_queue_wait()` | metrics.py:279 | (nowhere) | DEAD |
| `update_queue_depth()` | metrics.py:294 | (nowhere) | DEAD |
| `record_model_swap()` | metrics.py:307 | (nowhere) | DEAD |
| `record_cooldown_wait()` | metrics.py:323 | (nowhere) | DEAD |
| `update_vram_usage()` | metrics.py:328 | (nowhere) | DEAD |
| `update_gpu_temperature()` | metrics.py:339 | (nowhere) | DEAD |
| `record_model_swap_duration()` | metrics.py:350 | (nowhere) | DEAD |
| `emit_a2a_task()` | metrics.py:367 | a2a.py | ACTIVE |
| `emit_a2a_error()` | metrics.py:380 | a2a.py | ACTIVE |
| `observe_a2a_task_duration()` | metrics.py:393 | a2a.py | ACTIVE |
| `observe_a2a_queue_wait()` | metrics.py:415 | a2a.py | ACTIVE |
| `observe_llm_ttft()` | metrics.py:430 | a2a.py | PARTIAL |
| `update_a2a_tasks_active()` | metrics.py:443 | a2a.py | ACTIVE |
| `update_a2a_queue_depth()` | metrics.py:456 | (nowhere) | DEAD |
| `get_metrics_text()` | metrics.py:471 | server.py | ACTIVE |

---

## 2. OpenTelemetry Tracing Inventory

### 2.1 Defined Spans (5 span types)

| Span Name | Kind | Defined In | Called From | Status |
|---|---|---|---|---|
| `a2a.task.submit` | PRODUCER | telemetry.py:259 | a2a.py | ACTIVE |
| `a2a.task.process` | CONSUMER | telemetry.py:312 | a2a.py | ACTIVE |
| `bastion.scheduler.queue_wait` | INTERNAL | telemetry.py:351 | (nowhere) | DEAD |
| `bastion.scheduler.model_swap` | INTERNAL | telemetry.py:391 | (nowhere) | DEAD |
| `bastion.ollama.inference` | CLIENT | telemetry.py:426 | (nowhere) | DEAD |

### 2.2 GenAI Semantic Conventions

The telemetry module correctly implements OTel GenAI semantic attributes:
- `gen_ai.request.model` -- set on all inference spans
- `gen_ai.operation.name` -- set on inference spans ("generate", "chat", "embed")
- `gen_ai.provider.name` -- always "ollama"
- `gen_ai.usage.input_tokens` -- available via `set_inference_tokens()`
- `gen_ai.usage.output_tokens` -- available via `set_inference_tokens()`

**Problem**: The `set_inference_tokens()` function is defined but never called. Ollama responses include `prompt_eval_count` and `eval_count` fields, but no code path extracts and records these as OTel attributes.

### 2.3 Trace Context Propagation

The `inject_trace_context()` / `extract_trace_context()` functions correctly implement W3C Trace Context propagation for linking PRODUCER spans (task submission) to CONSUMER spans (task processing). This only works for A2A tasks.

**Gap**: No trace context is propagated through the proxy pipeline. A request flowing through `proxy.py` -> `queue.py` -> `scheduler.py` -> Ollama has no distributed trace.

### 2.4 Configuration Model

The `TelemetryConfig` model (`models.py`) supports three exporters:
- `none` -- spans recorded but not exported (default)
- `console` -- prints spans to stdout (debug)
- `otlp` -- exports via gRPC to OTLP endpoint (production)

**Default: disabled** (`enabled: false`). This is appropriate for a system that primarily targets local single-GPU setups.

---

## 3. Middleware Analysis

### 3.1 What MetricsMiddleware Records

`middleware.py` wraps every incoming HTTP request and captures:
- **Endpoint**: `request.url.path` (e.g., `/api/generate`, `/broker/status`)
- **Model**: parsed from JSON body (POST to `/api/*` only)
- **Tier**: from `X-Broker-Priority` header (defaults to "agent")
- **Duration**: wall-clock time from request start to response completion
- **Status code**: `response.status_code`

These are emitted via `record_request()` to:
- `bastion_requests_total` (counter)
- `bastion_request_duration_seconds` (histogram)

### 3.2 What MetricsMiddleware Does NOT Record

| Missing Data | Where It Exists | Impact |
|---|---|---|
| Queue wait time | `proxy.py:229` (`queued.age_seconds`) | Cannot distinguish queue latency from inference latency |
| Request body size | `proxy.py:118` (`len(body)`) | No visibility into payload sizes that affect latency |
| Response body size | (not tracked anywhere) | No bandwidth/throughput metrics |
| Error classification | (status_code only) | 502 (Ollama down) vs 503 (queue full) vs 504 (timeout) not distinguished |
| Request ID | (not generated) | Cannot correlate metrics with audit events or traces |
| Circuit breaker state at request time | `proxy.py:232` | Cannot see how many requests were rejected by CB |
| Streaming vs non-streaming | `proxy.py:147` (`is_streaming`) | Cannot differentiate latency profiles |
| Client IP / User-Agent | `request.headers` | No per-client metrics |

### 3.3 Middleware Body Parsing Risk

The middleware reads `request.body()` to extract the model name. FastAPI's `Request.body()` caches internally after the first read, so downstream handlers can still access it. However:

1. This adds latency to every POST request (JSON parse overhead)
2. For non-JSON bodies or non-`/api/*` routes, the parse fails silently (correct behavior)
3. The middleware cannot extract model names from the modified payload (after `use_mmap` injection) -- it reads the original body

---

## 4. The No-Op Stub Gap Analysis

### 4.1 How No-Op Stubs Work

**Prometheus** (`metrics.py`):
When `prometheus-client` is not installed, `Counter`, `Histogram`, and `Gauge` classes are replaced with no-op subclasses. All 17 metric objects become silent. The `generate_latest()` function returns `b""`.

**OpenTelemetry** (`telemetry.py`):
All span-recording functions check `is_enabled()` and return early (or yield None for context managers) when OTel is unavailable or disabled. This is a clean guard-clause pattern.

### 4.2 Gap Between No-Op and Full Observability

| Capability Level | Prometheus | OpenTelemetry | Audit |
|---|---|---|---|
| **Level 0: No deps installed** | All 17 metrics are silent no-ops. `/broker/metrics` returns 501. | All 5 spans are no-ops. No trace IDs generated. | Always active (stdlib logging). |
| **Level 1: Deps installed, default config** | 7 of 17 metrics emit data. `/broker/metrics` returns partial data. | 0 of 5 spans emit (disabled by default). | Always active, tier 2. |
| **Level 2: Full wiring (requires code changes)** | All 17 metrics would emit. Complete Prometheus dashboard possible. | All 5 spans would emit. Full distributed tracing. | Already complete. |
| **Level 3: Ideal (proposed new metrics)** | 17 existing + 15 proposed = 32 metrics. Alerting, auto-scaling. | 5 existing + 4 proposed = 9 span types. Full pipeline traces. | Already complete. |

**Current state: Level 1.** The system is stuck between "metrics infrastructure exists" and "metrics provide actionable data."

---

## 5. Missing Metrics -- What Should Be Instrumented

### 5.1 Critical Missing Metrics (scheduler.py)

The scheduler is the brain of BASTION. It performs model swaps, enforces cooldowns, and dispatches requests -- yet it emits **zero Prometheus metrics**.

**Where to add calls in `scheduler.py`:**

| Event | Metric Helper | Location in Code |
|---|---|---|
| Model swap occurs | `record_model_swap(from_model, to_model)` | `_handle_swap_dispatch()` after `self._total_swaps += 1` |
| Swap duration measured | `record_model_swap_duration(model, duration)` | Wrap `_handle_swap_dispatch()` in timing |
| Cooldown enforced | `record_cooldown_wait()` | `_handle_swap_dispatch()` at the `asyncio.sleep(min(remaining, 0.5))` branch |
| Queue depth changes | `update_queue_depth(model, depth)` | After every `enqueue()` and `dequeue_for_model()` in `queue.py` |
| GPU temperature polled | `update_gpu_temperature(celsius)` | `_process_tick()` after `check_gpu_safe()` |
| VRAM usage polled | `update_vram_usage(bytes_used)` | Periodic update in scheduler loop or health check |

### 5.2 Critical Missing Metrics (proxy.py)

The proxy handles every Ollama request but only emits metrics through the middleware wrapper. Internal proxy metrics are not captured.

| Proposed Metric | Type | Labels | Rationale |
|---|---|---|---|
| `bastion_proxy_queue_wait_seconds` | Histogram | model, tier | Already computed at `proxy.py:229` as `queued.age_seconds` but never emitted to Prometheus |
| `bastion_proxy_errors_total` | Counter | error_type (queue_full, timeout, circuit_open, ollama_error) | Distinguish failure modes; currently all lumped into status codes |
| `bastion_proxy_streaming_requests_total` | Counter | model | Track streaming vs non-streaming ratio |
| `bastion_proxy_request_body_bytes` | Histogram | model | Track payload sizes for capacity planning |

### 5.3 Important Missing Metrics (vram.py, circuitbreaker.py, watchdog.py)

| Proposed Metric | Type | Labels | Source |
|---|---|---|---|
| `bastion_vram_allocated_bytes` | Gauge | (none) | `VRAMManager._allocated` |
| `bastion_vram_reserved_bytes` | Gauge | (none) | `VRAMManager._reserved` |
| `bastion_vram_available_bytes` | Gauge | (none) | `VRAMManager.available_vram` |
| `bastion_vram_per_model_bytes` | Gauge | model | `VRAMManager._model_allocations` |
| `bastion_circuit_breaker_state` | Gauge | (none) | 0=closed, 1=open, 2=half_open |
| `bastion_circuit_breaker_failures_total` | Counter | (none) | `CircuitBreaker._consecutive_failures` |
| `bastion_watchdog_ollama_latency_seconds` | Histogram | (none) | `ProcessMonitor._status.ollama_latency_ms` |
| `bastion_watchdog_gpu_latency_seconds` | Histogram | (none) | `ProcessMonitor._status.gpu_query_latency_ms` |
| `bastion_scheduler_stall_seconds` | Histogram | reason | Duration of scheduler stalls by stall_reason |
| `bastion_swap_rate_level` | Gauge | (none) | 0=normal, 1=warn, 2=critical |
| `bastion_resident_models_count` | Gauge | (none) | Number of models loaded in VRAM |

---

## 6. Internal State That Could Become Metrics

### 6.1 Scheduler Internal State

The scheduler tracks rich operational data that would be invaluable as Prometheus metrics:

**Swap Rate Tracking** (`scheduler.py`):
- `self._swap_timestamps: deque[float]` -- rolling window of swap times
- `self._swap_rate_level: str` -- "normal", "warn", "critical"
- Current swap count in window: `len(self._swap_timestamps)`
- Rate level transitions are already audited (`audit.emit("swap_rate", ...)`) but not metricated
- This is BASTION's primary crash prevention mechanism -- it MUST be visible in Grafana

**Stall Diagnostics** (`scheduler.py`):
- `self._last_stall_reason: str` -- "at_max_concurrent", "swap_cooldown", "all_models_inflight"
- `self._last_stall_time: float` -- when the stall began
- Stall duration = `time.time() - self._last_stall_time`
- Stall frequency and duration by reason type would reveal scheduling bottlenecks

**Dispatch Counters** (`scheduler.py`):
- `self._total_dispatched: int` -- never exposed as a metric
- This should be `bastion_requests_dispatched_total` -- the actual throughput counter

### 6.2 Queue Internal State

**Queue Depth Over Time** (`queue.py`):
- `self._model_queues: dict[str, list[QueuedRequest]]`
- `self._total_size: int`
- `bastion_queue_depth` gauge with `model` label should be updated on every `enqueue()` and `dequeue_for_model()`
- Currently, queue depth is only queryable via `/broker/status` (point-in-time HTTP polls)

**Request Age Distribution**:
- `QueuedRequest.age_seconds` is a computed property that shows how long each request has waited
- A histogram of request ages at dequeue time would reveal starvation patterns
- Priority aging rate (`effective = base + age * 2.0`) means requests that wait longest get highest priority -- but we cannot verify this without metrics

**Queue Sweep Events** (`server.py`):
- Stale request sweeps happen every 60 seconds
- Swept count and ages are logged but not metricated
- Proposed: `bastion_queue_swept_total` counter

### 6.3 VRAM State Over Time

**VRAMManager Ledger** (`vram.py`):
- `self._allocated: int` -- confirmed (model loaded)
- `self._reserved: int` -- pending (loading in progress)
- `self._model_allocations: dict` -- per-model committed bytes
- The `status()` method returns all of this, but no metric emits it
- VRAM utilization over time is critical for capacity planning
- Reconciliation events (stale allocations freed) should be counted

**VRAMTracker Alerts** (`vram.py`):
- VRAM threshold alerts (>85% warning, >95% critical) are emitted as audit events
- These should also increment a `bastion_vram_alert_total` counter with `severity` label

### 6.4 Circuit Breaker State

**State Transitions** (`circuitbreaker.py`):
- Transitions between CLOSED, OPEN, HALF_OPEN are logged but not metricated
- `bastion_circuit_breaker_state` gauge (0/1/2) would enable Grafana state timeline
- `bastion_circuit_breaker_transitions_total` counter with `from` and `to` labels

### 6.5 Watchdog Health

**Ollama Latency** (`watchdog.py`):
- `self._status.ollama_latency_ms` is tracked but only queryable via `/broker/watchdog`
- Should be emitted as `bastion_watchdog_ollama_latency_seconds` histogram

**GPU Query Latency** (`watchdog.py`):
- nvidia-smi response time is a leading indicator of GPU lockups
- Should be emitted as `bastion_watchdog_gpu_latency_seconds` histogram

---

## 7. Missing OpenTelemetry Spans

### 7.1 The Proxy Pipeline Gap

The primary request flow through BASTION has **zero tracing**:

```
Client -> FastAPI -> proxy.py -> queue.py -> scheduler.py -> Ollama -> Client
               ^         ^          ^             ^             ^
            (no span) (no span) (no span)    (no span)     (no span)
```

Only the A2A path has tracing:
```
A2A Client -> a2a.py (PRODUCER) -> queue.py -> scheduler.py -> a2a.py (CONSUMER) -> Ollama
                  ^                                                 ^
          (a2a.task.submit)                              (a2a.task.process)
```

### 7.2 Proposed Spans for Complete Pipeline Coverage

| Span Name | Kind | Location | Attributes |
|---|---|---|---|
| `bastion.proxy.request` | SERVER | `proxy._handle_scheduled()` | model, endpoint, tier, streaming |
| `bastion.scheduler.queue_wait` | INTERNAL | Already defined in telemetry.py but unused. Should wrap `grant_event.wait()` in `proxy._handle_scheduled()` | request_id, model |
| `bastion.scheduler.model_swap` | INTERNAL | Already defined in telemetry.py but unused. Should wrap swap logic in `scheduler._handle_swap_dispatch()` | from_model, to_model |
| `bastion.ollama.inference` | CLIENT | Already defined in telemetry.py but unused. Should wrap the httpx call in `proxy._stream_response()` / `_forward_response()` | model, operation, tokens |

### 7.3 Token Count Capture

Ollama's non-streaming responses include `prompt_eval_count` and `eval_count`. The streaming final chunk includes `eval_count` and `total_duration`. These could be captured:

- In `proxy._forward_response()`: parse response JSON and call `set_inference_tokens()`
- In A2A streaming: already partially done (TTFT captured), but token counts are not set on the OTel span

---

## 8. Metrics-Driven Capabilities

### 8.1 Auto-Scaling Signals

If BASTION were deployed behind a load balancer or in a multi-node setup, these metrics would enable auto-scaling:

| Signal | Metric | Threshold |
|---|---|---|
| Queue saturation | `bastion_queue_depth` | > 50% of max_queue_size (256) |
| Request latency | `bastion_request_duration_seconds` p99 | > 30s |
| VRAM pressure | `bastion_vram_available_bytes` | < 2 GB |
| GPU thermal | `bastion_gpu_temperature_celsius` | > 78C (below 82C limit) |
| Swap rate stress | `bastion_swap_rate_level` | >= 1 (warn) |

### 8.2 Alerting Rules (Prometheus Alertmanager)

These PromQL expressions would be possible with full metric emission:

```yaml
# Critical: GPU approaching crash zone
- alert: BastionSwapRateCritical
  expr: bastion_swap_rate_level == 2
  for: 30s
  annotations:
    summary: "Model swap rate at critical level -- GPU crash risk"

# Warning: Queue building up
- alert: BastionQueueBacklog
  expr: bastion_queue_depth > 100
  for: 2m
  annotations:
    summary: "Queue depth > 100 for 2 minutes"

# Critical: Circuit breaker open
- alert: BastionCircuitOpen
  expr: bastion_circuit_breaker_state == 1
  for: 10s
  annotations:
    summary: "Ollama backend circuit breaker is OPEN"

# Warning: VRAM pressure
- alert: BastionVRAMPressure
  expr: (bastion_vram_allocated_bytes + bastion_vram_reserved_bytes) / bastion_vram_used_bytes > 0.9
  for: 1m
  annotations:
    summary: "VRAM utilization > 90%"

# Warning: High TTFT
- alert: BastionSlowTTFT
  expr: histogram_quantile(0.95, bastion_llm_time_to_first_token_seconds_bucket) > 5
  for: 5m
  annotations:
    summary: "95th percentile time-to-first-token > 5s"
```

**Current state**: None of these alerts are possible because the metrics are not emitted.

### 8.3 Capacity Planning Queries

With full metrics, capacity planning would be data-driven:

```promql
# Average requests per model per hour
rate(bastion_requests_total[1h]) by (model)

# Model swap frequency (crash prevention metric)
rate(bastion_model_swap_total[10m])

# Queue wait time by tier (SLA compliance)
histogram_quantile(0.99, bastion_queue_wait_seconds_bucket{tier="interactive"})

# VRAM headroom trend
bastion_vram_available_bytes / 1073741824  # Available GB

# Inference throughput
rate(bastion_requests_total{endpoint="/api/generate"}[5m])
```

### 8.4 Adaptive Scheduling (Metrics Feedback Loop)

Metrics could feed back into the scheduler to enable adaptive behavior:

1. **Dynamic cooldown**: If `bastion_model_swap_duration_seconds` p95 increases, automatically increase cooldown_seconds
2. **Priority re-balancing**: If `bastion_queue_wait_seconds{tier="background"}` p99 exceeds a threshold, increase aging_rate for background tier
3. **Concurrent dispatch tuning**: If `bastion_gpu_temperature_celsius` approaches max_temperature_c, reduce max_concurrent_dispatches
4. **Swap rate throttling**: Already implemented in `scheduler._get_swap_cooldown()`, but the trigger thresholds are static -- metrics could make them adaptive

---

## 9. Grafana Dashboard Proposals

### 9.1 Dashboard: BASTION Overview

**Row 1: Request Flow**
- Panel: Requests/sec by endpoint (`rate(bastion_requests_total[5m])`)
- Panel: Request duration heatmap (`bastion_request_duration_seconds_bucket`)
- Panel: Queue depth gauge (per model)
- Panel: Error rate by type

**Row 2: GPU Health**
- Panel: VRAM usage (allocated + reserved + available stacked area)
- Panel: GPU temperature timeline
- Panel: Resident models count
- Panel: Swap rate level (normal/warn/critical state timeline)

**Row 3: Scheduler Performance**
- Panel: Model swaps/min (`rate(bastion_model_swap_total[1m])`)
- Panel: Swap duration histogram
- Panel: Cooldown waits/min
- Panel: Stall time by reason

**Row 4: A2A Task Lifecycle**
- Panel: A2A tasks by skill and state
- Panel: A2A task duration by skill
- Panel: A2A queue wait time
- Panel: Time to first token by model

### 9.2 Dashboard: Crash Prevention

A specialized dashboard for the RTX 5090 crash prevention system:

- Panel: Swap rate (swaps in rolling 60s window) with warn/critical threshold lines
- Panel: Swap cooldown level state timeline (green=normal, yellow=warn, red=critical)
- Panel: VRAM headroom (available_bytes / total_bytes)
- Panel: GPU temperature with max_temperature_c threshold line
- Panel: Model swap duration trend (early warning for degraded PCIe performance)
- Panel: Concurrent dispatches (current vs max)

### 9.3 Dashboard: Queue Analytics

- Panel: Queue depth by model (stacked area)
- Panel: Queue wait time p50/p95/p99 by tier
- Panel: Request age at dequeue (starvation detection)
- Panel: Queue sweep events (stale request evictions)
- Panel: Effective priority distribution (aging working correctly?)

### 9.4 Currently Buildable vs. Proposed

| Dashboard | Currently Buildable | After Wiring Existing Metrics | After Adding Proposed Metrics |
|---|---|---|---|
| Request overview | Partial (counts, duration) | Same | Full (with error classification) |
| GPU health | No | Partial (VRAM, temperature) | Full (with swap rate, stalls) |
| Scheduler performance | No | Partial (swaps, cooldowns) | Full (with adaptive signals) |
| A2A lifecycle | Yes (7 metrics active) | Same | Full (with queue depth) |
| Crash prevention | No | Partial | Full |
| Queue analytics | No | Partial (depth, wait time) | Full (with age distribution) |

---

## 10. Middleware Gap: Request-Level Data Loss

### 10.1 Data Available but Not Recorded by Middleware

The middleware sees every request but discards most of the observable data:

| Data Point | Available At | Lost After |
|---|---|---|
| Queue wait time | `proxy.py:229` | Only in `/broker/recent` ring buffer, not in Prometheus |
| Model name for non-POST | Always None | GET requests to `/api/tags` etc. not labeled |
| Streaming flag | `proxy.py:147` | Not recorded; cannot distinguish streaming latency |
| Body size | `proxy.py:118` | Validated but not metricated |
| User-Agent (client type) | `request.headers` | Only used for priority detection, not metricated |
| X-Broker-Intent | `request.headers` | Used for priority but not tracked as label |
| Circuit breaker state | `proxy.circuit_breaker.state` | Not recorded; cannot correlate rejections with CB state |
| Request ID | Not generated | No correlation between metrics, traces, and audit events |

### 10.2 The Request ID Gap

Without a request ID:
- Prometheus metrics cannot be correlated with audit events
- OTel traces cannot be linked to specific metric data points
- `/broker/recent` entries cannot be cross-referenced with anything

A `RequestIDMiddleware` would:
1. Generate or accept `X-Request-ID`
2. Store it in `request.state.request_id`
3. Pass it through to audit events, metrics labels (carefully -- request IDs are unbounded cardinality), and OTel span attributes

**Cardinality note**: Request IDs should NOT be Prometheus labels (unbounded cardinality). They should be OTel span attributes and audit event fields only.

---

## 11. Specific Emission Site Recommendations

### 11.1 scheduler.py: Where to Add Metric Calls

```
Location: _handle_swap_dispatch(), after self._total_swaps += 1
Add:       record_model_swap(from_model, candidate.model)
           record_model_swap_duration(candidate.model, time.time() - swap_start)

Location: _handle_swap_dispatch(), at the cooldown sleep branch
Add:       record_cooldown_wait()

Location: _process_tick(), after check_gpu_safe() call
Add:       gpu = await query_gpu_status()
           if gpu.temperature_c:
               update_gpu_temperature(gpu.temperature_c)
           if gpu.vram_used_mb:
               update_vram_usage(gpu.vram_used_mb * 1024 * 1024)
```

### 11.2 queue.py: Where to Add Metric Calls

```
Location: enqueue(), after self._total_size += 1
Add:       update_queue_depth(request.model, len(self._model_queues[request.model]))

Location: dequeue_for_model(), after self._total_size -= 1
Add:       update_queue_depth(model, len(queue))  # len after pop
```

### 11.3 proxy.py: Where to Add Metric Calls

```
Location: _handle_scheduled(), after queue_wait_seconds = queued.age_seconds
Add:       record_queue_wait(model=model, tier=tier.value, wait_seconds=queue_wait_seconds)
```

### 11.4 server.py: Where to Add Periodic Gauge Updates

A periodic background task (similar to `_queue_sweep_loop`) could update gauge metrics:

```
Every 5 seconds:
    - update_vram_usage() from VRAMTracker
    - update_gpu_temperature() from health.query_gpu_status()
    - update_queue_depth() for all models from AffinityQueue
    - Update VRAMManager gauges (allocated, reserved, available)
    - Update circuit breaker state gauge
    - Update resident models count gauge
```

---

## 12. Label Cardinality Management

### 12.1 Current Cardinality Analysis

**Tier 1 (always safe):**
- `skill`: 4 values (infer, status, batch_infer, preload)
- `state`: 5 values (submitted, working, completed, failed, canceled)
- `error_code`: ~10 bounded values
- `method`: ~5 bounded values
- `tier`: 4 values (interactive, agent, pipeline, background)

**Potentially problematic:**
- `model`: 5-50 values in practice, bounded by config
- `endpoint`: ~12 values, bounded by route definitions
- `from_model` / `to_model`: cross-product could be O(model^2) but bounded by config

**Never use (correctly documented):**
- `task_id`, `request_id`, `context_id` -- unbounded cardinality

### 12.2 Cardinality Risks in Proposed Metrics

| Proposed Label | Risk | Mitigation |
|---|---|---|
| `error_type` | Low (bounded enum) | Define explicit set: queue_full, timeout, circuit_open, ollama_error |
| `stall_reason` | Low (bounded enum) | 5 defined values in scheduler |
| `severity` | Low (bounded: warning, critical) | Already bounded |

---

## 13. Comparison: Audit vs. Metrics vs. Telemetry Coverage

| Event | Audit Log | Prometheus Metric | OTel Span |
|---|---|---|---|
| Request complete | `audit.emit(EVENT_REQUEST_COMPLETE, ...)` in proxy.py | `bastion_requests_total` (middleware) | None |
| Model swap | `audit.emit(EVENT_SWAP, ...)` in scheduler.py | None (helper exists, never called) | None (context manager exists, never used) |
| Swap rate change | `audit.emit("swap_rate", ...)` in scheduler.py | None | None |
| Cooldown enforced | None | None (helper exists, never called) | None |
| GPU unsafe | None (logged) | None | None |
| Scheduler stall | `audit.emit("scheduler_stall", ...)` in scheduler.py | None | None |
| VRAM alert | `audit.emit(EVENT_VRAM_ALERT, ...)` in vram.py | None | None |
| VRAM reconciliation | `audit.emit("vram_reconciliation", ...)` in vram.py | None | None |
| Queue sweep | `audit.emit("queue_sweep", ...)` in server.py | None | None |
| A2A task submit | `emit_tiered(...)` in a2a.py | `bastion_a2a_tasks_total` | `a2a.task.submit` (PRODUCER) |
| A2A task complete | `emit_tiered(...)` in a2a.py | `bastion_a2a_task_duration_seconds` | `a2a.task.process` (CONSUMER) |
| A2A error | None | `bastion_a2a_errors_total` | Error on span |
| Circuit breaker trip | Logged | None | None |
| Watchdog health check | None | None | None |

**Key insight**: Audit logging has the broadest coverage. Many events that are audited are not metricated or traced. The audit log is the most complete observability source, but it is write-only (no query API) and not suitable for real-time dashboards.

---

## 14. Summary of Findings

### 14.1 Severity Classification

**Critical (blocks production observability):**
1. **9 of 17 Prometheus metrics never emitted** -- the infrastructure exists, the wiring does not
2. **Scheduler emits zero metrics** despite being the most operationally critical component
3. **Proxy pipeline has zero OTel spans** -- 3 span types are defined in telemetry.py but never used
4. **TTFT only captured for A2A streaming**, not for the primary proxy streaming path

**High (significant observability gaps):**
5. **No circuit breaker metrics** -- state transitions invisible to monitoring
6. **No VRAM ledger metrics** -- `VRAMManager` has 5 gauge-worthy fields, all unmetricated
7. **No watchdog latency metrics** -- Ollama and GPU health check latencies unrecorded
8. **No queue wait time in Prometheus** -- only in `/broker/recent` ring buffer

**Medium (missing data for advanced use cases):**
9. **No request ID middleware** -- cannot correlate across audit, metrics, traces
10. **No error classification in metrics** -- all failures lumped into status_code labels
11. **No streaming vs non-streaming distinction** in metrics
12. **No per-client metrics** (IP or User-Agent based)

**Low (nice to have):**
13. **No JSON format for `/broker/metrics`** -- clients without Prometheus cannot consume metrics
14. **No metrics for intent tracking** or session profile usage
15. **No lease lifecycle metrics** (created, expired, released)

### 14.2 Effort Estimates

| Action | Effort | Impact |
|---|---|---|
| Wire 9 existing metric helpers to call sites | Low (add ~15 lines across 3 files) | Critical -- enables 9 dormant metrics |
| Wire 3 existing OTel spans to scheduler/proxy | Low (add ~10 lines) | Critical -- enables pipeline tracing |
| Add periodic gauge update task | Low (new background task, ~30 lines) | High -- VRAM, temperature, queue depth gauges |
| Add RequestID middleware | Low (~20 lines) | High -- cross-observability correlation |
| Define 11 new proposed metrics | Medium (~50 lines in metrics.py) | High -- circuit breaker, watchdog, swap rate |
| Build Grafana dashboards | Medium (4 dashboards, JSON) | High -- visual operational awareness |
| Implement adaptive scheduling feedback | High (scheduler refactoring) | Medium -- requires careful threshold tuning |

### 14.3 Priority Order for Implementation

1. **Wire existing helpers** (scheduler.py, proxy.py, queue.py) -- 15 lines of code, 9 metrics immediately active
2. **Wire existing OTel spans** (scheduler.py, proxy.py) -- 10 lines, full pipeline tracing
3. **Add periodic gauge task** (server.py) -- 30 lines, VRAM/GPU/queue gauges live
4. **Add RequestID middleware** -- 20 lines, cross-observability correlation
5. **Add new circuit breaker + watchdog metrics** -- 50 lines in metrics.py, 15 lines at emission sites
6. **Build Grafana dashboards** -- configuration only, no code changes
7. **Add proxy queue wait TTFT for non-A2A** -- moderate changes in proxy._stream_response()
8. **Implement alerting rules** -- Prometheus configuration only
9. **Adaptive scheduling** -- future work, requires production data to tune thresholds

---

## Key Files Referenced

| File | Absolute Path | Role |
|---|---|---|
| metrics.py | `/home/user/BASTION/src/bastion/metrics.py` | 17 metric definitions, 16 helper functions, no-op stubs |
| telemetry.py | `/home/user/BASTION/src/bastion/telemetry.py` | 5 OTel span types, GenAI conventions, no-op stubs |
| middleware.py | `/home/user/BASTION/src/bastion/middleware.py` | MetricsMiddleware -- only active metric emission site (proxy) |
| models.py | `/home/user/BASTION/src/bastion/models.py` | TelemetryConfig, BrokerConfig with metrics-related models |
| scheduler.py | `/home/user/BASTION/src/bastion/scheduler.py` | Core scheduling loop -- ZERO metric/trace emissions |
| server.py | `/home/user/BASTION/src/bastion/server.py` | App factory, `/broker/metrics` endpoint, ring buffer |
| a2a.py | `/home/user/BASTION/src/bastion/a2a.py` | Only module calling A2A metric + telemetry helpers |
| proxy.py | `/home/user/BASTION/src/bastion/proxy.py` | Proxy pipeline -- no direct metric calls |
| queue.py | `/home/user/BASTION/src/bastion/queue.py` | AffinityQueue -- no metric calls |
| vram.py | `/home/user/BASTION/src/bastion/vram.py` | VRAMManager/Tracker -- 5 gauge-worthy fields unmetricated |
| circuitbreaker.py | `/home/user/BASTION/src/bastion/circuitbreaker.py` | Circuit breaker -- state transitions unmetricated |
| watchdog.py | `/home/user/BASTION/src/bastion/watchdog.py` | ProcessMonitor -- latencies unmetricated |

---

**End of Report**

Generated by Metrics & Telemetry Analyst
Session: S0 (Audit Phase)
Next: Synthesis lead to integrate findings across all analyst reports
