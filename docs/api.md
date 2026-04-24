# BASTION API Reference

BASTION exposes three layers of HTTP endpoints:

1. **Proxy routes** (`/api/*`) -- transparent Ollama passthrough
2. **Admin routes** (`/broker/*`) -- broker management and monitoring
3. **A2A routes** (`/a2a/*`) -- agent-to-agent protocol interface

In **single-port mode** (default), all routes are served on port 11434.
In **two-port mode**, proxy routes are on port 11434 and admin/A2A routes are on the configured `admin_port`.

Interactive API docs are available at `/broker/docs` (Swagger UI) and `/broker/redoc` (ReDoc).

---

## Proxy Routes (`/api/*`)

These routes transparently proxy requests to the Ollama backend. No authentication required.

### Scheduled Endpoints

Requests to these endpoints pass through the affinity queue and scheduler. The proxy injects `use_mmap: false` and default `num_ctx` into the options payload.

#### `POST /api/generate`

Generate text from a prompt. Ollama defaults to `stream: true`.

```bash
curl -X POST http://localhost:11434/api/generate \
  -d '{"model": "mymodel:8b", "prompt": "Hello world"}'
```

**Priority detection:**
- `X-Broker-Priority` header: `interactive`, `agent`, `pipeline`, `background`
- User-Agent containing "ollama" is auto-classified as `interactive`
- Default: `agent`

#### `POST /api/chat`

Multi-turn chat completion.

```bash
curl -X POST http://localhost:11434/api/chat \
  -d '{
    "model": "mymodel:14b",
    "messages": [{"role": "user", "content": "Hi"}]
  }'
```

#### `POST /api/embed`

Generate embeddings.

```bash
curl -X POST http://localhost:11434/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Hello world"}'
```

### Passthrough Endpoints

These endpoints forward directly to Ollama without scheduling:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tags` | GET | List available models |
| `/api/ps` | GET | List loaded models |
| `/api/show` | POST | Show model details |
| `/api/pull` | POST | Pull a model |
| `/api/delete` | DELETE | Delete a model |
| `/api/copy` | POST | Copy a model |
| `/api/create` | POST | Create a model |
| `/api/blobs` | * | Blob operations |

### Root

#### `GET /`

Returns `"Ollama is running"` -- mimics Ollama's root response for client compatibility checks.

---

## OpenAI-compatible passthrough — `/v1/*`

All `/v1/*` paths are proxied to Ollama's OpenAI-compatibility layer.
Subject to the same auth and rate-limit behavior as `/api/*`. Examples:
- `GET /v1/models` — list models
- `POST /v1/chat/completions` — chat completion
- `POST /v1/completions` — text completion
- `POST /v1/embeddings` — embedding generation

See the [Ollama OpenAI compatibility docs](https://github.com/ollama/ollama/blob/main/docs/openai.md)
for request/response shapes.

---

## Admin Routes (`/broker/*`)

Broker management and monitoring. Protected by API key auth when `auth.enabled: true` in config.

**Authentication:** When enabled, send `Authorization: Bearer <api_key>` header.

### Status & Monitoring

#### `GET /broker/status`

Full broker status including queue, GPU, loaded models, and VRAM ledger.

```bash
curl http://localhost:11434/broker/status
```

**Response:**
```json
{
  "version": "0.1.0",
  "uptime_seconds": 3600.0,
  "queue_depth": 2,
  "queue_by_model": {"model-a": 1, "model-b": 1},
  "loaded_models": [
    {"name": "model-a", "size_bytes": 5905580032, "vram_gb": 5.5}
  ],
  "gpu": {
    "temperature_c": 45,
    "vram_used_mb": 6200,
    "vram_free_mb": 26568,
    "vram_total_mb": 32768,
    "power_draw_watts": 125.0
  },
  "current_model": "model-a",
  "total_requests_served": 150,
  "total_model_swaps": 12,
  "state": "running",
  "vram_ledger": {
    "total_bytes": 34359738368,
    "safety_margin_bytes": 3435973836,
    "allocated_bytes": 5905580032,
    "reserved_bytes": 0,
    "available_bytes": 25018184500,
    "active_reservations": 0,
    "reservations": []
  }
}
```

#### `GET /broker/queue`

Detailed queue view.

```bash
curl http://localhost:11434/broker/queue
```

**Response:**
```json
{
  "models": {"model-a": 3, "model-b": 1},
  "total": 4,
  "pending_grants": 1
}
```

#### `GET /broker/health`

GPU health check. Reports safety status, GPU metrics, scheduler state, and circuit breaker state.

```bash
curl http://localhost:11434/broker/health
```

**Response:**
```json
{
  "healthy": true,
  "reason": "OK",
  "gpu": {
    "temperature_c": 42,
    "vram_used_mb": 5800,
    "vram_free_mb": 26968,
    "vram_total_mb": 32768,
    "power_draw_watts": 110.0
  },
  "scheduler_running": true,
  "circuit": "closed"
}
```

#### `GET /broker/vram`

VRAM ledger status from VRAMManager. Shows the full assume/confirm/forget ledger state.

```bash
curl http://localhost:11434/broker/vram
```

**Response:**
```json
{
  "total_bytes": 34359738368,
  "safety_margin_bytes": 3435973836,
  "allocated_bytes": 5905580032,
  "reserved_bytes": 0,
  "available_bytes": 25018184500,
  "active_reservations": 0,
  "reservations": []
}
```

#### `GET /broker/metrics`

Prometheus metrics in text exposition format. Returns 501 if `prometheus-client` is not installed.

```bash
curl http://localhost:11434/broker/metrics
```

Install with: `pip install bastion[metrics]`

**Metrics exported:**
- `bastion_requests_total` -- total requests by endpoint, status, tier
- `bastion_request_duration_seconds` -- request latency histogram
- `bastion_queue_wait_seconds` -- time in queue
- `bastion_queue_depth` -- current queue size per model
- `bastion_model_swap_total` -- model transitions
- `bastion_model_swap_duration_seconds` -- swap time histogram
- `bastion_cooldown_waits_total` -- scheduler cooldown count
- `bastion_vram_used_bytes` -- current VRAM usage
- `bastion_gpu_temperature_celsius` -- GPU temperature
- `bastion_a2a_tasks_total` -- A2A task submissions and outcomes
- `bastion_a2a_errors_total` -- A2A error counts
- `bastion_a2a_task_duration_seconds` -- A2A task duration
- `bastion_a2a_task_queue_wait_seconds` -- A2A queue wait
- `bastion_llm_time_to_first_token_seconds` -- streaming TTFT
- `bastion_a2a_tasks_active` -- active A2A task count by state
- `bastion_a2a_queue_depth` -- A2A queue depth per skill/model

#### `GET /broker/recent`

Last 50 completed requests for the dashboard trace viewer.

```bash
curl http://localhost:11434/broker/recent
```

**Response:**
```json
[
  {
    "timestamp": 1709740800.0,
    "model": "model-a",
    "endpoint": "/api/generate",
    "tier": "interactive",
    "queue_wait_s": 0.05,
    "duration_s": 2.3,
    "status_code": 200
  }
]
```

### Model Management

#### `POST /broker/preload`

Pre-load a model into VRAM. Checks VRAM budget before loading.

```bash
curl -X POST http://localhost:11434/broker/preload \
  -H "Content-Type: application/json" \
  -d '{"model": "mymodel:14b"}'
```

**Response (success):**
```json
{"status": "loaded", "model": "mymodel:14b"}
```

**Response (409 -- VRAM budget exceeded):**
```json
{"error": "Would exceed VRAM budget: 20.0GB loaded + 9.8GB requested = 29.8GB > 24.0GB limit"}
```

#### `POST /broker/unload`

Force-unload a model from VRAM. Sends `keep_alive: 0` to Ollama and polls until the model is confirmed removed.

```bash
curl -X POST http://localhost:11434/broker/unload \
  -H "Content-Type: application/json" \
  -d '{"model": "mymodel:14b"}'
```

**Response:**
```json
{"status": "unloaded", "model": "mymodel:14b"}
```

### Scheduler Control

#### `POST /broker/drain`

Enter drain mode: finish processing the current queue but reject new requests.

```bash
curl -X POST http://localhost:11434/broker/drain
```

**Response:**
```json
{"status": "draining", "queue_depth": 5}
```

#### `POST /broker/resume`

Exit drain mode and resume normal scheduling.

```bash
curl -X POST http://localhost:11434/broker/resume
```

**Response:**
```json
{"status": "running"}
```

### Watchdog

#### `GET /broker/watchdog`

Process monitor status: Ollama health and GPU responsiveness. The watchdog periodically pings Ollama and runs `nvidia-smi` to detect GPU lockups. When consecutive failures exceed the threshold, the scheduler is automatically paused.

```bash
curl http://localhost:11434/broker/watchdog
```

**Response:**
```json
{
  "ollama_state": "healthy",
  "gpu_state": "responsive",
  "ollama_latency_ms": 12.3,
  "gpu_query_latency_ms": 45.6,
  "last_check": 1709740800.0,
  "consecutive_ollama_failures": 0,
  "consecutive_gpu_timeouts": 0,
  "scheduler_paused": false
}
```

**States:**

| Field | Values | Description |
|-------|--------|-------------|
| `ollama_state` | `healthy`, `unhealthy`, `unknown` | Ollama HTTP ping result |
| `gpu_state` | `responsive`, `timeout`, `unavailable` | nvidia-smi query result |
| `scheduler_paused` | `true`, `false` | Whether the watchdog has paused scheduling |

When `consecutive_ollama_failures` or `consecutive_gpu_timeouts` reaches the failure threshold (default 3), the watchdog fires the `on_unhealthy` callback (drains the scheduler). When both recover, it fires `on_healthy` (resumes scheduling).

### Health Probes (Kubernetes-compatible)

#### `GET /broker/livez`

Liveness probe. Returns `200 ok` if the process is alive.

```bash
curl http://localhost:11434/broker/livez
```

#### `GET /broker/readyz`

Readiness probe. Returns `200 ok` if the scheduler is running, proxy is initialized, and the circuit breaker is not open. Returns `503` with a reason otherwise.

```bash
curl http://localhost:11434/broker/readyz
```

### Intent Declaration (Scheduler Optimization)

#### `POST /broker/intent`

Declare an upcoming model sequence for scheduler optimization. Accepts a profile name (from `session_profiles` in config) or an ad-hoc model sequence.

```bash
# Using a named profile
curl -X POST http://localhost:11434/broker/intent \
  -H "Content-Type: application/json" \
  -d '{
    "profile": "council_pipeline",
    "client_id": "my-pipeline",
    "estimated_requests": 20
  }'

# Ad-hoc model sequence
curl -X POST http://localhost:11434/broker/intent \
  -H "Content-Type: application/json" \
  -d '{
    "model_sequence": ["model-a", "model-b"],
    "client_id": "my-agent",
    "estimated_requests": 10
  }'
```

**Response:**
```json
{
  "intent_id": "a1b2c3d4e5f6",
  "resolved_priority": "interactive",
  "model_sequence": ["model-a", "model-b", "model-c", "model-a"],
  "estimated_requests": 20,
  "status": "registered"
}
```

#### `GET /broker/intents`

List all active intent declarations.

```bash
curl http://localhost:11434/broker/intents
```

**Response:**
```json
{
  "intents": {
    "a1b2c3d4e5f6": {
      "intent_id": "a1b2c3d4e5f6",
      "profile": "council_pipeline",
      "client_id": "my-pipeline",
      "estimated_requests": 20
    }
  },
  "total": 1
}
```

#### `POST /broker/intent/{intent_id}/complete`

Mark an intent as completed and remove it from the active set.

```bash
curl -X POST http://localhost:11434/broker/intent/a1b2c3d4e5f6/complete
```

**Response:**
```json
{"status": "completed", "intent_id": "a1b2c3d4e5f6"}
```

#### `DELETE /broker/intent/{intent_id}`

Cancel/delete an active intent.

```bash
curl -X DELETE http://localhost:11434/broker/intent/a1b2c3d4e5f6
```

**Response:**
```json
{"status": "deleted", "intent_id": "a1b2c3d4e5f6"}
```

---

## A2A Routes (`/a2a/*`)

Agent-to-Agent protocol interface. Protected by Bearer token auth when `a2a.tokens` is configured.

**Authentication:** Send `Authorization: Bearer <a2a_token>` header. When `a2a.tokens` is empty in config, access is open.

### Agent Card Discovery

#### `GET /.well-known/agent-card.json`

**No authentication required.** Tier 1 public agent card with generic info only. No model names, VRAM data, queue depth, or GPU info exposed.

```bash
curl http://localhost:11434/.well-known/agent-card.json
```

**Response:**
```json
{
  "name": "BASTION GPU Inference Broker",
  "description": "GPU inference broker with scheduling, batching, and model management",
  "version": "0.1.0",
  "serviceEndpoint": "http://localhost:11434/a2a",
  "protocolVersion": "0.1",
  "capabilities": {"streaming": true, "pushNotifications": false},
  "skills": ["text-generation", "embeddings"],
  "securitySchemes": {
    "BearerToken": {"type": "http", "scheme": "bearer"}
  },
  "security": [{"BearerToken": []}]
}
```

#### `GET /a2a/extended-card`

Tier 2 extended card. Requires A2A auth. Returns supported models, skill schemas, and availability status.

```bash
curl -H "Authorization: Bearer <token>" \
  http://localhost:11434/a2a/extended-card
```

### Task Management

#### `POST /a2a/tasks`

Create a new A2A task. Skill handlers run asynchronously.

```bash
curl -X POST http://localhost:11434/a2a/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "skill_id": "infer",
    "params": {
      "model": "mymodel:8b",
      "prompt": "What is the speed of light?",
      "stream": false
    }
  }'
```

**Response (201 Created):**
```json
{
  "id": "a1b2c3d4e5f6",
  "contextId": "f6e5d4c3b2a1",
  "status": {"state": "submitted", "message": null},
  "artifacts": [],
  "created_at": 1709740800.0,
  "updated_at": 1709740800.0
}
```

**Available skill_id values:**

| skill_id | Required params | Optional params |
|----------|----------------|-----------------|
| `infer` | `model`, `prompt` | `system_prompt`, `options`, `stream` |
| `batch_infer` | `model`, `prompts` | `system_prompt`, `options` |
| `preload` | `model` | `num_requests`, `timeout_seconds` |
| `status` | (none) | (none) |

#### `GET /a2a/tasks/{task_id}`

Get task status, artifacts, and results.

```bash
curl http://localhost:11434/a2a/tasks/a1b2c3d4e5f6
```

**Response:**
```json
{
  "id": "a1b2c3d4e5f6",
  "contextId": "f6e5d4c3b2a1",
  "status": {"state": "completed", "message": null},
  "artifacts": [
    {
      "artifact_id": "result",
      "parts": [{"kind": "text", "text": "The speed of light is approximately 299,792,458 m/s."}],
      "metadata": {"model": "mymodel:8b", "eval_count": 42}
    }
  ]
}
```

**Task states:** `submitted` -> `working` -> `completed` | `failed` | `canceled`

**CompactedResult behavior:** When a task reaches a terminal state (`completed`, `failed`, `canceled`), the full `A2ATaskRecord` is compacted into a lightweight `CompactedResult` and moved from the active store to the completed store. The compacted result retains:
- `id`, `status`, `artifacts`, `result_summary` (first 500 chars of text output)
- The `error` field (if failed)

Compacted tasks are garbage collected after `task_ttl_seconds` (default 1 hour). The response format for a compacted task differs slightly:

```json
{
  "id": "a1b2c3d4e5f6",
  "status": {"state": "completed", "message": null},
  "artifacts": [...],
  "result_summary": "The speed of light is approximately..."
}
```

Note: `contextId`, `created_at`, and `updated_at` fields are not preserved on compacted results.

#### `GET /a2a/tasks/{task_id}/stream`

SSE (Server-Sent Events) stream for real-time task status and artifact updates. Supports streaming inference tokens.

```bash
curl -N http://localhost:11434/a2a/tasks/a1b2c3d4e5f6/stream
```

**Events:**
```
data: {"statusUpdate": {"taskId": "a1b2c3d4e5f6", "status": {"state": "working"}}}

data: {"artifactUpdate": {"taskId": "a1b2c3d4e5f6", "artifact": {"parts": [{"kind": "text", "text": "The "}]}}}

data: {"statusUpdate": {"taskId": "a1b2c3d4e5f6", "status": {"state": "completed"}, "final": true}}
```

Heartbeats are sent as SSE comments (`: heartbeat`) every 15 seconds.

#### `DELETE /a2a/tasks/{task_id}`

Cancel a running task. Only works on tasks in `submitted` or `working` state.

```bash
curl -X DELETE http://localhost:11434/a2a/tasks/a1b2c3d4e5f6
```

**Response:**
```json
{"status": "canceled", "task_id": "a1b2c3d4e5f6"}
```

### Task Store Statistics

#### `GET /a2a/stats`

Returns task-store statistics:

```json
{
  "active": 3,
  "compacted": 12,
  "total_created": 150,
  "backpressure": "normal"
}
```

### Lease Management

Leases provide model reservation with hybrid eviction triggers (request count, TTL, idle timeout, fencing tokens). Leases are created automatically when using the `preload` A2A skill and can be managed via heartbeat and release endpoints.

#### `POST /a2a/leases/{lease_id}/heartbeat`

Touch a lease to keep it alive and reset the idle timeout. Requires the correct fencing token for zombie prevention -- stale heartbeats from old clients are rejected with `409`.

**Request body:**
```json
{"fencing_token": 1}
```

```bash
curl -X POST http://localhost:11434/a2a/leases/abc123/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"fencing_token": 1}'
```

**Response (200):**
```json
{
  "lease_id": "abc123",
  "remaining_requests": 45,
  "state": "active"
}
```

**Response (400 -- missing fencing token):**
```json
{"error": "Missing fencing_token"}
```

**Response (409 -- stale token or expired lease):**
```json
{"error": "Stale fencing token: got 1, expected 2"}
```

Fencing tokens are monotonically increasing integers assigned at lease creation. Each lease has a unique token, and heartbeat requests must provide the exact matching token. This prevents zombie leases from stale clients that may still be sending heartbeats after a lease has been recreated.

#### `DELETE /a2a/leases/{lease_id}`

Explicitly release a model lease. The model becomes eligible for eviction by the scheduler once all active leases are released.

```bash
curl -X DELETE http://localhost:11434/a2a/leases/abc123
```

**Response:**
```json
{"status": "released", "lease_id": "abc123"}
```

---

## Error Responses

All error responses use JSON format:

| Status | Meaning |
|--------|---------|
| 400 | Invalid request (missing fields, bad JSON) |
| 401 | Authentication required or invalid token |
| 404 | Resource not found (task, profile, lease) |
| 409 | Conflict (VRAM budget exceeded, stale fencing token) |
| 413 | Request body too large (exceeds `proxy.max_request_body_bytes`) |
| 501 | Feature not enabled (A2A disabled, metrics not installed) |
| 502 | Ollama backend unavailable |
| 503 | Service unavailable (queue full, circuit breaker open, not ready) |
| 504 | Request timed out in scheduler queue |

Circuit breaker error (A2A):
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32050,
    "message": "Backend resource unavailable",
    "data": {"reason": "LLM service temporarily unavailable", "retryAfter": 25}
  }
}
```
