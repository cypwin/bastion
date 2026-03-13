# A2A Protocol Analyst Report -- BASTION

**Generated**: 2026-03-13
**Analyst**: A2A Protocol Analyst (Claude Opus 4.6)
**Scope**: Full analysis of A2A protocol implementation against plan, lifecycle coverage, lease management, batch inference, SSE streaming, dual-store internals, session profiles, client gaps, and orchestration potential
**Input Files**: `src/bastion/a2a.py`, `src/bastion/taskstore.py`, `src/bastion/models.py`, `src/bastion/server.py`, `clients/bastion-client/`, `tests/test_a2a.py`, `tests/test_lease.py`, `tests/test_taskstore.py`, scout reports, reference plans

---

## Executive Summary

BASTION's A2A protocol implementation is **substantially complete** relative to the original plan in `docs/audit/ref-a2a-plan.md`. All four planned skills (infer, batch_infer, preload, status) are fully implemented -- not stubs. The task lifecycle state machine is enforced. SSE streaming bridges Ollama NDJSON to A2A events. Model leases with fencing tokens provide zombie-prevention. The dual-store TaskStore implements compaction, TTL eviction, and three-stage backpressure.

However, significant **unrealized capabilities** exist across nine dimensions:

1. **Plan vs. implementation gap**: A2A SDK types planned but not integrated; dynamic VRAM reporting in agent card removed in hardening
2. **Unused state transitions**: WORKING->CANCELED path is defined but unreachable in practice
3. **Lease management gaps**: No lease-aware inference (requests don't decrement lease counters); no lease listing endpoint; no lease renewal
4. **Batch inference limitations**: Sequential-only processing; no concurrency within a batch; no streaming batch results
5. **SSE underutilization**: Dashboard doesn't consume SSE streams; no server-push status broadcasting
6. **Hidden dual-store capabilities**: TaskStore.stats() never queried; backpressure level invisible; tombstone diagnostics unused
7. **Session profiles disconnected from A2A**: Profiles exist in config but A2A tasks don't reference them
8. **Client library has zero A2A support**: bastion-client wraps admin API only
9. **No orchestration primitives**: No task chaining, pipeline routing, or multi-step inference patterns

**Bottom line**: The A2A implementation delivers a solid single-task inference broker. The infrastructure to support multi-agent orchestration, pipeline scheduling, and advanced lease management is partially present but not wired together.

---

## 1. Plan vs. Implementation -- What Was Planned and What Was Delivered

### Reference: `docs/audit/ref-a2a-plan.md`

| Planned Feature | Status | Notes |
|----------------|--------|-------|
| `A2AHandler` class with task store | DONE | Implemented with hardened TaskStore (exceeds plan) |
| `_tasks: Dict[str, A2ATaskRecord]` | DONE | Upgraded to dual-store (active + completed) with compaction |
| `_reservations: Dict[str, Reservation]` | DONE | Plus hybrid leases (not in original plan) |
| `create_task()` method | DONE | With circuit breaker fast-fail (exceeds plan) |
| `get_task()` method | DONE | Returns CompactedResult for terminal tasks |
| `cancel_task()` method | DONE | Validates state machine |
| `subscribe_task()` SSE generator | DONE | With heartbeat, disconnect detection, sentinel shutdown |
| `build_agent_card()` single method | SPLIT | Became `build_public_card()` + `build_extended_card()` (three-tier disclosure) |
| `_handle_infer()` skill | DONE | Both streaming and non-streaming paths |
| `_handle_batch_infer()` skill | DONE | Was marked "stub" in module docstring but is fully implemented |
| `_handle_preload()` skill | DONE | Was marked "stub" in module docstring but is fully implemented |
| `_handle_status()` skill | DONE | Returns queue depth, loaded models, current model |
| SSE streaming bridge | DONE | `_stream_ollama_to_sse()` with TTFT metrics |
| A2A auth (separate from admin) | DONE | `make_a2a_token_dependency()` in `auth.py` |
| Dynamic agent card with VRAM/queue | CHANGED | Agent card hardened: no VRAM/queue in public card (Tier 1); supported models in extended card (Tier 2); raw state only in admin API (Tier 3) |
| A2A SDK type integration | PARTIAL | `A2A_SDK_AVAILABLE` flag exists but SDK types are never used; all serialization uses local dicts |
| `config/broker.yaml` a2a section | DONE | All 6 config fields present |
| Tests | DONE | 1154 lines across `test_a2a.py`, `test_lease.py`, `test_taskstore.py` |

### Features Delivered Beyond Plan

1. **Hybrid lease model** (`ModelLease` with fencing tokens, idle timeout, request counting) -- the plan only specified simple `Reservation` objects
2. **Three-tier agent card disclosure** -- the plan had a single dynamic card; implementation separates public/extended/admin
3. **Dual-store TaskStore** with compaction, tombstones, backpressure -- the plan used a plain dict
4. **Circuit breaker integration** -- fast-fail on open circuit returns JSON-RPC -32050 error
5. **Tiered audit logging** with A2A identity context -- not in original plan
6. **OpenTelemetry producer/consumer span linking** -- not in original plan
7. **Prometheus metrics** for A2A task lifecycle -- not in original plan
8. **Background GC prevention** via `_background_tasks` set with done callbacks

### Features Planned But Not Delivered

1. **A2A SDK type usage** for wire format compliance:
   - Plan: "Use SDK types for wire-format serialization"
   - Reality: SDK availability is detected (`A2A_SDK_AVAILABLE`) but never used. All A2A objects are plain dicts.
   - Impact: Wire format may drift from official A2A spec. No validation against SDK schemas.

2. **Dynamic VRAM reporting in agent card**:
   - Plan: "Dynamic card includes current VRAM availability, currently loaded models, queue depth"
   - Reality: Hardening removed all runtime state from agent cards. Even the extended card (Tier 2) only shows supported models from config, not currently loaded models or VRAM.
   - Impact: A2A agents cannot discover current broker load or available capacity through the card alone. They must use the `status` skill or `/broker/status` (Tier 3).

3. **Reservation consumption on inference**:
   - Plan: "When a request for a reserved model is dispatched, the A2A handler decrements `remaining_requests`. When it hits 0, the reservation is released."
   - Reality: Only `_handle_batch_infer()` decrements `reservation.remaining_requests` (line 1072, 1126). Normal `infer` requests through the proxy do NOT decrement any reservation or lease counter.
   - Impact: Leases created via `preload` skill return `remaining_requests` and `fencing_token`, but subsequent `infer` tasks don't consume them. The lease only expires by TTL, idle timeout, or explicit release.

---

## 2. Task Lifecycle State Machine -- Completeness Analysis

### Defined States

From `src/bastion/models.py` line 188:
```python
class A2ATaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
```

### Valid Transitions

From `src/bastion/taskstore.py` line 85:
```python
_VALID_TRANSITIONS = {
    SUBMITTED: {WORKING, CANCELED, FAILED},
    WORKING: {COMPLETED, FAILED, CANCELED},
    COMPLETED: set(),  # Terminal
    FAILED: set(),     # Terminal
    CANCELED: set(),   # Terminal
}
```

### Transition Usage Analysis

| Transition | Used By | Frequency |
|-----------|---------|-----------|
| SUBMITTED -> WORKING | `_run_skill_handler()` via `_safe_transition()` | Every successful task |
| WORKING -> COMPLETED | All skill handlers on success | Every successful task |
| WORKING -> FAILED | All skill handlers on error, circuit breaker, timeout | Every failed task |
| SUBMITTED -> FAILED | `create_task()` for invalid/unknown skill_id | Validation failures only |
| SUBMITTED -> CANCELED | `cancel_task()` | Client-initiated cancellation |
| WORKING -> CANCELED | `cancel_task()` | Client-initiated cancellation |

### Unreachable/Underutilized Transitions

**SUBMITTED -> FAILED (direct)**: This path is used only for validation failures (missing skill_id, unknown skill_id). The task never enters the active store's WORKING state. This is correct behavior -- fast-fail on bad input.

**WORKING -> CANCELED**: This transition is defined and the code path exists (`cancel_task` checks for both SUBMITTED and WORKING), but it is practically very difficult to trigger because:
- Skill handlers are fire-and-forget asyncio tasks that transition SUBMITTED -> WORKING almost immediately (line 487)
- The window between WORKING and COMPLETED/FAILED is the inference duration
- There is no cooperative cancellation -- `cancel_task()` only changes state, it does not cancel the background asyncio.Task
- The background task will still complete and attempt to transition, which `_safe_transition` handles gracefully by returning False

**Gap**: True cancellation would require:
1. Storing a reference to the background `asyncio.Task` per task_id
2. Calling `task.cancel()` in `cancel_task()`
3. Handling `asyncio.CancelledError` in `_run_skill_handler()` to transition to CANCELED
4. For streaming infer, also cancelling the httpx streaming connection

Currently, "canceling" a WORKING task only changes the state -- the Ollama inference continues to consume GPU resources until completion.

---

## 3. Model Lease Analysis -- Capabilities and Gaps

### Current Lease Architecture

The lease system has **two layers** (a design evolution visible in the code):

**Layer 1: Reservations (backward compat)**
- `Reservation` model in `models.py` (line 463)
- Stored in `A2AHandler._reservations` dict
- Created by `_handle_batch_infer()` and `_handle_preload()`
- Checked by scheduler via `has_active_reservation(model)` callback
- Simple: model name, remaining_requests, expires_at

**Layer 2: Hybrid Leases (upgrade)**
- `ModelLease` model in `models.py` (line 480)
- Stored in `A2AHandler._leases` dict
- Created by `create_lease()` method (called from `_handle_preload()`)
- Has fencing tokens for zombie prevention
- Has idle timeout, request counting, touch() heartbeat
- State machine: ACTIVE -> EXPIRED / RELEASED

### Lease Feature Analysis

| Feature | Implemented | Wired Up | Notes |
|---------|------------|----------|-------|
| Create lease | Yes | Via preload skill | `create_lease()` at line 1475 |
| Validate lease + fencing token | Yes | Via heartbeat endpoint | `validate_lease()` at line 1515 |
| Release lease explicitly | Yes | Via DELETE endpoint | `release_lease()` at line 1548 |
| Heartbeat (touch) | Yes | Via POST endpoint | `a2a_lease_heartbeat()` in server.py |
| Check active lease for model | Yes | Via scheduler callback | `has_active_lease()` at line 1570 |
| Idle timeout eviction | Yes | Via cleanup loop | 30-second sweep interval |
| TTL expiration | Yes | Via cleanup loop | Monotonic time comparison |
| Request count exhaustion | Yes | Via `should_release()` | Checked but never decremented by inference |
| Fencing token monotonic increment | Yes | Via `_next_fencing_token()` | Counter-based, process-scoped |
| Lease listing endpoint | **No** | - | No `/a2a/leases` endpoint exists |
| Lease renewal/extension | **No** | - | No way to extend TTL or add requests |
| Lease-aware inference | **No** | - | `infer` skill does not validate or consume leases |
| Lease transfer | **No** | - | No way to reassign a lease to another agent |

### Critical Gap: Lease-Inference Disconnect

The `preload` skill creates both a Reservation and a ModelLease, returning the `lease_id` and `fencing_token` to the client. The client can heartbeat the lease. But when the client subsequently calls the `infer` skill, there is no parameter to pass a `lease_id`. The infer handler:
- Does not check for an active lease
- Does not decrement `remaining_requests`
- Does not call `lease.use_request()`
- Does not validate the fencing token

This means leases serve **only** as model eviction prevention (the scheduler checks `has_active_reservation/lease` before unloading). They do not provide:
- Request accounting (tracking how many of the reserved N requests have been used)
- Priority elevation (leased requests could bypass queue)
- Fencing protection for inference (a stale client could still submit requests after lease expiry)

### Lease Management Improvements Possible

1. **Add `lease_id` parameter to infer skill**: Validate lease, call `use_request()`, reject if expired
2. **Add `/a2a/leases` listing endpoint**: Return all active leases with remaining time/requests
3. **Add lease renewal endpoint**: `POST /a2a/leases/{id}/renew` to extend TTL or add requests
4. **Priority elevation for leased requests**: Requests with valid lease_id get `PriorityTier.INTERACTIVE` instead of `AGENT`
5. **Lease metrics**: Expose lease utilization (created/released/expired/idle counts)

---

## 4. Batch Inference -- Capabilities and Enhancement Opportunities

### Current Implementation

`_handle_batch_infer()` (line 922, 294 lines) implements a complete batch inference pipeline:

1. Validates model, prompts list, batch size limit
2. Enqueues first prompt through the scheduler (ensures model is loaded)
3. Creates a Reservation to prevent model eviction
4. Processes all prompts sequentially via direct Ollama calls (bypasses queue)
5. Pushes per-prompt artifact updates via SSE
6. Handles partial failures (per-prompt success/failure tracking)
7. Cleans up reservation on completion
8. Creates final BatchInferResult artifact

### Batch Inference Strengths

- **Reservation-based model pinning**: Prevents model eviction during batch processing
- **Partial failure handling**: Individual prompt failures don't abort the batch
- **Progressive SSE updates**: Each prompt result is pushed to subscribers as it completes
- **Audit integration**: Batch completion emitted as tiered audit event

### Batch Inference Limitations and Enhancement Opportunities

**1. Sequential-only processing**

All prompts are processed sequentially (line 1103: `for idx, prompt in enumerate(prompts[1:], start=1)`). Ollama supports concurrent requests to the same model when `OLLAMA_NUM_PARALLEL > 1`. Parallel batch processing could significantly reduce wall-clock time.

Enhancement: Add `concurrency` parameter to batch_infer. Process prompts in batches of N using `asyncio.gather()` or `asyncio.Semaphore`.

**2. No streaming within batch**

Each prompt uses `stream: False`. For long-running prompts, the client sees no progress until the entire prompt completes.

Enhancement: Add `stream` parameter to batch_infer. When True, use `_stream_ollama_to_sse()` for each prompt with per-prompt artifact IDs.

**3. No chat endpoint support**

Batch inference only uses `/api/generate`. The `/api/chat` endpoint (multi-turn conversations) is not supported.

Enhancement: Add `endpoint` parameter (default: "generate", option: "chat"). For chat, accept `messages` array instead of `prompts` strings.

**4. No batch cancellation**

Once a batch starts, there is no way to stop it mid-flight. `cancel_task()` changes the state but doesn't interrupt the processing loop.

Enhancement: Check `record.state` before each prompt in the loop. If CANCELED, break early and report partial results.

**5. No batch retry**

Failed prompts are recorded but never retried. A transient Ollama error on prompt 3 of 50 means that result is permanently failed.

Enhancement: Add `retry_count` parameter. On failure, retry individual prompts up to N times with exponential backoff.

**6. First prompt goes through queue, rest bypass it**

The reservation model works but creates an asymmetry: prompt 0 has queue latency, prompts 1-N have direct latency. If another agent submits a high-priority request for a different model while the batch is running, the batch holds the model pinned via reservation, potentially starving the higher-priority request.

Enhancement: Add reservation priority degradation over time (aging in reverse for long-running batches).

---

## 5. SSE Streaming -- Underutilized Capabilities

### Current SSE Implementation

The SSE implementation is **robust and well-hardened**:

- **`subscribe_task()` generator** (a2a.py line 356): Bounded queues (maxsize=100), heartbeat every 15s, client disconnect detection, CancelledError contract compliance, sentinel shutdown
- **`_sse_wrapper()`** (server.py line 872): Converts dict events to SSE wire format, handles heartbeat markers
- **`_notify_subscribers()`** (a2a.py line 1887): Fan-out with drop-oldest on full queues
- **`_stream_ollama_to_sse()`** (a2a.py line 708): Bridges Ollama NDJSON to A2A artifact events with TTFT metrics

### SSE Underutilization

**1. Dashboard does not consume SSE streams**

The Code Cartographer scout identified this: `dashboard.py` polls HTTP endpoints every 2 seconds. It could subscribe to `/a2a/tasks/{id}/stream` for real-time task progress visualization, especially for batch inference (showing each prompt result as it arrives).

The dashboard is fully HTTP-isolated (zero internal imports), so it would use `httpx.AsyncClient.stream()` to consume SSE.

**2. No server-push for broker state changes**

The SSE infrastructure only serves task-specific streams. There is no `GET /broker/status/stream` or `GET /a2a/events` endpoint for global state changes (model loads/unloads, queue depth changes, circuit breaker state transitions).

Adding a global event bus would enable:
- Dashboard real-time updates without polling
- A2A agents subscribing to model availability changes
- External monitoring systems receiving push notifications

**3. No SSE for batch progress aggregation**

While `_handle_batch_infer()` pushes per-prompt artifact updates, there is no aggregate progress event (e.g., "batch is 60% complete"). Clients must count individual artifact events to determine overall progress.

**4. Streaming infer only for `/api/generate` format**

`_stream_ollama_to_sse()` handles both `/api/generate` (response field) and `/api/chat` (message.content field) NDJSON formats. However, the `infer` skill only builds `/api/generate` payloads. Chat-format streaming is prepared but unreachable.

**5. No reconnection protocol**

If an SSE client disconnects and reconnects, it misses all events during the gap. The A2A spec supports `Last-Event-ID` for resumption, but BASTION does not implement it. Events have no sequential IDs.

---

## 6. Dual-Store TaskStore -- Hidden Capabilities

### Architecture

The `TaskStore` (439 lines) implements a sophisticated data management system that significantly exceeds the original plan's "plain dict":

```
Active Store (dict[str, A2ATaskRecord])
    |
    | [terminal state transition]
    v
Completed Store (OrderedDict[str, CompactedResult])
    |
    | [TTL expiry or capacity eviction]
    v
Tombstone Store (OrderedDict[str, float])
    |
    | [2x completed TTL]
    v
[Garbage collected]
```

### Hidden/Underutilized Features

**1. `stats()` method never queried**

`TaskStore.stats()` (line 302) returns a comprehensive health report:
```python
{
    "active_count": int,
    "completed_count": int,
    "tombstone_count": int,
    "subscriber_count": int,
    "pressure_level": str,
    "maxsize": int,
}
```

No endpoint calls this method. The Data Model Scout confirmed: "No A2A endpoint returns task store health. Can't see backpressure state or eviction counts."

Recommendation: Add `GET /a2a/stats` endpoint that returns `_store.stats()`.

**2. Backpressure invisible to clients**

Three-stage backpressure (NORMAL -> PRESSURE -> OVERLOADED) is implemented with hysteresis (80% up, 70% down thresholds) but:
- Clients only see the effect (TaskStoreFullError / `retry_after: 60`)
- No way to query current pressure level proactively
- No warning before hitting OVERLOADED
- The `retry_after` value is hardcoded at 60 seconds regardless of actual pressure

Enhancement: Return `X-Backpressure-Level` header on task creation responses. Reduce `retry_after` dynamically based on completion rate.

**3. Adaptive completed TTL under pressure**

`_effective_completed_ttl` (line 332) reduces completed task retention from the configured TTL (default 1 hour) to 5 minutes when under PRESSURE. This is a memory management feature that works silently. Clients are not informed that completed task results may expire faster during high load.

Enhancement: Include `effective_ttl_seconds` in task GET responses so clients know how long the result will be available.

**4. Tombstone diagnostics unused**

Tombstones track evicted task IDs (up to 10,000) to prevent ID confusion. The `get()` method checks tombstones but returns None indistinguishably from "never existed." No API distinguishes "task existed but was evicted" from "task never existed."

Enhancement: Return HTTP 410 Gone for tombstoned tasks instead of 404 Not Found.

**5. `count_by_state()` only used for metrics**

`count_by_state("submitted")` is called once (a2a.py line 282) to update a Prometheus gauge. It could power a richer `/a2a/stats` endpoint showing the state distribution of active tasks.

**6. Subscriber cleanup for orphaned tasks**

The sweep logic (line 422) cleans up subscriber lists for tasks that no longer exist in active or completed stores. This is a safety net, not a primary feature. But it means SSE connections for long-completed tasks are silently closed during the next sweep cycle (every 60 seconds).

---

## 7. Session Profiles and Multi-Agent Patterns

### Current Session Profile Implementation

From `src/bastion/models.py` line 339:
```python
class SessionProfile(BaseModel):
    model_sequence: list[str]
    default_priority: PriorityTier
    description: str
```

Session profiles are configured in `broker.yaml` under `session_profiles` and loaded into `BrokerConfig.session_profiles`. They are used by:
- `POST /broker/intent` -- clients declare an upcoming model sequence
- `proxy.py` -- priority detection via `X-Broker-Intent` header
- `server.py` -- intent resolution and tracking

### Disconnect from A2A

**Session profiles are completely disconnected from the A2A protocol**. There is no way for an A2A agent to:

1. Discover available session profiles (no skill or card field lists them)
2. Declare an intent via A2A (must use `/broker/intent` admin endpoint)
3. Reference a profile when creating A2A tasks
4. Benefit from profile-based priority elevation in A2A task processing

### Multi-Agent Patterns This Could Enable

**1. Pipeline declarations via A2A**

An orchestrating agent could declare: "I will use models [qwen3:30b, phi4:14b, nomic-embed-text] in sequence, 5 requests each." The scheduler could pre-plan transitions, minimize swaps, and elevate priority for the declared sequence.

Implementation: Add `declare_pipeline` skill that wraps `/broker/intent` functionality.

**2. Context-linked task chains**

A2ATaskRecord has `context_id` (shared across related tasks), but nothing uses it for scheduling. Tasks with the same `context_id` could be:
- Prioritized together (pipeline affinity)
- Scheduled on the same model sequence (minimize swaps)
- Tracked as a pipeline with aggregate progress

Implementation: Add `context_id` to QueuedRequest. Scheduler uses context_id to group related requests.

**3. Model-aware agent negotiation**

Currently, agents submit tasks and hope the model is available. With session profiles + A2A integration:
1. Agent discovers available models via extended card
2. Agent declares intent (profile or ad-hoc sequence)
3. BASTION pre-loads model and creates lease
4. Agent submits N tasks with lease_id for guaranteed scheduling

Implementation: Add `negotiate_session` skill that combines intent declaration + preload + lease creation.

**4. Multi-agent council pattern**

BASTION's priority tiers (interactive > agent > pipeline > background) can support a council of agents where:
- The orchestrator gets interactive priority
- Council members get agent priority
- Background indexing gets background priority

But there is no A2A mechanism to assign different priority tiers to different A2A tokens. All A2A tasks get `PriorityTier.AGENT` (line 596).

Enhancement: Add `priority_tier` field to A2AConfig per token, or allow task-level priority specification.

---

## 8. Client Library Gaps

### Current bastion-client Capabilities

From `clients/bastion-client/bastion_client/client.py` (143 lines):

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `declare_intent()` | `POST /broker/intent` | Pre-announce model sequences |
| `infer()` | `POST /api/generate` | Submit inference with priority |
| `check_vram()` | `GET /broker/status` | Query GPU/VRAM status |

### Zero A2A Support

The client library has **no A2A methods**. The API Surface Scout confirmed: "No A2A client wrapper yet."

Missing client methods:
1. `discover()` -- fetch `/.well-known/agent-card.json`
2. `get_extended_card()` -- fetch `/a2a/extended-card` with auth
3. `create_task()` -- `POST /a2a/tasks`
4. `get_task()` -- `GET /a2a/tasks/{id}`
5. `cancel_task()` -- `DELETE /a2a/tasks/{id}`
6. `stream_task()` -- `GET /a2a/tasks/{id}/stream` (SSE consumer)
7. `batch_infer()` -- create batch_infer task with progress tracking
8. `preload()` -- create preload task, return lease info
9. `heartbeat_lease()` -- `POST /a2a/leases/{id}/heartbeat`
10. `release_lease()` -- `DELETE /a2a/leases/{id}`

### Client Model Gaps

From `clients/bastion-client/bastion_client/models.py` (47 lines):

Only 4 models defined: `IntentRequest`, `IntentResponse`, `VRAMInfo`, `InferenceResult`.

Missing models:
- `A2ATaskResponse` -- parsed task object
- `BatchInferRequest` / `BatchInferResult`
- `LeaseInfo` -- lease_id, fencing_token, remaining_requests, expires_at
- `AgentCard` -- parsed card with skills, capabilities
- `SSEEvent` -- typed event for streaming

### Test Coverage Gap

`clients/bastion-client/tests/test_client.py` exists but does not test any A2A functionality (because none exists in the client).

---

## 9. Agent Orchestration and Pipeline Routing Potential

### Current Orchestration Primitives

BASTION has the building blocks for orchestration but they are not assembled:

| Primitive | Present | Wired to A2A |
|-----------|---------|-------------|
| Priority tiers (4 levels) | Yes | Partially (A2A always uses AGENT) |
| Session profiles (model sequences) | Yes | No |
| Intent declarations (pre-announce) | Yes | No |
| Model leases (eviction prevention) | Yes | Not consumed by inference |
| Context IDs (task grouping) | Yes | Not used for scheduling |
| Batch inference (N prompts) | Yes | Yes |
| SSE streaming (real-time updates) | Yes | Yes |
| Circuit breaker (fast-fail) | Yes | Yes (error -32050) |
| Backpressure (task store) | Yes | Yes (TaskStoreFullError) |

### Orchestration Patterns BASTION Could Support

**Pattern 1: Sequential Pipeline**
```
Agent -> [preload model A] -> [infer x5 with lease] ->
         [preload model B] -> [infer x3 with lease] -> done
```
Requires: Lease-aware inference, task chaining

**Pattern 2: Fan-out / Fan-in**
```
Agent -> [batch_infer: 10 prompts same model] -> aggregate results
```
Already supported. Enhancement: Parallel prompt processing.

**Pattern 3: Model Council (Quorum)**
```
Agent -> [infer model A] --\
      -> [infer model B] ---+-> aggregate / vote
      -> [infer model C] --/
```
Requires: Parallel task submission with shared context_id, aggregation logic

**Pattern 4: RAG Pipeline**
```
Agent -> [embed query (nomic)] -> [retrieve from vector DB] ->
         [infer with context (qwen3:30b)] -> response
```
Requires: Task chaining, cross-task data passing (artifact from task A -> input for task B)

**Pattern 5: Adaptive Routing**
```
Agent -> check status ->
  if model A loaded: infer on A
  elif model B loaded: infer on B
  else: preload cheapest model -> infer
```
Already possible via `status` skill + conditional logic, but entirely client-side.

### What Would Be Needed for Full Orchestration

1. **Task chaining**: A `depends_on: [task_id]` field in task creation that waits for upstream tasks to complete before starting
2. **Artifact forwarding**: Pass output artifacts from one task as input_params to another
3. **Pipeline skill**: A meta-skill that accepts a pipeline definition (sequence of skills + models) and executes them as a unit
4. **Priority escalation**: Allow A2A tasks to specify priority tier (currently hardcoded to AGENT)
5. **Lease-bound task groups**: Associate a set of tasks with a lease, automatically decrementing on each completion
6. **Callback/webhook**: Notify a URL when a task reaches a terminal state (the plan mentions `pushNotifications: False` in the agent card capabilities)

---

## 10. Detailed Findings by File

### `src/bastion/a2a.py` (1894 lines)

**Strengths:**
- Comprehensive error handling in all skill handlers (circuit breaker, HTTP errors, queue full, timeouts)
- Clean separation of concerns (task lifecycle, skill routing, lease management, agent card)
- GC prevention for fire-and-forget tasks via `_background_tasks` set
- Tiered audit logging with A2A identity context
- OpenTelemetry producer/consumer span linking for distributed tracing

**Issues found:**
1. **Module docstring says "batch_infer: Batch inference (stub)" and "preload: Model reservation (stub)"** (line 10-11) but both are fully implemented. The docstring is stale.
2. **`_handle_preload()` creates BOTH a Reservation AND a Lease** (lines 1356-1378). This is redundant. The Reservation is kept for backward compatibility with the scheduler's `has_active_reservation()` check, but the Lease has the same functionality plus more. The dual creation could lead to state divergence if one expires before the other.
3. **No cooperative task cancellation**: `cancel_task()` changes state but doesn't cancel the underlying asyncio.Task. GPU resources continue to be consumed.
4. **`_cleanup_expired_reservations()` runs every 30 seconds forever**: No graceful shutdown of this background task. When the server shuts down, this could cause warnings about pending tasks.
5. **Shared httpx client lifecycle**: `_http_client` is passed in but never closed by A2AHandler. The lifecycle is managed by server.py, which is correct, but there is no assertion or documentation of this contract.

### `src/bastion/taskstore.py` (439 lines)

**Strengths:**
- Excellent separation from A2AHandler (clean interface)
- Proper state machine validation with clear error messages
- Hysteresis in backpressure prevents oscillation
- Orphan subscriber cleanup in sweep

**Issues found:**
1. **`CompactedResult.completed_at` uses `time.monotonic()`** (line 72) but `A2ATaskRecord.created_at` uses `time.time()` (line 206 of models.py). This makes it impossible to compute "how long did the task take from creation to completion" without knowing both clocks. The `completed_at` monotonic timestamp is also not meaningful across process restarts.
2. **`_effective_completed_ttl` is only applied in `get()` and `_sweep()`**: Tasks accessed via other paths (e.g., iterating `_completed` directly) don't get the pressure-adjusted TTL.
3. **`start_cleanup()` must be called externally**: If forgotten, no periodic cleanup runs. The TaskStore does not start cleanup automatically on creation.

### `src/bastion/models.py` (528 lines)

**A2A-relevant models are well-structured.** Key observations:

1. **`ModelLease.expiry` uses `time.monotonic()`** but **`ModelLease.created_at` uses `time.time()`**: Inconsistent clock usage (same issue as CompactedResult). Cannot compute "absolute expiry time" from these fields.
2. **`BatchInferRequest` and `ReservationRequest` models exist but are never used**: The skill handlers parse `record.input_params` dict directly instead of validating through these models. This means invalid batch requests get deeper into processing before failing.
3. **`SessionProfile`** has no link to A2A at all -- it references `PriorityTier` and model sequences but is only used by the intent system.

### `src/bastion/server.py` (A2A routes section)

**Well-implemented A2A routes.** Key observations:

1. **Three-tier agent card** (public, extended, admin) is correctly separated with auth boundaries
2. **SSE wrapper** handles heartbeats, sentinels, and proper SSE wire format
3. **Lease heartbeat endpoint** validates fencing token before touching -- correct zombie prevention
4. **Two-port mode** duplicates A2A routes (lines 1430-1547). All A2A route logic is duplicated between `create_app()` and `create_admin_app()`. Changes to one must be mirrored in the other.

---

## 11. Test Coverage Assessment

### Test Files and Line Counts

| File | Lines | Coverage Area |
|------|-------|--------------|
| `tests/test_a2a.py` | 1154 | Task lifecycle, infer, status, batch_infer, preload, agent card (3-tier), SSE streaming, reservations |
| `tests/test_lease.py` | 492 | ModelLease model, should_release(), A2AHandler lease ops, zombie cleanup, heartbeat flow |
| `tests/test_taskstore.py` | 462 | Basics, state transitions, dual-store, TTL, backpressure, subscribers, stats, cleanup |

### Coverage Gaps

1. **No integration tests**: All tests mock Ollama. No tests verify that A2A tasks actually flow through the real queue, scheduler, and VRAM tracker.
2. **No SSE wire format tests**: Tests verify event content but not that the output matches SSE spec (`data: {json}\n\n`).
3. **No concurrent task tests**: Tests create one task at a time. No tests for concurrent task creation, backpressure behavior under load, or race conditions.
4. **No circuit breaker A2A integration test**: The test for `create_task` fast-fail on open circuit is missing (though the code path exists at line 199).
5. **No lease-inference integration test**: No test verifying that a preload -> infer -> release flow works end-to-end.
6. **No two-port mode A2A test**: A2A routes are duplicated in both port modes but only tested in single-port mode.

---

## 12. Summary of Recommendations (Priority Ordered)

### High Priority (Functional Gaps)

1. **Implement lease-aware inference**: Add optional `lease_id` + `fencing_token` parameters to the `infer` skill. Validate lease, decrement `remaining_requests`, reject expired leases.
2. **Add cooperative task cancellation**: Store asyncio.Task references per task_id. Cancel the background task when `cancel_task()` is called.
3. **Fix stale module docstring**: Remove "stub" labels from batch_infer and preload in `a2a.py` docstring (line 10-11).
4. **Add A2A client library methods**: Implement at minimum `create_task()`, `get_task()`, `stream_task()`, `preload()`, `heartbeat_lease()` in bastion-client.

### Medium Priority (Observability)

5. **Expose TaskStore stats**: Add `GET /a2a/stats` endpoint returning `_store.stats()`.
6. **Add lease listing endpoint**: `GET /a2a/leases` returning active leases with remaining time/requests.
7. **Return HTTP 410 for tombstoned tasks**: Distinguish "evicted" from "never existed" in task GET.
8. **Add backpressure header**: Return `X-Backpressure-Level` on task creation responses.

### Lower Priority (Enhancement)

9. **Connect session profiles to A2A**: Add `declare_pipeline` skill or `profile` parameter to task creation.
10. **Add parallel batch processing**: Allow concurrent prompt processing within `batch_infer` via `concurrency` parameter.
11. **Implement SSE Last-Event-ID**: Enable client reconnection without missing events.
12. **Unify Reservation and Lease**: Remove dual Reservation+Lease creation in preload; use Lease exclusively.
13. **Consistent clock usage**: Standardize on `time.time()` for human-readable timestamps and `time.monotonic()` for duration calculations. Do not mix them in the same object.
14. **Validate input via Pydantic models**: Use `BatchInferRequest` and `ReservationRequest` to validate `input_params` instead of manual dict parsing.

---

## Appendix: File Reference

| File | Path | Relevance |
|------|------|-----------|
| A2A Handler | `/home/user/BASTION/src/bastion/a2a.py` | Core protocol handler (1894 lines) |
| Task Store | `/home/user/BASTION/src/bastion/taskstore.py` | Dual-store with compaction (439 lines) |
| Models | `/home/user/BASTION/src/bastion/models.py` | A2ATaskRecord, ModelLease, Reservation, etc. (528 lines) |
| Server Routes | `/home/user/BASTION/src/bastion/server.py` | A2A route handlers (lines 870-1060) |
| Client Library | `/home/user/BASTION/clients/bastion-client/bastion_client/client.py` | No A2A support (143 lines) |
| Client Models | `/home/user/BASTION/clients/bastion-client/bastion_client/models.py` | No A2A models (47 lines) |
| A2A Tests | `/home/user/BASTION/tests/test_a2a.py` | Task lifecycle, skills, cards, SSE (1154 lines) |
| Lease Tests | `/home/user/BASTION/tests/test_lease.py` | Lease model and handler tests (492 lines) |
| TaskStore Tests | `/home/user/BASTION/tests/test_taskstore.py` | Dual-store, TTL, backpressure (462 lines) |
| A2A Plan | `/home/user/BASTION/docs/audit/ref-a2a-plan.md` | Original implementation plan |
| Phase 3 Protocol | `/home/user/BASTION/docs/audit/ref-phase3-protocol.md` | Phase planning document |

---

**End of Report**

Generated by A2A Protocol Analyst
Session: S0 (Audit Phase)
