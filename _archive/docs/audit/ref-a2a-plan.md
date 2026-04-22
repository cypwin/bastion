# S7 -- A2A Agent Interface: Implementation Plan

## Overview

This plan covers implementing the A2A (Agent-to-Agent) protocol interface for BASTION,
turning it from a project-internal proxy into a discoverable GPU broker that any
A2A-compliant agent can find and use. The implementation follows the A2A Protocol
Specification v0.3.0 and leverages the `a2a-sdk[http-server]>=0.3` optional dependency
already declared in `pyproject.toml:35`.

## Key Design Decision: SDK Usage Strategy

After researching the a2a-python SDK (`a2a-sdk v0.3.24`), the recommended approach is
a **hybrid strategy**: use the SDK's type definitions (`a2a.types`) for protocol
compliance (AgentCard, Task, TaskState, Message, Part, Artifact schemas), but implement
the HTTP routing and request handling directly in FastAPI rather than adopting the SDK's
`A2AStarletteApplication` + `DefaultRequestHandler` pattern.

**Rationale:**
- BASTION already has a mature FastAPI app with middleware (auth, rate limiting,
  circuit breaker). Wrapping it in the SDK's Starlette app would create an awkward
  dual-framework situation.
- The SDK's server module (`a2a.server`) expects a specific `AgentExecutor` +
  `EventQueue` pattern that doesn't map cleanly to BASTION's existing
  `AffinityQueue` + `Scheduler` + `OllamaProxy` pipeline.
- Using SDK types for data models ensures wire-format compliance without coupling
  to the SDK's server architecture.
- If the SDK is not installed (optional dep), the module gracefully falls back to
  equivalent Pydantic models defined locally.

**Import pattern:**
```python
try:
    from a2a.types import (
        AgentCard as A2AAgentCard,
        AgentSkill as A2AAgentSkill,
        AgentCapabilities as A2AAgentCapabilities,
        Task as A2ATask,
        TaskState as A2ATaskState,
        TaskStatus as A2ATaskStatus,
        Message as A2AMessage,
        TextPart as A2ATextPart,
        DataPart as A2ADataPart,
        Artifact as A2AArtifact,
    )
    A2A_SDK_AVAILABLE = True
except ImportError:
    A2A_SDK_AVAILABLE = False
```

When `A2A_SDK_AVAILABLE` is False, use locally defined Pydantic models that match
the A2A wire format. This mirrors the pattern used for `prometheus-client` in
`src/bastion/metrics.py`.

---

## File-by-File Breakdown

### (a) `src/bastion/a2a.py` (~200 LOC) -- Core Protocol Handler

This is the central module implementing A2A task lifecycle and skill routing.

#### Class: `A2AHandler`

```python
class A2AHandler:
    """A2A protocol handler for BASTION.

    Manages the A2A task lifecycle, routes incoming tasks to skill handlers,
    and integrates with the existing AffinityQueue/Scheduler pipeline.
    """

    def __init__(
        self,
        config: BrokerConfig,
        enqueue_fn: Callable[[QueuedRequest], Awaitable[asyncio.Event]],
        vram_tracker: VRAMTracker,
        scheduler: Scheduler,
    ) -> None: ...
```

**Fields:**
- `_tasks: Dict[str, A2ATaskRecord]` -- In-memory task store (task_id -> record)
- `_reservations: Dict[str, Reservation]` -- Active model reservations
- `_config: BrokerConfig` -- For reading a2a config section
- `_enqueue_fn` -- Same callback used by `OllamaProxy` to place requests in the queue
- `_vram_tracker: VRAMTracker` -- For capability negotiation (available VRAM)
- `_scheduler: Scheduler` -- For reservation priority elevation

**In-memory task store design:**

```python
@dataclass
class A2ATaskRecord:
    """Internal record for an A2A task."""
    task_id: str
    context_id: str
    state: str  # "submitted" | "working" | "completed" | "failed" | "canceled"
    skill_id: str
    input_message: dict  # The original A2A Message
    output_artifacts: List[dict]  # Completed artifacts
    error: Optional[str]
    created_at: float
    updated_at: float
    # SSE subscribers for this task
    _subscribers: List[asyncio.Queue]
```

Using a dict is appropriate since BASTION is stateless/in-memory by design (CLAUDE.md:
"all state is in-memory queues (no SQLite)"). Tasks are ephemeral -- they exist for the
duration of an inference request or batch job, then become queryable results until
the process restarts.

**Task lifecycle state machine:**

```
submitted --> working --> completed
                    \--> failed
submitted --> canceled (via DELETE)
```

State transitions emit events to SSE subscribers via `asyncio.Queue.put_nowait()`.

**Skill routing:**

```python
_SKILL_HANDLERS: Dict[str, Callable] = {
    "infer": self._handle_infer,
    "batch_infer": self._handle_batch_infer,
    "preload": self._handle_preload,
    "status": self._handle_status,
}
```

The `create_task()` method parses the incoming A2A Message, extracts `skill_id`
from structured data or metadata, looks up the handler, and dispatches. Unknown
skill IDs return a task in `failed` state with an appropriate error.

**Key methods:**

```python
async def create_task(self, message: dict) -> dict:
    """Create a new A2A task from an incoming message.

    1. Generate task_id (uuid4 hex, 12 chars -- matches QueuedRequest pattern)
    2. Extract skill_id from message parts (DataPart with skill routing info)
    3. Validate skill_id against _SKILL_HANDLERS
    4. Create A2ATaskRecord in "submitted" state
    5. Launch skill handler as asyncio.Task (fire-and-forget)
    6. Return A2A Task object with id, status, contextId
    """

async def get_task(self, task_id: str) -> Optional[dict]:
    """Return current task state, artifacts, and status."""

async def cancel_task(self, task_id: str) -> bool:
    """Cancel a task if it's still in submitted/working state."""

async def subscribe_task(self, task_id: str) -> AsyncGenerator[dict, None]:
    """SSE event generator for task status/artifact updates."""

async def build_agent_card(self) -> dict:
    """Build dynamic agent card from runtime state."""
```

**Integration with AffinityQueue:**

A2A tasks become `QueuedRequest` entries through the existing `enqueue_fn`:

```python
queued = QueuedRequest(
    model=model_name,
    endpoint="/api/generate",  # or /api/chat
    body=json.dumps(ollama_payload).encode(),
    priority=base_priority,
    base_priority=base_priority,
    tier=PriorityTier.AGENT,  # A2A clients default to AGENT tier
    client_info=f"a2a:{task_id}",
)
grant_event = await self._enqueue_fn(queued)
await asyncio.wait_for(grant_event.wait(), timeout=queue_timeout)
```

This reuses the entire scheduler pipeline -- the A2A handler is just another
client of the same queue, identical to how `OllamaProxy._handle_scheduled()` works.

**Error handling strategy:**
- Invalid JSON / missing fields: return 400 with A2A-format error
- Unknown skill_id: create task in `failed` state immediately
- Queue full: create task in `failed` state with "queue_full" error
- Queue timeout: transition task to `failed` with "timeout" error
- Ollama errors: transition task to `failed` with error details
- All errors are also pushed to SSE subscribers

---

### (b) `src/bastion/models.py` additions -- New Pydantic Models

Add these models to the existing `models.py` file, in a new section after the
S6 Intent models (after line ~291).

```python
# ---------------------------------------------------------------------------
# S7: A2A models
# ---------------------------------------------------------------------------

class A2AConfig(BaseModel):
    """A2A interface configuration."""
    enabled: bool = False
    tokens: List[str] = Field(default_factory=list)
    reservation_max_requests: int = 100
    reservation_timeout_seconds: float = 600.0  # 10 minutes
    task_ttl_seconds: float = 3600.0  # Completed tasks kept for 1 hour
    max_batch_size: int = 50


class A2ATaskState(str, Enum):
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
    input_params: Dict[str, Any] = Field(default_factory=dict)
    output_artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    model_config = {"arbitrary_types_allowed": True}


class BatchInferRequest(BaseModel):
    """Parameters for the batch_infer skill."""
    model: str
    prompts: List[str]
    system_prompt: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)
    priority: PriorityTier = PriorityTier.AGENT


class BatchInferResult(BaseModel):
    """Result of a batch_infer task."""
    results: List[Dict[str, Any]]  # Per-prompt results (index-aligned)
    total: int
    succeeded: int
    failed: int


class ReservationRequest(BaseModel):
    """Parameters for the preload/reservation skill."""
    model: str
    num_requests: int = 10
    timeout_seconds: Optional[float] = None  # Falls back to config default
    priority: PriorityTier = PriorityTier.INTERACTIVE


class Reservation(BaseModel):
    """Active model reservation."""
    reservation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str
    remaining_requests: int
    priority: PriorityTier = PriorityTier.INTERACTIVE
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0  # Set from config
```

**BrokerConfig addition:**

Add `a2a: A2AConfig = Field(default_factory=A2AConfig)` to the `BrokerConfig` class
(after `circuit_breaker` field, around line 131).

---

### (c) `src/bastion/server.py` modifications -- Route Mounting

#### New module-level state

```python
_a2a_handler: A2AHandler | None = None
```

#### Lifespan changes

In the `lifespan()` function, after creating `_scheduler`, initialize the A2A handler:

```python
if config.a2a.enabled:
    _a2a_handler = A2AHandler(
        config=config,
        enqueue_fn=_enqueue_request,
        vram_tracker=_vram_tracker,
        scheduler=_scheduler,
    )
    logger.info("A2A interface enabled")
```

#### New routes in `create_app()`

Add after the existing agent card route (line ~461), before the catch-all proxy routes:

```python
# ── A2A Interface Routes ─────────────────────────────────────────

@app.post("/a2a/tasks")
async def a2a_create_task(request: Request):
    """Create a new A2A task (SendMessage equivalent)."""
    if not _a2a_handler:
        return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
    body = await request.json()
    result = await _a2a_handler.create_task(body)
    return JSONResponse(result, status_code=201)

@app.get("/a2a/tasks/{task_id}")
async def a2a_get_task(task_id: str):
    """Get task status and results."""
    if not _a2a_handler:
        return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
    result = await _a2a_handler.get_task(task_id)
    if result is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return result

@app.get("/a2a/tasks/{task_id}/stream")
async def a2a_stream_task(task_id: str):
    """SSE stream for task status/artifact updates."""
    if not _a2a_handler:
        return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
    generator = _a2a_handler.subscribe_task(task_id)
    if generator is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return StreamingResponse(
        _sse_wrapper(generator),
        media_type="text/event-stream",
    )

@app.delete("/a2a/tasks/{task_id}")
async def a2a_cancel_task(task_id: str):
    """Cancel a running task."""
    if not _a2a_handler:
        return JSONResponse({"error": "A2A interface not enabled"}, status_code=501)
    success = await _a2a_handler.cancel_task(task_id)
    if not success:
        return JSONResponse({"error": "Task not found or not cancelable"}, status_code=404)
    return {"status": "canceled", "task_id": task_id}
```

**SSE wrapper helper** (in server.py):

```python
async def _sse_wrapper(generator: AsyncGenerator[dict, None]) -> AsyncGenerator[bytes, None]:
    """Wrap A2A events as SSE-formatted bytes."""
    async for event in generator:
        data = json.dumps(event)
        yield f"data: {data}\n\n".encode()
```

#### Auth for A2A routes

The existing `AuthMiddleware` protects `/broker/*` routes. For A2A, we extend the
`_PROTECTED_PREFIXES` in `auth.py` to include `/a2a/` when A2A auth is enabled.

**Recommended approach:** Rather than modifying `auth.py`'s static prefix list, add
A2A auth as a **separate check within the A2A routes themselves**, since A2A tokens
are configured in a different config section (`a2a.tokens` vs `auth.api_keys`).

```python
# Inside each /a2a/* route handler (or as a dependency):
def _check_a2a_auth(request: Request) -> Optional[JSONResponse]:
    """Validate A2A bearer token. Returns error response or None if valid."""
    if not _config.a2a.tokens:
        return None  # No tokens configured = open access
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Missing or invalid Authorization header"}, status_code=401)
    token = auth_header[7:]
    if token not in _config.a2a.tokens:
        return JSONResponse({"error": "Invalid A2A token"}, status_code=401)
    return None
```

This function is called at the top of each `/a2a/*` handler. The agent card
endpoint (`/.well-known/agent-card.json`) remains public -- discovery must be open.

**Why not reuse AuthMiddleware:** The A2A tokens are separate from admin API keys.
An agent might have an A2A token but not an admin API key. Keeping them separate
avoids privilege conflation.

#### Dynamic agent card evolution

Replace the static dict at `server.py:430-461` with a call to
`_a2a_handler.build_agent_card()` when A2A is enabled, falling back to the current
static card when disabled:

```python
@app.get("/.well-known/agent-card.json")
async def agent_card():
    """A2A Agent Card -- describes BASTION's capabilities.

    When A2A is enabled, returns dynamic card with runtime state.
    Otherwise returns static capabilities card.
    """
    if _a2a_handler:
        return await _a2a_handler.build_agent_card()
    # Static fallback (existing code)
    return { ... }
```

The dynamic card includes:
- Current VRAM availability (`vram_free_gb` from VRAMTracker)
- Currently loaded models (from VRAMTracker)
- Queue depth (from AffinityQueue)
- Supported models (from config)
- Proper A2A protocol fields: `securitySchemes`, `capabilities.streaming`, skill
  definitions with `inputSchema`/`outputSchema`

---

### (d) Skill Handlers (methods on `A2AHandler`)

#### `infer` skill (~50 LOC)

Single prompt inference through the scheduler pipeline.

```python
async def _handle_infer(self, record: A2ATaskRecord) -> None:
    """Handle the 'infer' skill: single prompt -> result.

    Flow:
    1. Extract model, prompt, system_prompt, options from input_params
    2. Build Ollama-format payload (same as what /api/generate expects)
    3. Create QueuedRequest and enqueue via _enqueue_fn
    4. Await grant event (scheduler loads model)
    5. Forward to Ollama via httpx (reuse proxy's HTTP client pattern)
    6. Collect response (streaming or non-streaming)
    7. Store result as artifact on the task record
    8. Transition task to "completed"
    """
```

**Input params (from A2A message DataPart):**
```json
{
    "skill_id": "infer",
    "model": "qwen3:14b",
    "prompt": "Explain quantum computing",
    "system_prompt": "You are a helpful assistant",
    "options": {"temperature": 0.7},
    "stream": false
}
```

**Output artifact:**
```json
{
    "artifact_id": "result-0",
    "parts": [{"kind": "text", "text": "Quantum computing is..."}],
    "metadata": {"model": "qwen3:14b", "eval_count": 142, "total_duration": 3500000000}
}
```

For streaming infer, the handler pushes `TaskArtifactUpdateEvent` SSE events as
tokens arrive from Ollama's NDJSON stream (see section (e) below).

#### `batch_infer` skill (~100 LOC)

Batch inference with single-model-load guarantee.

```python
async def _handle_batch_infer(self, record: A2ATaskRecord) -> None:
    """Handle the 'batch_infer' skill: N prompts -> N results.

    Flow:
    1. Parse BatchInferRequest from input_params
    2. Validate batch size <= config.a2a.max_batch_size
    3. Create first QueuedRequest to ensure model is loaded
    4. After grant, process all prompts sequentially using direct Ollama calls
       (model is already loaded, no need to re-enqueue each prompt)
    5. Collect results indexed by position, with per-prompt status
    6. Push partial results as artifact updates (SSE)
    7. Transition task to "completed" with BatchInferResult
    """
```

**Preventing model interleaving:**

The key challenge is ensuring no other model gets loaded between batch prompts.
Two approaches were considered:

| Approach | Mechanism | Pros | Cons |
|---|---|---|---|
| **Queue manipulation** | Enqueue all N as separate requests with high affinity | Uses existing queue; no scheduler changes | N requests clutter the queue; other models could interleave if affinity bonus isn't high enough |
| **Scheduler reservation flag** (recommended) | Enqueue first prompt normally; after grant, hold a "batch lock" that makes the scheduler skip model swaps | Guarantees no interleaving; clean separation | Requires scheduler awareness of batch mode |

**Recommended approach: Direct Ollama calls after initial grant.**

After the first prompt is granted by the scheduler (model confirmed loaded), the
batch handler bypasses the queue for subsequent prompts and calls Ollama directly
via httpx. This works because:
1. The model is already loaded (scheduler just confirmed it).
2. BASTION controls the scheduler -- no other process can trigger an unload.
3. As long as the batch handler holds a "reservation" on the model, the scheduler
   defers eviction (see reservation mechanism below).

The handler creates a `Reservation` for the batch duration:

```python
# After first prompt granted:
reservation = Reservation(
    model=model_name,
    remaining_requests=len(prompts) - 1,
    priority=PriorityTier.INTERACTIVE,
    expires_at=time.time() + self._config.a2a.reservation_timeout_seconds,
)
self._reservations[reservation.reservation_id] = reservation
```

The scheduler checks `_reservations` before evicting a model (see section (d)
reservation skill for details).

**Partial results format:**

```json
{
    "results": [
        {"index": 0, "status": "completed", "response": "..."},
        {"index": 1, "status": "completed", "response": "..."},
        {"index": 2, "status": "failed", "error": "context length exceeded"},
        {"index": 3, "status": "completed", "response": "..."}
    ],
    "total": 4,
    "succeeded": 3,
    "failed": 1
}
```

#### `preload` / reservation skill (~80 LOC)

```python
async def _handle_preload(self, record: A2ATaskRecord) -> None:
    """Handle the 'preload' skill: reserve model for N requests.

    Flow:
    1. Parse ReservationRequest from input_params
    2. Validate model exists in config
    3. Check VRAM availability via VRAMTracker
    4. Trigger model load (enqueue a minimal generate request, like /broker/preload)
    5. Create Reservation with request count + timeout
    6. Store reservation and return reservation_id in task artifact
    7. Transition task to "completed"
    """
```

**Reservation interaction with scheduler eviction:**

The `Scheduler._process_tick()` method needs a small addition to check reservations
before unloading a model:

```python
# In Scheduler._process_tick(), before _unload_model():
if self._has_reservation(model):
    logger.info("Deferring eviction of '%s' -- active reservation", model)
    # Try loading the new model alongside (if VRAM allows)
    # Otherwise skip this tick
    return False
```

The `_has_reservation()` check is implemented via a callback from A2AHandler:

```python
# A2AHandler provides:
def has_active_reservation(self, model: str) -> bool:
    """Check if any active reservation exists for a model."""
    now = time.time()
    for r in self._reservations.values():
        if r.model == model and r.remaining_requests > 0 and now < r.expires_at:
            return True
    return False
```

The scheduler receives this callback during initialization (dependency injection).

**Reservation consumption:** When a request for a reserved model is dispatched,
the A2A handler decrements `remaining_requests`. When it hits 0, the reservation
is released and the scheduler can evict normally.

**Reservation cleanup:** A background asyncio task periodically scans for expired
reservations (every 30 seconds) and removes them.

#### `status` skill (~20 LOC)

```python
async def _handle_status(self, record: A2ATaskRecord) -> None:
    """Handle the 'status' skill: return broker status.

    Essentially wraps /broker/status as an A2A task response.
    Returns queue depth, loaded models, GPU health as a DataPart.
    """
```

This is the simplest skill -- just queries the existing broker state and returns
it as a structured artifact.

---

### (e) SSE Streaming Bridge (~80 LOC)

The bridge converts Ollama's NDJSON streaming output to A2A SSE events.

**Architecture:**

```
Ollama NDJSON stream        A2A SSE stream
(application/x-ndjson)      (text/event-stream)
                    \       /
                     Bridge
                    /       \
    OllamaProxy             A2AHandler
    (existing)              (new)
```

**Event format:**

Each SSE event is a JSON object conforming to the A2A streaming spec:

```
data: {"statusUpdate": {"taskId": "abc123", "status": {"state": "working"}, "timestamp": "2026-03-03T12:00:00Z"}}

data: {"artifactUpdate": {"taskId": "abc123", "artifact": {"artifact_id": "token-0", "parts": [{"kind": "text", "text": "Quantum"}]}, "timestamp": "2026-03-03T12:00:01Z"}}

data: {"artifactUpdate": {"taskId": "abc123", "artifact": {"artifact_id": "token-1", "parts": [{"kind": "text", "text": " computing"}]}, "timestamp": "2026-03-03T12:00:01Z"}}

data: {"statusUpdate": {"taskId": "abc123", "status": {"state": "completed"}, "timestamp": "2026-03-03T12:00:05Z"}}
```

**Bridge implementation (in `a2a.py`):**

```python
async def _stream_ollama_to_sse(
    self,
    record: A2ATaskRecord,
    ollama_url: str,
    payload: dict,
    http_client: httpx.AsyncClient,
) -> None:
    """Bridge Ollama NDJSON streaming to A2A SSE events.

    1. Open httpx streaming connection to Ollama
    2. For each NDJSON line:
       a. Parse JSON chunk
       b. Extract token text from chunk["response"] (generate) or
          chunk["message"]["content"] (chat)
       c. Create TaskArtifactUpdateEvent with TextPart
       d. Push to all SSE subscribers via asyncio.Queue
    3. On stream end (chunk["done"] == true):
       a. Create final artifact with full accumulated text
       b. Push TaskStatusUpdateEvent with state="completed"
    """
```

**Connection lifecycle:**
- SSE connections are kept alive with periodic heartbeat comments (`:\n\n`)
  every 15 seconds to prevent proxy/load-balancer timeouts.
- Client disconnection is detected via `asyncio.CancelledError` when the
  `StreamingResponse` generator is abandoned.
- Subscriber queues are bounded (maxsize=100) to prevent memory leaks if a
  slow consumer falls behind. Overflow events are dropped with a warning.

---

### (f) Auth for A2A Routes (~40 LOC)

Already covered in section (c). Summary:

- A2A tokens are configured under `a2a.tokens` in `broker.yaml` (separate from
  `auth.api_keys` which protect `/broker/*`)
- Token validation is a helper function called at the top of each `/a2a/*` handler
- Agent card (`/.well-known/agent-card.json`) remains public
- When no tokens are configured, A2A routes are open (same pattern as auth.py)
- The agent card advertises the security scheme:

```json
{
    "securitySchemes": {
        "a2aBearer": {
            "type": "http",
            "scheme": "bearer"
        }
    },
    "security": [{"a2aBearer": []}]
}
```

If no tokens are configured, `securitySchemes` and `security` are omitted from the
card (indicating open access).

---

### (g) `config/broker.yaml` additions

Add after the `circuit_breaker` section (line ~158), before `request_overrides`:

```yaml
# -- A2A Agent Interface --------------------------------------------------
# When enabled, BASTION exposes A2A protocol endpoints at /a2a/*
# for agent-to-agent task submission, batch inference, and model reservation.
a2a:
  enabled: false
  tokens: []               # Bearer tokens for /a2a/* routes. Empty = open access.
  reservation_max_requests: 100   # Max requests per model reservation
  reservation_timeout_seconds: 600  # 10 minutes -- safety net for abandoned reservations
  task_ttl_seconds: 3600   # Completed tasks kept for 1 hour before cleanup
  max_batch_size: 50       # Max prompts in a single batch_infer task
```

---

### (h) `tests/test_a2a.py` -- Testing Strategy

#### What to Mock

- **Ollama backend**: Use existing `MockOllamaResponses` from `conftest.py` pattern
- **VRAMTracker**: Mock `get_loaded_models()`, `can_load_model()`, `get_loaded_vram_gb()`
- **Scheduler grant**: Provide a fake `enqueue_fn` that returns immediately-set Events
  (same pattern as `test_proxy.py:fake_enqueue`)
- **GPU status**: Use existing `mock_gpu_safe` fixture

#### Test Configuration

```python
@pytest.fixture
def a2a_config() -> BrokerConfig:
    """BrokerConfig with A2A enabled."""
    return BrokerConfig(
        a2a=A2AConfig(
            enabled=True,
            tokens=["test-a2a-token"],
            max_batch_size=5,
            reservation_timeout_seconds=10.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            max_queue_size=16,
        ),
    )
```

#### Test Categories and Key Scenarios

**1. A2AHandler unit tests (core lifecycle):**

```python
class TestA2ATaskLifecycle:
    async def test_create_task_returns_submitted_state(self): ...
    async def test_get_task_returns_current_state(self): ...
    async def test_get_nonexistent_task_returns_none(self): ...
    async def test_cancel_submitted_task(self): ...
    async def test_cancel_completed_task_fails(self): ...
    async def test_unknown_skill_creates_failed_task(self): ...
```

**2. Infer skill tests:**

```python
class TestInferSkill:
    async def test_single_prompt_completes(self): ...
    async def test_infer_with_system_prompt(self): ...
    async def test_infer_queue_full_fails_task(self): ...
    async def test_infer_timeout_fails_task(self): ...
    async def test_infer_ollama_error_fails_task(self): ...
```

**3. Batch infer tests:**

```python
class TestBatchInferSkill:
    async def test_batch_all_succeed(self): ...
    async def test_batch_partial_failure(self): ...
    async def test_batch_exceeds_max_size_rejected(self): ...
    async def test_batch_empty_prompts_rejected(self): ...
    async def test_batch_model_stays_loaded(self): ...
```

**4. Reservation tests:**

```python
class TestReservationSkill:
    async def test_reservation_created(self): ...
    async def test_reservation_prevents_eviction(self): ...
    async def test_reservation_expires_after_timeout(self): ...
    async def test_reservation_consumed_on_requests(self): ...
    async def test_max_requests_limit(self): ...
```

**5. Agent card tests:**

```python
class TestAgentCard:
    def test_static_card_when_a2a_disabled(self): ...
    async def test_dynamic_card_includes_vram(self): ...
    async def test_dynamic_card_includes_loaded_models(self): ...
    async def test_dynamic_card_includes_queue_depth(self): ...
    async def test_card_includes_security_when_tokens_configured(self): ...
    async def test_card_omits_security_when_no_tokens(self): ...
```

**6. Auth tests:**

```python
class TestA2AAuth:
    def test_no_token_returns_401(self): ...
    def test_invalid_token_returns_401(self): ...
    def test_valid_token_passes(self): ...
    def test_agent_card_public_without_token(self): ...
    def test_no_tokens_configured_open_access(self): ...
```

**7. SSE streaming tests:**

```python
class TestA2AStreaming:
    async def test_subscribe_receives_status_updates(self): ...
    async def test_subscribe_receives_artifact_updates(self): ...
    async def test_subscribe_nonexistent_task_404(self): ...
    async def test_sse_format_correct(self): ...
```

**Testing SSE streaming:**

Use `httpx.AsyncClient` with `stream=True` to consume SSE events:

```python
async def test_sse_stream(self):
    async with httpx.AsyncClient(app=app) as client:
        async with client.stream("GET", f"/a2a/tasks/{task_id}/stream") as resp:
            lines = []
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    lines.append(json.loads(line[6:]))
                if len(lines) >= 3:
                    break
    assert lines[0]["statusUpdate"]["status"]["state"] == "working"
    assert "artifactUpdate" in lines[1]
    assert lines[-1]["statusUpdate"]["status"]["state"] == "completed"
```

Alternatively, use FastAPI's `TestClient` which supports streaming responses.

---

## Key Design Decisions Summary

### 1. Task ID generation
Use `uuid.uuid4().hex[:12]` -- consistent with `QueuedRequest.id` and
`IntentDeclaration.intent_id` patterns already in the codebase.

### 2. `batch_infer` model interleaving prevention
After the initial scheduler grant confirms the model is loaded, subsequent batch
prompts are sent directly to Ollama (bypassing the queue) while a `Reservation`
prevents the scheduler from evicting the model. This is simpler and more reliable
than trying to manipulate queue ordering.

### 3. Reservation interaction with scheduler eviction
The scheduler checks `A2AHandler.has_active_reservation(model)` via a callback
before evicting. This is a lightweight check (dict lookup) that doesn't require
the scheduler to know about A2A internals.

### 4. Thread safety of in-memory task store
BASTION runs on asyncio (single-threaded event loop). The task store dict and
reservation dict are accessed only from coroutines, so no locking is needed.
The only thread-safety concern is the `AffinityQueue` which already uses
`threading.Lock` (accessed from both the async loop and potential thread-pool
executors). The A2A handler only interacts with the queue through `_enqueue_fn`,
which is safe.

### 5. SDK type usage
Use SDK types for wire-format serialization (ensuring protocol compliance) but
not for server architecture (handler, executor patterns). If the SDK is not
installed, fall back to local Pydantic models. The agent card is always built
manually (not via SDK's `AgentCard` class) to include BASTION-specific runtime
fields.

### 6. Existing tests unaffected
All changes are additive:
- New models added to `models.py` don't affect existing model parsing
- New routes in `server.py` are added before the catch-all proxy route
- New config field (`a2a`) has `enabled: false` default -- zero impact on existing behavior
- A2A handler is only initialized when `config.a2a.enabled` is True
- No modifications to existing queue, scheduler, or proxy logic (reservation
  check is the only scheduler touch point, and it's a no-op when A2A is disabled)

---

## Implementation Order

1. **Models first** (`models.py`): Add `A2AConfig`, `A2ATaskState`, `A2ATaskRecord`,
   `BatchInferRequest`, `BatchInferResult`, `ReservationRequest`, `Reservation`.
   Add `a2a` field to `BrokerConfig`.

2. **Config** (`broker.yaml`): Add the `a2a:` section.

3. **Core handler** (`a2a.py`): Implement `A2AHandler` with task store, lifecycle,
   and skill routing. Start with `infer` and `status` skills.

4. **Server integration** (`server.py`): Mount routes, lifespan init, dynamic
   agent card, A2A auth check.

5. **Batch infer** (`a2a.py`): Add `_handle_batch_infer` with reservation logic.

6. **Reservation** (`a2a.py` + `scheduler.py`): Add `_handle_preload`, reservation
   store, scheduler eviction check callback.

7. **SSE streaming** (`a2a.py`): Add `_stream_ollama_to_sse` bridge, subscriber
   management, SSE wrapper.

8. **Tests** (`test_a2a.py`): Full test suite covering all categories above.

---

## File Change Summary

| File | Action | Est. LOC Changed |
|------|--------|-----------------|
| `src/bastion/models.py` | Add A2A models and config | +80 |
| `src/bastion/a2a.py` | New file -- core handler | +350 |
| `src/bastion/server.py` | Add routes, lifespan init, auth helper | +80 |
| `src/bastion/scheduler.py` | Add reservation eviction check | +15 |
| `config/broker.yaml` | Add a2a section | +10 |
| `tests/test_a2a.py` | New file -- full test suite | +300 |
| **Total** | | **~835** |

---

## Dependencies

- `a2a-sdk[http-server]>=0.3` -- already declared as optional in `pyproject.toml:35`
- No new required dependencies
- All existing tests must continue to pass (A2A is disabled by default)
