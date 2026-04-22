# BASTION Architecture

## Three-Layer Design

BASTION is structured as three cooperating layers sharing in-memory state:

### Layer 1: Ollama Proxy (`/api/*`)

The transparent reverse proxy that makes BASTION invisible to Ollama clients. Every request to `localhost:11434/api/*` passes through here.

**Responsibilities:**
- Parse request body to extract model name and streaming flag
- Inject safety overrides (`use_mmap: false`, default `num_ctx`)
- Detect priority tier from `X-Broker-Priority` header or User-Agent heuristics
- For scheduled endpoints (`/api/generate`, `/api/chat`, `/api/embed`): enqueue in the AffinityQueue and await scheduler grant
- For passthrough endpoints (`/api/tags`, `/api/ps`, `/api/show`, etc.): forward directly
- Stream NDJSON responses without buffering (critical for `ollama run`)
- Record circuit breaker success/failure for backend health tracking
- Cache `/api/tags` response for graceful degradation when Ollama is down

### Layer 2: Admin API (`/broker/*`)

Management and monitoring endpoints for operators and the TUI dashboard.

**Responsibilities:**
- Expose broker status, queue depth, GPU health, VRAM ledger
- Model management (preload, unload)
- Scheduler control (drain, resume)
- Kubernetes-compatible health probes (`/broker/livez`, `/broker/readyz`)
- Prometheus metrics endpoint
- Intent declaration for scheduler optimization
- Recent request trace viewer

### Layer 3: A2A Agent Interface (`/a2a/*`, `/.well-known/*`)

Agent-to-Agent protocol endpoints for machine-to-machine communication.

**Responsibilities:**
- Agent card discovery (three-tier disclosure: public, extended, admin)
- Task lifecycle management (create, get, cancel, stream)
- Skill routing (infer, batch_infer, preload, status)
- Model leases with hybrid eviction triggers
- SSE streaming for real-time task updates

---

## Request Flow

```
Client (ollama run / curl / agent)
  |
  | HTTP POST /api/generate {"model": "mymodel:8b", "prompt": "..."}
  v
OllamaProxy.handle_request()
  |
  |-- Parse body, extract model name
  |-- Inject use_mmap: false, default num_ctx
  |-- Detect priority tier (header / User-Agent)
  |-- Is scheduled endpoint? (/api/generate, /api/chat, /api/embed)
  |     |
  |     YES: Create QueuedRequest
  |     |    Call _enqueue_fn() -> (grant_event, done_fn)
  |     |    AffinityQueue.enqueue() -> model sub-queue
  |     |    Scheduler.notify() -> wake scheduler loop
  |     |    await grant_event (blocks until scheduler grants)
  |     |
  |     |    [Scheduler loop runs in background]
  |     |    Scheduler._process_tick()
  |     |      |-- Check GPU health (nvidia-smi)
  |     |      |-- Phase 1: dispatch to co-resident models (non-blocking)
  |     |      |     |-- For each model with queued work:
  |     |      |     |     Is model resident? (ResidencyCache)
  |     |      |     |     Has in-flight request? (serialize same-model)
  |     |      |     |     -> Dispatch concurrently (up to max_concurrent)
  |     |      |-- Phase 2: handle non-resident model (swap needed)
  |     |      |     |-- Check cooldown (dynamic swap rate limiter)
  |     |      |     |-- VRAM reservation (assume/confirm/forget)
  |     |      |     |-- Evict models if needed (least-useful first)
  |     |      |     |-- Dispatch (blocking -- serialized swap)
  |     |      |
  |     |    _dispatch_request() -> grant_event.set()
  |     |
  |     |    [Proxy handler unblocked]
  |     |
  |     NO: Forward directly to Ollama (passthrough)
  |
  |-- Check circuit breaker (fast-fail if open)
  |-- Forward to Ollama backend (http://127.0.0.1:11435)
  |     |
  |     |-- Streaming: _stream_response() -> NDJSON passthrough
  |     |     done_fn() called in generator's finally block
  |     |
  |     |-- Non-streaming: _forward_response() -> JSON response
  |     |     done_fn() called after response
  |
  |-- Record audit event
  |-- Record in recent requests ring buffer
  v
Response returned to client
```

---

## VRAM Management

BASTION uses a multi-layer VRAM management strategy to prevent overcommit and crashes.

### VRAM Budget

```
Total VRAM:     32 GB (example — adjust for your GPU)
Headroom:       -8 GB (OS, display, CUDA overhead, KV cache)
                ------
Usable budget:  24 GB (gpu.max_vram_gb = total - headroom)
Safety margin:  10% of total (VRAMManager)
```

### Data Sources

Two complementary sources provide VRAM state:

1. **Ollama `/api/ps`** -- model-level state (which models are loaded, size per model)
2. **nvidia-smi** -- hardware-level truth (total/used/free VRAM, temperature, power)

These can disagree: Ollama may auto-unload models that nvidia-smi still reports as allocated. Both are checked before allowing model loads.

### VRAMManager: Assume/Confirm/Forget Pattern

The VRAMManager eliminates TOCTOU (Time-of-Check-Time-of-Use) races by atomically reserving VRAM before async model loading:

```
1. reserve(model, vram_bytes)    -> VRAMReservation
   [Deducts from available pool immediately -- no await between check and deduction]

2. Load model (async)
   [Protected by reservation + load semaphore]

3a. commit(reservation)          -> Move from reserved to allocated (success)
3b. release(reservation)         -> Return to available pool (failure/TTL)
```

**Concurrency safety:** The Lock in `reserve()` is defense-in-depth. Python's asyncio cooperative scheduling already makes synchronous code between await points atomic, but the lock prevents bugs from future refactoring.

**TTL safety net:** Reservations expire after 120 seconds (configurable). A background sweep reclaims expired reservations to prevent permanent VRAM leaks from crashed load operations.

### VRAM Convergence

After unloading a model, Ollama's scheduler doesn't free VRAM instantly. `VRAMManager.wait_for_vram_convergence()` polls nvidia-smi every 250ms until the free VRAM delta is less than 1 MB, or times out after 5 seconds.

### ResidencyCache

The scheduler needs to know which models are currently loaded. To avoid hammering Ollama's `/api/ps` on every scheduling tick, a `ResidencyCache` wraps the query with a 1-second TTL. The cache is explicitly invalidated after BASTION-initiated load/unload operations.

---

## Priority System

### Priority Tiers

| Tier | Base Priority | Typical Use |
|------|---------------|-------------|
| INTERACTIVE | 100 | `ollama run`, Claude Code MCP (user-facing) |
| AGENT | 50 | Autonomous agents, A2A clients |
| PIPELINE | 25 | Batch pipelines (extract, ingest) |
| BACKGROUND | 10 | Night cycle, consolidation, embedding |

### Priority Detection

Priority is determined in order of precedence:
1. **Explicit header**: `X-Broker-Priority: pipeline`
2. **User-Agent heuristic**: User-Agent containing "ollama" -> INTERACTIVE
3. **Default**: AGENT

### Priority Aging

Effective priority increases over time to prevent starvation:

```
effective_priority = base_priority + (age_seconds * aging_rate) + affinity_bonus
```

With `aging_rate = 2.0`:
- A BACKGROUND request (base=10) waiting 45 seconds has effective priority 100
- This equals a fresh INTERACTIVE request, ensuring background jobs eventually get served

### Model Affinity Bonus

When the scheduler picks the next request to serve, requests targeting the currently loaded model get a `model_affinity_bonus` (default 10.0). This encourages draining same-model requests before swapping, reducing total GPU transitions.

### Affinity Queue Structure

Requests are grouped into per-model sub-queues. `pick_next()` scans all sub-queues and returns the request with the highest effective priority (including affinity bonus). `dequeue_for_model()` takes the highest-priority request within a specific model's queue.

---

## State Machines

### Task Lifecycle (A2A)

```
           +-- create_task() --+
           v                   |
       SUBMITTED               |
           |                   |
           | _run_skill_handler()
           v
        WORKING
           |
     +-----+-----+
     |     |      |
     v     v      v
 COMPLETED FAILED CANCELED
```

**Valid transitions:**
- `submitted -> working` (skill handler starts)
- `submitted -> canceled` (via DELETE before processing)
- `working -> completed` (skill handler succeeds)
- `working -> failed` (skill handler error, timeout, circuit open)
- `working -> canceled` (via DELETE during processing)

Terminal tasks are compacted from the active store into a completed store that retains only task_id, status, error, artifacts, and a result summary. Compacted tasks are garbage collected after `task_ttl_seconds` (default 1 hour).

### Circuit Breaker

Three-state breaker protecting BASTION from cascading failures when Ollama is down:

```
    CLOSED --(N consecutive failures)--> OPEN --(recovery_timeout)--> HALF_OPEN
       ^                                                                  |
       +------------------(probe succeeds)--------------------------------+

    HALF_OPEN --(probe fails)--> OPEN (reset recovery timer)
```

- **CLOSED**: Normal operation. Backend calls proceed. Consecutive failures counted.
- **OPEN**: Fast-fail with 503. No backend calls. Wait `recovery_timeout` (30s default).
- **HALF_OPEN**: Allow one probe request. Success -> CLOSED. Failure -> OPEN (reset timer).

**Configuration:**
- `failure_threshold`: 5 consecutive failures to trip open
- `recovery_timeout`: 30 seconds before half-open probe

**Graceful degradation:** When the circuit is open and a client requests `/api/tags`, BASTION returns the last cached successful response.

### Task Store (Dual-Store Architecture)

The `TaskStore` manages A2A task lifecycle with a dual-store architecture designed for memory efficiency and bounded resource usage.

```
                  create()
                     |
                     v
              +--------------+
              | Active Store |  (Dict[str, A2ATaskRecord])
              | max: 10,000  |
              +------+-------+
                     |
           update_state() to terminal
           (completed/failed/canceled)
                     |
                     v
           CompactedResult.from_record()
                     |
                     v
            +-----------------+
            | Completed Store |  (OrderedDict[str, CompactedResult])
            | max: 50,000     |
            +--------+--------+
                     |
              TTL expired or
              capacity overflow
                     |
                     v
            +----------------+
            | Tombstones     |  (OrderedDict[str, float])
            | max: 10,000    |
            +----------------+
```

**Active store:** Holds full `A2ATaskRecord` objects for tasks in `submitted` or `working` state. Bounded to 10,000 entries with three-stage backpressure:
- **NORMAL** (< 80% capacity): accept all tasks
- **PRESSURE** (80-100%): accept tasks but reduce completed TTL to 5 minutes
- **OVERLOADED** (100%): reject new tasks with `TaskStoreFullError` (retry after 60s)

Hysteresis prevents oscillation: PRESSURE -> NORMAL requires dropping below 70%.

**Completed store:** Holds lightweight `CompactedResult` objects (frozen dataclass, ~200 bytes each). Retains `task_id`, final `status`, output `artifacts` (as immutable tuple), a 500-char `result_summary`, any `error` message, and a monotonic `completed_at` timestamp. Bounded to 50,000 entries (FIFO eviction).

**Tombstones:** Records that a task existed but was evicted. Allows distinguishing "never existed" from "existed but expired". Bounded to 10,000 entries. Swept after 2x completed TTL.

**Periodic cleanup:** A background task runs every 60 seconds to sweep expired entries from all three stores and clean up orphaned SSE subscriber queues.

### CompactedResult Lifecycle

When a task reaches a terminal state via `TaskStore.update_state()`:

1. `CompactedResult.from_record(record)` extracts a summary:
   - Copies `task_id`, `state`, `error`
   - Extracts first 500 chars of text from the first text/data artifact as `result_summary`
   - Freezes `output_artifacts` as an immutable tuple
   - Records `completed_at` using `time.monotonic()`
2. The compacted result is stored in the completed `OrderedDict`
3. The full `A2ATaskRecord` is removed from the active store
4. If the completed store exceeds `completed_maxsize`, the oldest entry is evicted to tombstones

This compaction reduces per-task memory from ~2-10 KB (full record with artifacts, params) to ~200 bytes (frozen dataclass with summary).

### Safe State Transitions (`_safe_transition`)

All A2A task state transitions are routed through `A2AHandler._safe_transition()`, which wraps `TaskStore.update_state()` with graceful error handling:

```python
def _safe_transition(self, task_id: str, new_state: A2ATaskState) -> bool:
    try:
        self._store.update_state(task_id, new_state)
        return True
    except KeyError:    # Task already compacted (race with cancel)
        return False
    except ValueError:  # Invalid transition (e.g., completed -> working)
        return False
```

This pattern handles three race conditions:
- **Cancel + complete race:** A task is canceled via `DELETE` while its handler is completing. The handler's `_safe_transition(COMPLETED)` returns `False` because the task was already compacted by the cancel path.
- **Double transition:** A handler accidentally tries to transition twice. The second call returns `False` because the task is no longer in the active store.
- **Post-compaction update:** A handler holds a stale reference to a record that was already compacted. `KeyError` is caught and logged at DEBUG level.

The valid transition map enforces the A2A state machine:
- `submitted` -> `working`, `canceled`, `failed`
- `working` -> `completed`, `failed`, `canceled`
- Terminal states (`completed`, `failed`, `canceled`) -> no transitions allowed

### Lease States

Model leases use hybrid eviction triggers:

```
    ACTIVE --(request_limit)--> RELEASED
    ACTIVE --(ttl_expired)----> EXPIRED
    ACTIVE --(idle_timeout)---> EXPIRED
    ACTIVE --(explicit release) -> RELEASED
```

**Eviction triggers (checked in order):**
1. Request count exhausted (`remaining_requests <= 0`)
2. Absolute TTL expired
3. Idle timeout exceeded (no activity for `idle_timeout` seconds)

**Fencing tokens:** Each lease gets a monotonically increasing fencing token. Heartbeat requests must provide the correct token, preventing zombie leases from stale clients.

---

## Swap Rate Limiter

Testing showed GPU crashes after ~60 rapid model swaps in ~7 minutes (8-9/min). BASTION uses a dynamic swap rate limiter to stay well below the crash threshold:

```
Normal  (< 4 swaps/min):   cooldown = 2.0s
Warn    (4-5 swaps/min):   cooldown = 5.0s   (half of crash rate)
Critical (>= 6 swaps/min): cooldown = 10.0s  (2-3 below crash rate)
```

The limiter uses a rolling window of swap timestamps. Level transitions are logged and audited.

---

## Concurrent Dispatch

BASTION supports concurrent inference to co-resident models (models already loaded in VRAM):

**Phase 1 (non-blocking):**
- For each model with queued work that is already resident and has no in-flight request:
  - Dispatch without waiting for completion
  - Up to `max_concurrent_dispatches` (default 3) in parallel
  - 100ms stagger between dispatches to reduce GPU power transients

**Phase 2 (blocking, serialized):**
- If a non-resident model needs dispatch (swap required):
  - Enforce cooldown
  - Reserve VRAM atomically
  - Evict models if needed
  - Dispatch and wait for completion (serialized to prevent concurrent Ollama access during transitions)

**Rules:**
- Different co-resident models -> dispatch concurrently
- Same model with in-flight request -> serialize (OLLAMA_NUM_PARALLEL=1)
- Model swap needed -> serialize (PCIe crash risk)
- Model with in-flight request -> cannot be evicted

---

## Watchdog Process Monitor

The `ProcessMonitor` runs as an async background task that periodically checks two health signals:

### Ollama Health Check
- Sends HTTP GET to Ollama's root endpoint (`/`)
- Measures response latency
- Tracks consecutive failures (non-200 responses or connection errors)

### GPU Lockup Detection
- Runs `nvidia-smi --query-gpu=temperature.gpu` as an async subprocess
- Applies a configurable timeout (default 5 seconds)
- A timeout indicates a possible GPU driver wedge (a precursor to GPU crashes)
- Tracks consecutive timeouts separately from Ollama failures

### Health State Machine

```
HEALTHY  --(N consecutive failures)--> UNHEALTHY (scheduler drained)
UNHEALTHY --(both checks pass)------> HEALTHY   (scheduler resumed)
```

- **Failure threshold:** 3 consecutive failures (configurable) before transitioning to unhealthy
- **On unhealthy:** Fires `on_unhealthy` callback (default: `scheduler.drain()`)
- **On recovery:** Fires `on_healthy` callback (default: `scheduler.resume()`)
- **Check interval:** 10 seconds (configurable)

### Systemd Integration

The watchdog module also provides `sd_notify` integration for systemd service management:
- `notify_ready()` -- sends `READY=1` after startup
- `notify_watchdog()` -- sends `WATCHDOG=1` heartbeat
- `notify_stopping()` -- sends `STOPPING=1` during shutdown
- `notify_status(msg)` -- sends `STATUS=<msg>` for systemd status display

All sd_notify functions are safe no-ops when `NOTIFY_SOCKET` is not set (i.e., not running under systemd).
