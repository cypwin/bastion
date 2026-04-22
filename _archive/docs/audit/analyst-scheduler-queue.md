# Scheduler & Queue Analysis -- BASTION Audit

**Generated**: 2026-03-13
**Analyst**: Scheduler & Queue Domain Analyst
**Scope**: Scheduling algorithms, queue mechanics, priority system, affinity patterns, cooldown dynamics, and expansion opportunities
**Source Files**:
- `/home/cyprian/BASTION/src/bastion/scheduler.py` (710 lines)
- `/home/cyprian/BASTION/src/bastion/queue.py` (200 lines)
- `/home/cyprian/BASTION/src/bastion/models.py` (528 lines)
- `/home/cyprian/BASTION/src/bastion/server.py` (1561 lines)
- `/home/cyprian/BASTION/src/bastion/vram.py` (616 lines)
- `/home/cyprian/BASTION/src/bastion/proxy.py` (442 lines)
- `/home/cyprian/BASTION/src/bastion/metrics.py` (522 lines)
- `/home/cyprian/BASTION/docs/audit/ref-broker-config.yaml` (222 lines)

**Building On Scout Findings**:
- Data Model Scout: 17 scheduler/queue internal fields never surfaced via API
- Code Cartographer: tight coupling scheduler.py <-> vram.py <-> queue.py (expected, core loop)
- API Surface Scout: 5 missing endpoints where data exists but no route serves it

---

## Executive Summary

BASTION's scheduler implements a **two-phase affinity-aware dispatch algorithm** with dynamic cooldown escalation, GPU health gating, and concurrent multi-model inference. The system is well-engineered for its primary use case (single-GPU RTX 5090 crash prevention) but has significant untapped potential in seven areas:

1. **Scheduling algorithm**: Effective but has no concept of estimated completion time, meaning shortest-job-first and deadline scheduling are impossible.
2. **Queue metrics**: 8 computed internal metrics are never exposed -- including request age, effective priority scores, throughput rates, and stale sweep counts.
3. **Priority system**: 4 tiers exist (interactive/agent/pipeline/background) with linear aging, but there is no per-client fairness, no priority ceiling, and no dynamic re-prioritization.
4. **Affinity patterns**: Model affinity is tracked as a single `_current_model` pointer; historical swap patterns are recorded but never used for predictive pre-loading.
5. **Cooldown/transition data**: A sophisticated three-level dynamic cooldown exists (normal/warn/critical) but its state is invisible to operators and clients.
6. **Preemption/fairness**: Not supported -- all scheduling is cooperative with first-come ordering within priority bands.
7. **Config utilization**: 11 of 21 `SchedulerConfig` fields are undocumented in the example config, and several have interactions that are non-obvious without reading source code.

**Key Insight**: BASTION's scheduling is hardware-constrained, not algorithm-constrained. The RTX 5090's vulnerability to rapid model swap cycles (crash at ~8-9 swaps/minute) means the scheduler must be conservative. This makes aggressive optimizations like preemption or speculative loading risky unless gated behind the existing swap rate limiter.

---

## 1. Scheduling Algorithm Analysis

### Current Algorithm: Two-Phase Affinity Dispatch

The scheduler runs as a single `asyncio.Task` (`_loop()`) that wakes on either new request arrival (`_wake_event`) or a configurable timer (`loop_interval_seconds`, default 100ms). Each tick executes `_process_tick()`:

**Phase 1 -- Co-Resident Dispatch (Non-Blocking)**
```
For each co-resident model with queued work:
  - Skip if model has in-flight request (same-model serialization)
  - Skip if max_concurrent_dispatches reached
  - Dispatch with needs_swap=False (non-blocking path)
  - Prefer current_model first (affinity drain)
  - Re-evaluate after each dispatch (queue state changes)
```

**Phase 2 -- Non-Resident Dispatch (Blocking Swap)**
```
If slots remain and non-resident models need work:
  - pick_next() selects highest effective priority request
  - Check cooldown timer (dynamic: 2s / 5s / 10s based on swap rate)
  - If cooldown active: drain current model's queue instead
  - If cooldown expired: VRAM reservation -> eviction if needed -> swap -> dispatch
```

**Stall Detection (Phase 3)**
```
If no dispatch occurred but queue is non-empty:
  - Diagnose reason: at_max_concurrent, all_models_inflight,
    swap_cooldown, non_resident_models
  - Log on reason change (deduplicated)
  - Emit audit event
```

### Algorithm Strengths

1. **Crash prevention is first-class**: Dynamic cooldown escalation (normal -> warn -> critical) directly prevents the observed failure mode of 60 swaps in 7 minutes. The thresholds (4 warn, 6 critical within 60s window) leave a 33% safety margin below the observed crash rate of 8-9/min.

2. **Concurrent dispatch to co-resident models**: Phase 1 can dispatch to up to `max_concurrent_dispatches` (default 3) different co-resident models simultaneously, with configurable stagger delay (100ms) to avoid VRM power transients. This is hardware-aware scheduling.

3. **Affinity drain is correct**: By preferring the current model's queue before swapping, the algorithm minimizes total swap count from O(N*M) to O(M) where M is unique models in the queue.

4. **GPU health gating**: Every tick checks `check_gpu_safe()` (temperature + power thresholds) before any dispatch. When unsafe, the scheduler backs off for `gpu_unsafe_backoff_seconds` (default 5s).

5. **VRAM ledger atomicity**: The `VRAMManager.reserve/commit/release` pattern eliminates TOCTOU races between "is there enough VRAM?" and "start loading the model."

### Algorithm Weaknesses

1. **No estimated completion time tracking**: The scheduler has no visibility into how long an in-flight inference will take. This means it cannot do shortest-job-first scheduling, and it cannot estimate when a blocked same-model slot will free up.

2. **Linear scan in pick_next()**: `AffinityQueue.pick_next()` iterates over every request in every model queue to find the highest effective priority. With `max_queue_size=512`, this is O(N) per tick. For the current workload this is fine (sub-millisecond), but it would not scale to thousands of queued requests.

3. **No batching optimization**: When multiple requests target the same model and endpoint, they could theoretically be batched into a single Ollama call (Ollama supports `OLLAMA_NUM_PARALLEL`). Currently each request is dispatched individually.

4. **Cooldown during swap blocks entire scheduler**: When a cooldown timer is active and the current model's queue is empty, the scheduler sleeps for `min(remaining, 0.5)` -- during which no co-resident dispatches happen either. Phase 1's concurrent dispatch should be allowed to continue during Phase 2's cooldown.

5. **No predictive pre-loading**: The scheduler has access to `IntentDeclaration` data (model sequences declared by clients) but does not use this for proactive model pre-loading. Intent data flows from `server.py` -> `proxy._detect_priority()` for priority elevation only.

### Alternative Algorithms to Consider

**A. Weighted Fair Queuing (WFQ)**
- Assign each priority tier a weight: interactive=100, agent=50, pipeline=25, background=10
- Each tier gets a virtual time counter; dispatch from the tier with the lowest virtual time
- Prevents starvation WITHOUT aging (aging can be removed or reduced)
- Better suited for multi-tenant scenarios where "fairness" across tiers matters

**B. Earliest Deadline First (EDF)**
- Add `deadline` field to `QueuedRequest` (e.g., `submitted_at + queue_timeout_seconds`)
- Pick the request with the earliest deadline instead of highest priority
- Particularly useful for interactive requests that have hard latency SLOs
- Requires no new infrastructure; `queue_timeout_seconds` already defines implicit deadlines

**C. Multi-Level Feedback Queue (MLFQ)**
- Start all requests at interactive priority
- Demote long-waiting requests (those that have been skipped repeatedly) to lower queues
- Promote background requests that have been waiting exceptionally long
- More nuanced than linear aging; prevents pathological priority inversion

**D. Intent-Aware Pre-Loading**
- When an intent declares `model_sequence: [A, B, C]`, and model A is being served:
  - If VRAM permits, start pre-loading model B in the background
  - If VRAM does not permit, schedule B's load to start when A's queue drains
- Reduces effective swap latency from `load_time + cooldown` to just `cooldown`
- Risk: wasted VRAM if the intent is abandoned or models change

---

## 2. Queue Metrics: Computed but Never Exposed

The AffinityQueue and QueuedRequest compute rich operational data that is invisible to operators. Here is a complete inventory:

### 2.1 Per-Request Metrics (QueuedRequest)

| Metric | Source | Exposed? | Value |
|--------|--------|----------|-------|
| `age_seconds` | `QueuedRequest.age_seconds` property | Never | How long a request has been waiting. Critical for SLO monitoring. |
| `effective_priority()` | `QueuedRequest.effective_priority(aging_rate, bonus)` | Never | Actual scheduling score including aging. Reveals priority inversions. |
| `base_priority` | `QueuedRequest.base_priority` field | Never | Original tier-based priority before aging. |
| `tier` | `QueuedRequest.tier` (PriorityTier enum) | Never | Which tier (interactive/agent/pipeline/background) the request was classified as. |
| `client_info` | `QueuedRequest.client_info` field | Never | User-Agent string (truncated to 80 chars). Reveals which clients generate load. |
| `endpoint` | `QueuedRequest.endpoint` field | Never | Which API endpoint (/api/generate, /api/chat, /api/embed) the request targets. |

**Impact**: The `/broker/queue` endpoint returns only `{"models": {"qwen3:14b": 3}, "total": 3}` -- model names and counts. An operator cannot determine:
- Whether any requests are close to timing out
- Whether priority aging has caused inversions
- Which clients are generating the most load
- What the distribution of request ages looks like

### 2.2 Queue-Level Metrics (AffinityQueue)

| Metric | Source | Exposed? | Value |
|--------|--------|----------|-------|
| Models with requests | `get_models_with_requests()` | Never | List of models that have pending work (used internally by scheduler). |
| Per-model queue size | `model_queue_size(model)` | Partially (via `queue_depth_by_model()`) | Individual model depth is available but not the per-request detail. |
| Stale sweep count | `sweep_stale()` return value | Never (logged only) | Number of requests evicted per sweep cycle. Indicator of queue health. |
| Queue rejection count | `enqueue()` returns False | Never (503 returned) | Frequency of queue-full rejections. Important for capacity planning. |

### 2.3 Scheduler-Level Metrics (Scheduler)

| Metric | Source | Exposed? | Value |
|--------|--------|----------|-------|
| `total_dispatched` | `Scheduler._total_dispatched` | Never | Total inference requests dispatched since startup. `/broker/status.total_requests_served` exists but was historically disconnected (now wired to `_proxy._requests_served`). |
| `swap_rate_level` | `Scheduler._swap_rate_level` | Never | Current throttle state: "normal", "warn", or "critical". When "critical", cooldown is 10s instead of 2s. |
| `swap_timestamps` | `Scheduler._swap_timestamps` | Never | Rolling deque of swap times within the 60s window. Raw data for swap rate visualization. |
| `stall_reason` | `Scheduler.stall_reason` property | Partially (via `/broker/queue`) | Present in `/broker/queue` response but not in `/broker/status`. |
| `stall_time` | `Scheduler.stall_time` property | Partially (via `/broker/queue`) | Same as above. |
| Cooldown remaining | Computed in `/broker/queue` handler | Partially | Computed server-side and returned in `/broker/queue` but requires knowing which endpoint to check. |
| Effective cooldown value | `_get_swap_cooldown()` return | Never | The actual cooldown in effect (2s, 5s, or 10s). Different from `cooldown_remaining`. |

### 2.4 Server-Level Dispatch Metrics

| Metric | Source | Exposed? | Value |
|--------|--------|----------|-------|
| Inflight models | `server._inflight_models` | Partially (via `/broker/queue`) | Dict of `{model: count}` for in-flight inferences. Present in `/broker/queue` but not `/broker/status`. |
| Pending grants | `server._pending_grants` | Partially | Count only (via `/broker/queue`). Not the request IDs. |
| Pending completions | `server._pending_completions` | Never | No endpoint exposes which requests are awaiting Ollama completion. |
| Recent requests | `server._recent_requests` | Via `/broker/recent` | This IS exposed (deque of last 50 requests with latency data). |

### 2.5 Prometheus Metrics: Defined but Not Emitted by Scheduler

The `metrics.py` module defines scheduler-specific Prometheus metrics that are never populated:

| Prometheus Metric | Status | Why |
|-------------------|--------|-----|
| `bastion_model_swap_total` | Defined, helper exists (`record_model_swap()`) | Scheduler does not call `record_model_swap()` |
| `bastion_model_swap_duration_seconds` | Defined, helper exists (`record_model_swap_duration()`) | Scheduler does not time swap operations |
| `bastion_cooldown_waits_total` | Defined, helper exists (`record_cooldown_wait()`) | Scheduler does not call `record_cooldown_wait()` |
| `bastion_queue_depth` | Defined, helper exists (`update_queue_depth()`) | Not updated per-tick by scheduler |
| `bastion_queue_wait_seconds` | Defined, helper exists (`record_queue_wait()`) | Not called from scheduler dispatch path |

**Impact**: Even when `prometheus-client` is installed, the Prometheus `/broker/metrics` endpoint returns zeros for all scheduler-specific metrics. The middleware layer captures request-level metrics (latency, status codes), but the scheduler's internal state (swap counts, cooldown waits, queue depth changes) is invisible to Prometheus.

---

## 3. Priority Tier System Analysis

### Current Implementation

Four tiers with configurable base priority values:

| Tier | Default Base Priority | Typical Use Case |
|------|----------------------|------------------|
| `INTERACTIVE` | 100.0 | `ollama run`, Claude Code MCP (user-facing) |
| `AGENT` | 50.0 | AI agents, A2A clients |
| `PIPELINE` | 25.0 | Batch pipelines (extract, ingest) |
| `BACKGROUND` | 10.0 | Night cycle, consolidation, embedding |

**Priority Detection** (in `proxy._detect_priority()`):
1. Explicit `X-Broker-Priority` header (highest precedence)
2. `X-Broker-Intent` header -> lookup intent -> use profile's default_priority
3. User-Agent heuristic: "ollama" in UA -> INTERACTIVE
4. Default: AGENT

**Priority Aging Formula**:
```
effective_priority = base_priority + (age_seconds * aging_rate) + affinity_bonus
```

With defaults (`aging_rate=2.0`, `affinity_bonus=10.0`):
- A BACKGROUND request (base=10) equals AGENT (base=50) after 20 seconds
- A BACKGROUND request equals INTERACTIVE (base=100) after 45 seconds
- An AGENT request with affinity (50+10=60) equals INTERACTIVE (100) after 20 seconds

### Strengths of Current System

1. **Starvation prevention**: Linear aging guarantees that even BACKGROUND requests eventually get served. The 45-second crossover time is appropriate for an inference workload where individual requests can take 5-30 seconds.

2. **Simple, predictable**: Operators can reason about the system -- "if my background job has been waiting 60 seconds, it has effective priority 130, higher than any fresh interactive request."

3. **Affinity bonus is well-calibrated**: At 10.0 points, it provides a meaningful preference for draining the current model (equivalent to 5 seconds of aging) without overriding genuine priority differences (INTERACTIVE vs AGENT is a 50-point gap).

### Weaknesses and Extension Opportunities

**A. No Per-Client Fairness**

There is no tracking of how many requests each client has made. A single aggressive agent could flood the AGENT tier and consume all scheduling slots, starving other agents at the same tier.

Potential extension:
- Track `client_info` (already captured as User-Agent) per request
- Implement per-client token buckets within each tier
- Demote clients exceeding their fair share to the next lower tier

**B. No Priority Ceiling**

Aging has no cap. A BACKGROUND request waiting 300 seconds reaches effective priority 610 (10 + 300*2), which is 6x higher than INTERACTIVE. While this guarantees service, it can cause unexpected priority inversions for requests that have been waiting due to VRAM constraints (not scheduler delay).

Potential extension:
- Add `max_effective_priority` per tier (e.g., BACKGROUND caps at 150)
- Or switch to asymptotic aging: `base + max_boost * (1 - e^(-age/tau))`

**C. No Dynamic Re-Prioritization**

Once a request enters the queue, its tier is fixed. There is no mechanism for:
- Promoting a PIPELINE request to INTERACTIVE if a human is waiting for it
- Demoting an AGENT request if its parent task has been cancelled
- Adjusting priority based on model load patterns (e.g., boost requests for already-loaded models beyond the affinity bonus)

Potential extension:
- Add `PATCH /broker/queue/{request_id}/priority` endpoint
- Allow A2A tasks to adjust priority of pending requests via task metadata

**D. No Tier 5: PREEMPTIVE**

For truly latency-critical workloads (e.g., real-time conversation), a PREEMPTIVE tier could:
- Interrupt a running BACKGROUND inference (by closing the Ollama connection)
- Immediately load the required model
- Serve the preemptive request with minimal latency

This is high-risk (requires Ollama to handle connection interruptions gracefully) but would enable real-time SLOs.

**E. Missing Tier: DEADLINE**

A deadline-based tier would allow clients to specify "this request must complete by time T":
```json
{
  "model": "qwen3:14b",
  "prompt": "...",
  "x-broker-deadline": "2026-03-13T15:00:00Z"
}
```

Effective priority would increase exponentially as the deadline approaches, rather than linearly with age.

---

## 4. Model Affinity Patterns

### What Is Tracked

| State | Location | Description |
|-------|----------|-------------|
| `_current_model` | `Scheduler` | Last model dispatched to. Used as affinity target. |
| `_last_swap_time` | `Scheduler` | Timestamp of most recent model swap. |
| `_swap_timestamps` | `Scheduler` | Rolling deque of all swap times within the rate window. |
| `_total_swaps` | `Scheduler` | Cumulative swap count since startup. |
| Resident models | `VRAMTracker.residency_cache` | Set of models currently loaded in VRAM (TTL-cached, 1s). |
| Model allocations | `VRAMManager._model_allocations` | Per-model VRAM allocation in bytes. |

### What Is NOT Tracked (But Could Be)

1. **Swap transition matrix**: Which model was unloaded to load which other model. Currently only "from -> to" is logged via `audit.emit(EVENT_SWAP, ...)` but no in-memory structure accumulates these transitions. A transition matrix would reveal:
   - Pairs of models that frequently co-occur (good candidates for co-residency)
   - "Hot" models that get swapped in and out repeatedly (candidates for `always_allowed` or permanent residency)
   - Pathological patterns (A->B->A->B oscillation)

2. **Per-model request frequency**: How many requests each model receives over time. The queue tracks depth at a point in time, but there is no time-series or histogram of per-model demand. This would enable:
   - Weighted eviction (prefer evicting infrequently-requested models)
   - Capacity planning (which models need more VRAM or dedicated GPUs)

3. **Model load/unload duration**: How long it takes to load each model into VRAM. Varies significantly by model size (0.4 GB nomic-embed vs 19.5 GB qwen3:30b). Currently not timed at all. This would enable:
   - Swap cost estimation (schedule cheap swaps before expensive ones)
   - Proactive loading during idle periods

4. **Co-residency patterns**: Which sets of models tend to be resident simultaneously. With `ollama_max_loaded_models=4`, understanding which 4-model sets minimize total swap cost would be valuable.

5. **Intent fulfillment tracking**: When a client declares intent (model_sequence: [A, B, C]) and requests arrive, there is no tracking of how well the actual request pattern matches the declared intent. This would enable:
   - Intent accuracy scoring (how useful are client predictions?)
   - Adaptive scheduling (trust high-accuracy clients more)

### Predictive Pre-Loading Opportunity

The `IntentDeclaration` and `SessionProfile` infrastructure is fully implemented but under-utilized:

**Current flow**:
```
Client -> POST /broker/intent {profile: "council_pipeline"} -> intent registered
Client -> POST /api/generate {model: A, X-Broker-Intent: intent_id} -> priority elevated
Client -> POST /api/generate {model: B, X-Broker-Intent: intent_id} -> priority elevated
```

**Unrealized flow**:
```
Client -> POST /broker/intent {profile: "council_pipeline"}
Scheduler sees intent: model_sequence = [A, B, C, A]
  -> If A is resident, note B is next; when A's queue drains, pre-load B
  -> After B completes, pre-load C
  -> After C completes, keep A resident (it appears again in sequence)
```

The `session_profiles` in `ref-broker-config.yaml` already define these sequences:
- `council_pipeline`: qwen3:30b -> phi4:14b -> mistral-nemo:12b -> qwen3:30b
- `extraction_pipeline`: nuextract -> qwen3:8b
- `embedding_pipeline`: nomic-embed-text

Pre-loading the next model in the declared sequence while the current model is still processing would halve the latency of model transitions.

---

## 5. Cooldown and Transition Data Analysis

### Three-Level Dynamic Cooldown System

The cooldown system (`_get_swap_cooldown()`) is BASTION's primary crash prevention mechanism. It uses a rolling window of swap timestamps to dynamically adjust cooldown duration:

```
Swap Count in 60s Window    Level       Cooldown
---------------------------  ----------  --------
0-3                          normal      2.0s
4-5                          warn        5.0s
6+                           critical    10.0s
```

**Observed crash rate**: ~60 swaps in ~7 minutes = 8.6 swaps/minute.
**Critical threshold**: 6 swaps in 60s = 6 swaps/minute.
**Safety margin**: 30% below crash rate.

### Cooldown State Machine

```
       <4 swaps/min         >=4 swaps/min         >=6 swaps/min
normal ──────────────> normal ──────────────> warn ──────────────> critical
  ^                                             |                    |
  |           <4 swaps/min                      |  <4 swaps/min     |
  └─────────────────────────────────────────────┴────────────────────┘
```

Level transitions are:
- Logged at WARNING level
- Emitted as `swap_rate` audit events with full metadata
- Stored in `_swap_rate_level` (but never exposed to API)

### What Cooldown Data Could Inform

**A. Operator Alerting**

When the swap rate reaches "warn" level, operators should be notified. Currently, this information is:
- Written to Python logger (if someone is watching)
- Written to audit log (if someone parses JSONL)
- NOT available in any API response

A `/broker/scheduler/diagnostics` endpoint (as proposed by the API Surface Scout) should include:
```json
{
  "swap_rate": {
    "level": "warn",
    "swaps_in_window": 4,
    "window_seconds": 60,
    "effective_cooldown_seconds": 5.0,
    "cooldown_remaining_seconds": 2.3,
    "time_until_normal": 15.0
  }
}
```

**B. Adaptive Scheduling**

The cooldown system is currently reactive (adjusts cooldown AFTER swaps happen). It could be proactive:
- If swap rate is "warn", the affinity bonus could be doubled (20.0 instead of 10.0) to more aggressively drain the current model before swapping
- If swap rate is "critical", the scheduler could refuse non-resident model requests entirely (return 503) rather than just slowing down

**C. Cooldown History for Capacity Planning**

Tracking how often each level is reached would reveal:
- Peak swap rate hours (when diverse model usage is highest)
- Whether `ollama_max_loaded_models=4` is the right limit
- Whether additional VRAM or a second GPU would eliminate throttling

**D. Transition Cost Matrix**

Currently, all swaps are treated equally. But swap cost varies enormously:
- Loading nomic-embed-text (0.4 GB) takes ~1 second
- Loading qwen3:30b (19.5 GB) takes ~15 seconds
- Unloading before loading doubles the time

A per-model load time history would enable:
- Variable cooldowns (short cooldown after a small model load, long after a large one)
- Cost-aware scheduling (prefer cheap swaps when swap rate is elevated)

---

## 6. Preemption, Fairness Quotas, and Deadline Scheduling

### Current State: None of These Exist

BASTION's scheduling is entirely **cooperative** and **non-preemptive**:
- Once a request is dispatched to Ollama, it runs to completion
- There is no mechanism to cancel, interrupt, or preempt a running inference
- Queue ordering is determined solely by effective priority (base + aging + affinity)
- There are no per-client quotas or fairness constraints

### Preemption Analysis

**Feasibility**: Low. Ollama does not support inference cancellation via API. The only "preemption" would be:
1. Close the HTTP connection to Ollama (causes Ollama to abort the inference)
2. Immediately send a new request for the preempting model

**Risks**:
- Ollama may not free GPU memory immediately after connection close
- The aborted inference's KV cache may remain allocated
- Connection-close based cancellation is unreliable (Ollama might continue processing)
- Wasted compute (the aborted inference's tokens are lost)

**Recommendation**: Do not implement preemption at this time. Instead, focus on making interactive requests reliably fast through:
- Intent-based pre-loading (model is already resident when interactive request arrives)
- Higher affinity bonus for interactive tier
- Dedicated "always_allowed" slots for frequently-used interactive models

### Fairness Quotas

**Use Case**: In a multi-tenant scenario (multiple AI agents using BASTION), one aggressive agent should not monopolize scheduling.

**Possible Implementation**:
```python
class FairnessTracker:
    """Per-client token bucket for fair scheduling."""

    def __init__(self, tokens_per_minute: int = 30):
        self._buckets: dict[str, float] = defaultdict(lambda: tokens_per_minute)
        self._last_refill: dict[str, float] = {}
        self._tokens_per_minute = tokens_per_minute

    def consume(self, client_id: str) -> bool:
        """Try to consume a token. Returns False if rate-limited."""
        self._refill(client_id)
        if self._buckets[client_id] >= 1.0:
            self._buckets[client_id] -= 1.0
            return True
        return False

    def get_priority_penalty(self, client_id: str) -> float:
        """Return priority penalty based on token deficit."""
        tokens = self._buckets[client_id]
        if tokens > 0:
            return 0.0
        return abs(tokens) * 5.0  # 5 points penalty per token deficit
```

This would integrate with `effective_priority()`:
```
effective = base + (age * aging_rate) + affinity_bonus - fairness_penalty
```

**Integration point**: The `client_info` field on `QueuedRequest` already captures User-Agent strings. This could be parsed into a client identifier for fairness tracking.

### Deadline-Based Scheduling

**Use Case**: Interactive requests have implicit deadlines (users waiting). Pipeline requests may have explicit deadlines (batch must complete by midnight).

**Possible Implementation**:

Add to `QueuedRequest`:
```python
deadline: float | None = None  # Absolute time (epoch seconds) by which this must complete
```

Add to `effective_priority()`:
```python
def effective_priority(self, aging_rate: float, affinity_bonus: float = 0.0) -> float:
    base = self.base_priority + (self.age_seconds * aging_rate) + affinity_bonus
    if self.deadline is not None:
        time_remaining = self.deadline - time.time()
        if time_remaining <= 0:
            return float('inf')  # Expired deadline -- serve immediately
        urgency = 100.0 / max(time_remaining, 1.0)  # Urgency increases as deadline approaches
        base += urgency
    return base
```

**Integration**: Clients would set deadlines via `X-Broker-Deadline` header, similar to how `X-Broker-Priority` works today. The proxy's `_detect_priority()` would parse this header and propagate it to `QueuedRequest`.

---

## 7. Configuration: Documented, Undocumented, and Underutilized

### Full SchedulerConfig Inventory (21 Fields)

| Field | Default | In ref-broker-config.yaml | In Example Config | Used? | Notes |
|-------|---------|--------------------------|-------------------|-------|-------|
| `cooldown_seconds` | 2.0 | Yes | Yes | Yes | Base cooldown between model swaps |
| `model_affinity_bonus` | 10.0 | Yes | Yes | Yes | Priority boost for current model |
| `aging_rate` | 2.0 | Yes | Yes | Yes | Priority points per second of wait |
| `max_queue_size` | 512 | Yes | Yes | Yes | Hard queue limit (503 above) |
| `residency_cache_ttl_seconds` | 1.0 | Yes | No | Yes | How often to refresh Ollama /api/ps |
| `ollama_max_loaded_models` | 4 | Yes | No | Yes | Max co-resident models before proactive eviction |
| `loop_interval_seconds` | 0.1 | Yes | No | Yes | Scheduler wake-up interval |
| `error_backoff_seconds` | 1.0 | Yes | No | Yes | Sleep after scheduler exception |
| `gpu_unsafe_backoff_seconds` | 5.0 | Yes | No | Yes | Sleep when GPU health check fails |
| `shutdown_timeout_seconds` | 10.0 | Yes | No | Yes | Max wait for graceful stop |
| `swap_rate_window_seconds` | 60.0 | Yes | No | Yes | Rolling window for swap counting |
| `swap_rate_warn_threshold` | 4 | Yes | No | Yes | Swaps in window to trigger warn level |
| `swap_rate_critical_threshold` | 6 | Yes | No | Yes | Swaps in window to trigger critical level |
| `swap_rate_warn_cooldown_seconds` | 5.0 | Yes | No | Yes | Cooldown at warn level |
| `swap_rate_critical_cooldown_seconds` | 10.0 | Yes | No | Yes | Cooldown at critical level |
| `max_concurrent_dispatches` | 3 | Yes | No | Yes | Max parallel inferences to co-resident models |
| `concurrent_dispatch_delay_seconds` | 0.1 | Yes | No | Yes | Stagger between concurrent dispatches |
| `queue_ttl_seconds` | 600.0 | Yes | No | Yes | Max request age before sweep |

### Non-Obvious Interactions

1. **`cooldown_seconds` vs `swap_rate_*_cooldown_seconds`**: The base `cooldown_seconds` (2.0) is only used when swap rate is "normal". At "warn" or "critical" levels, `swap_rate_warn_cooldown_seconds` (5.0) or `swap_rate_critical_cooldown_seconds` (10.0) override it entirely. An operator setting `cooldown_seconds: 0.5` for faster swaps would be surprised when the system auto-escalates to 10s.

2. **`ollama_max_loaded_models` vs VRAM budget**: The max loaded models limit (4) triggers proactive eviction AFTER a swap succeeds, independent of VRAM budget checks. So even if 5 models fit in VRAM, the 5th would be evicted. This is intentional (reduces swap cycle rate) but not documented in the config comment.

3. **`max_concurrent_dispatches` vs `concurrent_dispatch_delay_seconds`**: These interact to control GPU power transients. With defaults (3 concurrent, 100ms delay), the worst-case ramp-up time is 200ms (3 dispatches with 100ms between each pair). Increasing `max_concurrent_dispatches` without adjusting the delay increases transient power stress.

4. **`queue_ttl_seconds` vs `proxy.queue_timeout_seconds`**: Both set timeouts for queued requests, but at different layers:
   - `queue_ttl_seconds` (600s): Background sweep every 60s, removes stale requests
   - `queue_timeout_seconds` (300s): Proxy-layer timeout, returns 504 to the client
   - Since `queue_timeout_seconds < queue_ttl_seconds`, the proxy timeout fires first for scheduled requests. The TTL sweep catches orphaned requests that somehow escaped proxy cleanup.

5. **`aging_rate` vs `model_affinity_bonus`**: With aging_rate=2.0 and affinity_bonus=10.0, the affinity bonus is equivalent to 5 seconds of aging. This means a request waiting 5+ seconds for a different model will overcome the affinity bonus. If the affinity bonus is intended to be stronger, it should be increased relative to aging_rate.

### Missing Configuration Options

1. **`max_effective_priority`**: No cap on priority aging. A request waiting 300s has effective priority 610.

2. **`eviction_strategy`**: Currently hardcoded to "prefer models with no queued requests, then smallest VRAM first." Could be configurable: `least_recently_used`, `smallest_first`, `least_queued_first`.

3. **`preload_on_intent`**: Boolean to enable/disable predictive pre-loading when intents are declared.

4. **`stall_timeout_seconds`**: No config for how long a stall is acceptable before taking action (e.g., force-evicting a model or escalating priority).

5. **`per_model_max_inflight`**: Currently hardcoded to 1 (same-model serialization for OLLAMA_NUM_PARALLEL=1). If Ollama is configured for higher parallelism, this should be configurable.

---

## 8. Architectural Observations

### Thread Safety

`AffinityQueue` uses `threading.Lock` (not `asyncio.Lock`) for thread safety. This is unusual in an otherwise fully async codebase. The reasoning is sound -- `threading.Lock` is non-reentrant and non-async, which prevents accidental `await` inside critical sections. However, it means queue operations are blocking (though very briefly, since they're pure memory operations).

The scheduler itself is single-threaded (one `asyncio.Task`), so there are no race conditions between Phase 1 and Phase 2 dispatch. However, `server.py` calls `queue.enqueue()` from FastAPI request handlers (potentially concurrent via asyncio), which is why the `threading.Lock` exists.

### Error Handling

The scheduler's error handling is robust:
- `_loop()` catches ALL exceptions, logs them, and backs off for `error_backoff_seconds`
- `_dispatch_for_model()` catches dispatch failures and calls `_dispatch_error_fn` for cleanup
- `stop()` uses `asyncio.wait_for()` with timeout and cancels the task if it hangs
- VRAM reservation failures trigger eviction-retry logic before giving up

One gap: if `_process_tick()` raises an exception inside the Phase 1 dispatch loop, the entire tick is aborted. Remaining co-resident models that could have been dispatched are skipped until the next tick.

### State Consistency

The scheduler maintains several pieces of state that must stay consistent:
- `_current_model` must match what is actually loaded in Ollama
- `_swap_timestamps` must reflect actual swaps (not attempted swaps)
- `_total_swaps` and `_total_dispatched` are monotonically increasing

The `_sync_current_model()` method on startup reconciles `_current_model` with Ollama's actual state. The `VRAMManager.reconcile()` method catches stale VRAM allocations when Ollama auto-unloads models. These are good safety nets.

One consistency risk: `_current_model` is updated before the dispatch succeeds (line 532 in scheduler.py: `self._current_model = candidate.model`). If the dispatch fails, `_current_model` points to a model that may not be loaded. The `_dispatch_error_fn` cleanup does not reset `_current_model`. In practice, this is mitigated by the next tick's residency cache refresh, but it could cause one tick of incorrect affinity scoring.

---

## 9. Recommendations

### Immediate Value (No Architecture Changes)

1. **Wire scheduler Prometheus metrics**: Add `record_model_swap()`, `record_cooldown_wait()`, and `update_queue_depth()` calls to the scheduler's dispatch and cooldown paths. These metrics are already defined in `metrics.py` with full helper functions; they just need to be called.

2. **Expose swap rate level in `/broker/status`**: Add a `scheduler_state` section:
   ```json
   {
     "scheduler_state": {
       "swap_rate_level": "normal",
       "swaps_in_window": 2,
       "effective_cooldown_seconds": 2.0,
       "cooldown_remaining_seconds": 0.0,
       "total_dispatched": 1234,
       "stall_reason": "",
       "stall_duration_seconds": 0.0
     }
   }
   ```

3. **Add queue detail endpoint**: `GET /broker/queue/details` returning per-request breakdown:
   ```json
   {
     "requests": [
       {
         "id": "abc123",
         "model": "qwen3:14b",
         "tier": "agent",
         "age_seconds": 12.5,
         "effective_priority": 75.0,
         "endpoint": "/api/generate"
       }
     ]
   }
   ```

4. **Fix Phase 2 cooldown blocking Phase 1**: When the swap cooldown is active, the scheduler should still attempt Phase 1 co-resident dispatch before sleeping. Currently, `_handle_swap_dispatch()` sleeps for `min(remaining, 0.5)` if the cooldown is active and current model has no work, blocking all dispatch.

### Medium-Term (Minor Architecture Changes)

5. **Implement swap transition tracking**: Add a `dict[tuple[str, str], int]` counting (from_model, to_model) transitions. Use this data to:
   - Detect oscillation patterns (A->B->A->B) and suggest co-residency
   - Feed the dashboard with swap flow visualization
   - Identify models that should be `always_allowed`

6. **Add priority ceiling to aging**: Cap effective priority at `2 * tier_max` (e.g., BACKGROUND caps at 200, AGENT at 300). Prevents pathological inversions after long waits due to VRAM constraints.

7. **Intent-aware pre-loading**: When an active intent's current model is being served and the next model in the sequence is not resident, check if VRAM permits pre-loading it. If so, issue a background preload. Guard behind a config flag (`preload_on_intent: true`).

8. **Per-client request tracking**: Add a lightweight counter per `client_info` hash to detect and optionally throttle aggressive clients within a priority tier.

### Long-Term (Significant Architecture Changes)

9. **Deadline-based scheduling**: Add `deadline` field to `QueuedRequest`, parse from `X-Broker-Deadline` header, and integrate with priority calculation. This enables SLO-aware scheduling for diverse workloads.

10. **Weighted Fair Queuing**: Replace linear aging with per-tier virtual time counters. Each tier gets guaranteed throughput proportional to its weight. This provides stronger fairness guarantees than aging alone.

11. **Model load time estimation**: Track per-model load/unload durations. Use these estimates to:
    - Make cost-aware eviction decisions (evict cheapest-to-reload model)
    - Provide ETA for queued requests (predicted queue wait + predicted load time + predicted inference time)
    - Optimize swap ordering (cheap swaps first when multiple are needed)

12. **Multi-GPU extension**: The current architecture assumes a single GPU. For multi-GPU, the scheduler would need:
    - Per-GPU affinity queues
    - Cross-GPU model migration decisions
    - Per-GPU VRAM budgets and health checks
    - GPU selection heuristic (prefer GPU that already has the model)

---

## 10. Test Coverage Assessment

The test suite (`test_scheduler.py`: 1031 lines, `test_queue.py`: 156 lines) provides good coverage of core functionality:

**Well-Tested**:
- Start/stop lifecycle
- Basic dispatch flow
- GPU gating (pauses when unsafe)
- Drain mode
- Concurrent dispatch to co-resident models
- Same-model serialization
- Swap blocking dispatch
- In-flight eviction protection
- Dynamic cooldown escalation (6 tests covering all level transitions)
- VRAM reservation lifecycle (reserve/commit/release/evict-retry)
- Eviction strategy (prefer no-queued-work, protect always_allowed, protect reserved)
- Startup model sync
- Stop timeout handling
- Queue enqueue/dequeue/priority ordering
- Affinity selection
- Queue full rejection
- Cancel and drain operations

**Not Tested**:
- Stall diagnosis logic (`_diagnose_stall()`)
- Proactive eviction after swap (excess model eviction when `len(resident) > max_loaded`)
- Phase 1 -> Phase 2 fallthrough within a single tick
- Multiple concurrent ticks (scheduler processing overlapping work)
- Queue TTL sweep interaction with scheduler (tested separately in `test_e2e_stress.py`)
- Priority aging crossover behavior (BACKGROUND aging past INTERACTIVE)
- Intent-based priority detection (tested in `test_intent.py`)
- `_dispatch_error_fn` callback path (tested in integration)
- Watchdog drain/resume integration

---

## Summary

BASTION's scheduler and queue subsystem is a well-engineered solution to a hard hardware-specific problem (GPU crash prevention via swap rate limiting). The two-phase affinity dispatch algorithm, dynamic cooldown escalation, and VRAM reservation ledger are sophisticated and correct.

The primary gap is **observability**: the scheduler computes rich internal state (swap rate levels, stall reasons, per-request ages, effective priorities) that operators cannot see. Wiring the existing Prometheus metrics and adding a scheduler diagnostics endpoint would be the highest-value immediate improvement.

The secondary gap is **predictive capability**: the intent system and session profiles provide forward-looking model sequence information that the scheduler does not act on. Intent-aware pre-loading would reduce model transition latency by overlapping load times with inference.

The priority system is adequate for the current single-GPU, few-tenant use case but would need per-client fairness and priority ceilings for multi-tenant production deployment.

---

**End of Report**

Generated by Scheduler & Queue Domain Analyst
Session: S0 (Audit Phase)
Dependencies: scout-code-cartography.md, scout-api-surface.md, scout-data-models.md
