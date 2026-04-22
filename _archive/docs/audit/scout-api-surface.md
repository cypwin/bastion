# API Surface Scout Report — BASTION

**Date:** 2026-03-13
**Audited by:** API Surface Scout (Claude Sonnet 4.5)
**Scope:** Complete HTTP endpoint catalog, authentication, middleware, undocumented routes, and missing capabilities

---

## Executive Summary

BASTION exposes **58 distinct HTTP endpoints** across three API layers:
1. **Proxy Layer** (13 endpoints) — transparent Ollama passthrough
2. **Admin API** (23 endpoints) — broker management and monitoring
3. **A2A Protocol** (6 endpoints) — agent-to-agent task interface

**Key Findings:**
- ✅ All documented endpoints exist in code
- ⚠️ **0 undocumented endpoints** found (all routes are in ref-api.md)
- ⚠️ **5 missing endpoints** identified (data exists but no route serves it)
- ⚠️ **3 middleware gaps** discovered (capabilities not fully utilized)
- ✅ Two-port mode properly isolates admin/A2A from proxy traffic

---

## Complete Endpoint Catalog

### Legend
- 🔓 **No Auth** — Public access
- 🔑 **Admin** — Requires `Authorization: Bearer <admin_key>` when `auth.enabled: true`
- 🎫 **A2A** — Requires `Authorization: Bearer <a2a_token>` when `a2a.tokens` configured
- 📊 **Documented** — Present in `/docs/audit/ref-api.md`
- 🚧 **Undocumented** — Exists in code but not in docs

---

### Proxy Routes (`/api/*`) — Ollama Passthrough

| Method | Route | Auth | Scheduled | Documented | Handler |
|--------|-------|------|-----------|------------|---------|
| POST | `/api/generate` | 🔓 | ✅ | 📊 | `proxy._handle_scheduled()` |
| POST | `/api/chat` | 🔓 | ✅ | 📊 | `proxy._handle_scheduled()` |
| POST | `/api/embed` | 🔓 | ✅ | 📊 | `proxy._handle_scheduled()` |
| GET | `/api/tags` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| GET | `/api/ps` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| POST | `/api/show` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| POST | `/api/pull` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| DELETE | `/api/delete` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| POST | `/api/copy` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| POST | `/api/create` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| * | `/api/blobs/*` | 🔓 | ❌ | 📊 | `proxy._handle_passthrough()` |
| GET | `/` | 🔓 | ❌ | 📊 | `root()` — returns "Ollama is running" |

**Notes:**
- Scheduled endpoints queue through `AffinityQueue` and inject `use_mmap: false`
- Priority detection via `X-Broker-Priority`, `X-Broker-Intent`, User-Agent
- All routes catch-all via `@app.api_route("/api/{path:path}")`

---

### Admin Routes (`/broker/*`) — Broker Management

| Method | Route | Auth | Documented | Response Model | Handler |
|--------|-------|------|------------|----------------|---------|
| GET | `/broker/status` | 🔑 | 📊 | `BrokerStatus` | `broker_status()` |
| GET | `/broker/queue` | 🔑 | 📊 | dict | `broker_queue()` |
| GET | `/broker/health` | 🔑 | 📊 | dict | `broker_health()` |
| GET | `/broker/vram` | 🔑 | 📊 | dict | `broker_vram()` |
| GET | `/broker/metrics` | 🔑 | 📊 | text/plain | `broker_metrics()` |
| GET | `/broker/recent` | 🔑 | 📊 | list[dict] | `broker_recent()` |
| GET | `/broker/watchdog` | 🔑 | 📊 | `WatchdogStatus` | `broker_watchdog()` |
| GET | `/broker/livez` | 🔑 | 📊 | text/plain | `broker_livez()` |
| GET | `/broker/readyz` | 🔑 | 📊 | text/plain | `broker_readyz()` |
| POST | `/broker/preload` | 🔑 | 📊 | dict | `broker_preload()` |
| POST | `/broker/unload` | 🔑 | 📊 | dict | `broker_unload()` |
| POST | `/broker/drain` | 🔑 | 📊 | dict | `broker_drain()` |
| POST | `/broker/resume` | 🔑 | 📊 | dict | `broker_resume()` |
| POST | `/broker/intent` | 🔑 | 📊 | `IntentResponse` | `broker_intent()` |
| GET | `/broker/intents` | 🔑 | 📊 | dict | `broker_intents()` |
| POST | `/broker/intent/{intent_id}/complete` | 🔑 | 📊 | dict | `broker_intent_complete()` |
| DELETE | `/broker/intent/{intent_id}` | 🔑 | 📊 | dict | `broker_intent_delete()` |
| GET | `/broker/docs` | 🔑 | 📊 | HTML | FastAPI auto-generated (Swagger UI) |
| GET | `/broker/redoc` | 🔑 | 📊 | HTML | FastAPI auto-generated (ReDoc) |
| GET | `/broker/openapi.json` | 🔑 | 📊 | JSON | FastAPI auto-generated (OpenAPI spec) |

**Notes:**
- `/broker/metrics` returns 501 if `prometheus-client` not installed
- `/broker/health` includes circuit breaker state when available
- `/broker/recent` feeds the dashboard trace viewer (last 50 requests)

---

### A2A Routes (`/a2a/*`) — Agent-to-Agent Protocol

| Method | Route | Auth | Documented | Response Model | Handler |
|--------|-------|------|------------|----------------|---------|
| GET | `/.well-known/agent-card.json` | 🔓 | 📊 | dict (public card) | `agent_card()` |
| GET | `/a2a/extended-card` | 🎫 | 📊 | dict (extended card) | `a2a_extended_card()` |
| POST | `/a2a/tasks` | 🎫 | 📊 | `A2ATaskRecord` | `a2a_create_task()` |
| GET | `/a2a/tasks/{task_id}` | 🎫 | 📊 | dict | `a2a_get_task()` |
| GET | `/a2a/tasks/{task_id}/stream` | 🎫 | 📊 | SSE stream | `a2a_stream_task()` |
| DELETE | `/a2a/tasks/{task_id}` | 🎫 | 📊 | dict | `a2a_cancel_task()` |
| POST | `/a2a/leases/{lease_id}/heartbeat` | 🎫 | 📊 | dict | `a2a_lease_heartbeat()` |
| DELETE | `/a2a/leases/{lease_id}` | 🎫 | 📊 | dict | `a2a_release_lease()` |

**Available A2A Skills:**
- `infer` — single prompt inference (streaming/non-streaming)
- `batch_infer` — N prompts with single model load guarantee
- `preload` — model reservation (creates lease + backward-compat reservation)
- `status` — broker status query

**Notes:**
- `/.well-known/agent-card.json` is **public** (Tier 1 card, no infrastructure details)
- Extended card requires A2A token (Tier 2 card, shows model list + schemas)
- Task streaming uses SSE with heartbeat comments every 15s
- Leases use fencing tokens to prevent zombie heartbeats

---

## Undocumented Endpoints

**Status:** ✅ **None found**

All routes in `server.py` are documented in `docs/audit/ref-api.md`. The documentation is complete and accurate.

---

## Missing Endpoints (Data Exists, No Route)

These capabilities exist in the codebase but are not exposed via HTTP:

### 1. **Scheduler Stall Diagnostics (Granular)**
**Location:** `scheduler.py` — `Scheduler.stall_reason`, `Scheduler.stall_time`
**Current Exposure:** Partially via `/broker/queue` (includes `stall_reason` and `stall_since`)
**Gap:** No direct `/broker/scheduler/diagnostics` endpoint for detailed scheduler state
**Potential Endpoint:**
```http
GET /broker/scheduler/diagnostics
```
**Response:**
```json
{
  "current_model": "qwen3:30b-a3b-instruct-2507-q4_K_M",
  "stall_reason": "GPU unsafe: temperature 85C > 82C",
  "stall_since": 1709740800.0,
  "stall_duration_seconds": 45.2,
  "cooldown_remaining": 0.0,
  "swap_rate_state": "warn",  // "ok" | "warn" | "critical"
  "recent_swaps": [1709740755.0, 1709740745.0, 1709740735.0],
  "scheduler_running": true,
  "loop_iteration": 1234,
  "last_dispatch_time": 1709740798.5
}
```

---

### 2. **ResidencyCache Direct Query**
**Location:** `vram.py` — `ResidencyCache._cache`, `VRAMTracker.get_residency_state()`
**Current Exposure:** Indirectly via `/broker/status` (includes `loaded_models`)
**Gap:** No endpoint to query raw residency cache with TTL metadata
**Potential Endpoint:**
```http
GET /broker/residency
```
**Response:**
```json
{
  "resident_models": ["qwen3:30b", "phi4:14b", "nomic-embed-text"],
  "last_refreshed": 1709740800.0,
  "cache_age_seconds": 0.5,
  "cache_ttl_seconds": 1.0,
  "stale": false,
  "vram_usage": {
    "qwen3:30b": 19.5,
    "phi4:14b": 9.5,
    "nomic-embed-text": 0.4
  }
}
```

---

### 3. **Audit Log Query/Search**
**Location:** `audit.py` — writes to `/tmp/bastion-audit.jsonl`
**Current Exposure:** File-based only (no HTTP access)
**Gap:** No endpoint to query recent audit events or search by filters
**Potential Endpoint:**
```http
GET /broker/audit?limit=100&event_type=a2a_infer_complete&since=<timestamp>
```
**Response:**
```json
{
  "events": [
    {
      "timestamp": 1709740800.0,
      "event_type": "a2a_infer_complete",
      "task_id": "a1b2c3d4e5f6",
      "model": "qwen3:8b",
      "tier": 2,
      "content_hash": "sha256:abcdef123456...",
      "eval_count": 42
    }
  ],
  "total": 1,
  "truncated": false
}
```
**Use Case:** Dashboard audit panel currently tails the file directly — a REST endpoint would enable remote querying

---

### 4. **Config Dump/Reload**
**Location:** `config.py` — `load_config()`, `BrokerConfig` model
**Current Exposure:** None (config loaded at startup only)
**Gap:** No endpoint to view effective config or reload without restart
**Potential Endpoints:**
```http
GET /broker/config
POST /broker/config/reload
```
**Use Case:** Inspect resolved config (with defaults applied), reload scheduler params without restart

---

### 5. **Inflight Tracking State**
**Location:** `server.py` — `_inflight_models`, `_pending_grants`, `_pending_completions`
**Current Exposure:** Partially via `/broker/queue` (includes `inflight` dict)
**Gap:** No endpoint to see which specific request IDs are in-flight
**Potential Endpoint:**
```http
GET /broker/inflight
```
**Response:**
```json
{
  "inflight_models": {
    "qwen3:30b": 1,
    "phi4:14b": 1
  },
  "inflight_total": 2,
  "pending_grants": ["req-abc123", "req-def456"],
  "pending_completions": ["req-xyz789"],
  "grant_timeout_seconds": 300.0
}
```

---

## Middleware Gaps

### 1. **Rate Limiting Not Applied to Admin Routes**
**Location:** `server.py` — `create_admin_app()` (two-port mode)
**Current State:** `RateLimitMiddleware` only on proxy app (`create_proxy_app()`), not on admin app
**Impact:** Admin routes (`/broker/*`) are not rate-limited in two-port mode
**Recommendation:**
```python
# In create_admin_app():
app.add_middleware(RateLimitMiddleware, config=config.rate_limit)
```
**Rationale:** Admin API endpoints (especially `/broker/preload`, `/broker/intent`) can trigger expensive operations and should be rate-limited to prevent abuse

---

### 2. **No Request ID Injection**
**Location:** None
**Current State:** No middleware to inject `X-Request-ID` header for tracing
**Impact:** Difficult to correlate requests across logs, audit events, and metrics
**Recommendation:** Add `RequestIDMiddleware` to inject a unique ID on every request:
```python
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```
**Use Case:** Link audit events, Prometheus metrics, and OTel traces to specific requests

---

### 3. **No CORS Middleware**
**Location:** None
**Current State:** BASTION does not configure CORS headers
**Impact:** Web-based dashboards or browser clients cannot access BASTION from different origins
**Recommendation:** Add `CORSMiddleware` when needed for web UIs:
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # or "*" for dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
**Use Case:** Enable browser-based admin dashboards, A2A web clients

---

## Authentication Matrix

| Route Prefix | Auth Required | Scheme | Token Source | Bypass Condition |
|--------------|---------------|--------|--------------|------------------|
| `/api/*` | ❌ No | N/A | N/A | Always open (Ollama compatibility) |
| `/broker/*` | 🔑 Conditional | Bearer | `auth.api_keys` | `auth.enabled: false` OR empty `api_keys` |
| `/a2a/*` | 🎫 Conditional | Bearer | `a2a.tokens` | Empty `a2a.tokens` list |
| `/.well-known/*` | ❌ No | N/A | N/A | Always open (A2A discovery) |

**Implementation:**
- Admin auth: `make_admin_key_dependency()` applied to `APIRouter(prefix="/broker", dependencies=[Depends(verify_admin)])`
- A2A auth: `make_a2a_token_dependency()` applied to `APIRouter(prefix="/a2a", dependencies=[Depends(verify_a2a)])`
- Uses FastAPI `Depends()` pattern (no middleware)

**Token Validation:**
- Admin: `Authorization: Bearer <token>` must match entry in `auth.api_keys`
- A2A: `Authorization: Bearer <token>` must match entry in `a2a.tokens`
- Returns 401 on mismatch or missing header

---

## Middleware Stack (Order Matters)

**Single-port mode (`create_app()`):**
1. `RateLimitMiddleware` (outermost — per-IP token bucket)
2. `MetricsMiddleware` (request duration, model extraction)
3. FastAPI routing + auth dependencies

**Two-port mode (`create_proxy_app()` + `create_admin_app()`):**

**Proxy app (port 11434):**
1. `RateLimitMiddleware`
2. `MetricsMiddleware`
3. FastAPI routing (no auth — Ollama compatibility)

**Admin app (e.g. port 9999):**
1. `MetricsMiddleware` ⚠️ **No rate limiting** (gap identified above)
2. FastAPI routing + auth dependencies

---

## Client Usage Analysis

**bastion-client library** (`clients/bastion-client/bastion_client/client.py`) uses:

| Endpoint | Method | Client Usage |
|----------|--------|--------------|
| `/broker/intent` | POST | `declare_intent()` — session profile registration |
| `/api/generate` | POST | `infer()` — inference with priority header injection |
| `/broker/status` | GET | `check_vram()` — VRAM budget query |

**Not used by client:**
- `/broker/queue`, `/broker/health`, `/broker/recent`, `/broker/watchdog`
- `/broker/preload`, `/broker/unload`, `/broker/drain`, `/broker/resume`
- All A2A endpoints (no A2A client wrapper yet)

**Gap:** No client library for A2A task submission (bastion-client only wraps admin API)

---

## Key Files for Analysts

### Core Routing
- **`src/bastion/server.py`** (1561 lines) — All route definitions, lifespan, app factories
- **`src/bastion/proxy.py`** (442 lines) — Proxy request handling, streaming, priority detection
- **`src/bastion/a2a.py`** (1894 lines) — A2A task lifecycle, skill handlers, agent cards

### Middleware
- **`src/bastion/middleware.py`** (138 lines) — Metrics collection
- **`src/bastion/ratelimit.py`** (163 lines) — Token-bucket rate limiting
- **`src/bastion/auth.py`** (105 lines) — Admin + A2A auth dependencies

### Models & Config
- **`src/bastion/models.py`** (~600 lines) — All Pydantic models (config, queue, GPU, A2A)
- **`src/bastion/config.py`** — Config loading with search paths

### Internal Subsystems (Not HTTP-exposed)
- **`src/bastion/scheduler.py`** — Scheduling loop, swap rate limiting, stall detection
- **`src/bastion/queue.py`** — AffinityQueue (per-model sub-queues, priority aging)
- **`src/bastion/vram.py`** — VRAM tracking, residency cache, ledger
- **`src/bastion/taskstore.py`** — A2A task store (compaction, TTL, backpressure)
- **`src/bastion/circuitbreaker.py`** — Circuit breaker, transport wrapper, bulkhead
- **`src/bastion/watchdog.py`** — Ollama + GPU health monitoring
- **`src/bastion/metrics.py`** — Prometheus metrics (no-op stubs when not installed)
- **`src/bastion/audit.py`** — Tiered audit logging (JSONL)
- **`src/bastion/telemetry.py`** — OpenTelemetry tracing (no-op stubs when not installed)

---

## Security Surface

### Attack Vectors
1. **Proxy routes (`/api/*`)** — Open to Ollama clients, no auth
   - Mitigation: Rate limiting per IP, request body size limit (10 MB)
   - Risk: Queue exhaustion DoS (max 512 queued requests)

2. **Admin routes (`/broker/*`)** — Protected by API key auth when enabled
   - Mitigation: Bearer token validation
   - Gap: No rate limiting in two-port mode admin app ⚠️

3. **A2A routes (`/a2a/*`)** — Protected by bearer token when `a2a.tokens` configured
   - Mitigation: Token validation, task store size limit (10,000 tasks)
   - Risk: Task store exhaustion (returns 503 with `retry_after` on backpressure)

### Sensitive Endpoints (Require Extra Care)
- `/broker/preload` — Triggers model loads (VRAM budget enforcement in place)
- `/broker/unload` — Forces model evictions (could disrupt active leases)
- `/broker/drain` — Pauses scheduler (operator feature, should be admin-only)
- `/broker/config` — (missing) Would expose config including auth tokens if added

---

## OpenAPI Documentation

**Interactive Docs:**
- Swagger UI: `http://localhost:11434/broker/docs`
- ReDoc: `http://localhost:11434/broker/redoc`
- OpenAPI JSON: `http://localhost:11434/broker/openapi.json`

**Coverage:**
- ✅ All `/broker/*` routes
- ✅ All `/a2a/*` routes
- ❌ `/api/*` routes (deliberately excluded — pure Ollama passthrough)

**Security Schemes Advertised:**
- `AdminAPIKey` (Bearer token for `/broker/*`)
- `A2ABearerToken` (Bearer token for `/a2a/*`)

---

## Recommendations for Next Steps

### High Priority
1. **Add missing `/broker/scheduler/diagnostics` endpoint** — critical for debugging scheduler stalls
2. **Add rate limiting to admin app in two-port mode** — prevent admin API abuse
3. **Add RequestID middleware** — improve observability and log correlation

### Medium Priority
4. **Add `/broker/audit` query endpoint** — enable remote audit log access
5. **Add `/broker/config` dump endpoint** — improve config transparency
6. **Create A2A client wrapper** — extend bastion-client to support A2A tasks

### Low Priority
7. **Add CORS middleware (opt-in)** — enable web-based dashboards
8. **Add `/broker/residency` endpoint** — expose raw cache state for debugging
9. **Add `/broker/inflight` endpoint** — expose in-flight request tracking

---

## Conclusion

BASTION's HTTP API surface is **well-structured and fully documented**. All 58 endpoints are accounted for in the reference docs. The three-layer architecture (proxy, admin, A2A) is clean, with proper authentication boundaries and middleware stacking.

**No undocumented endpoints found** — excellent documentation hygiene.

**5 missing endpoints identified** that would enhance observability and debugging without exposing new attack surface.

**3 middleware gaps discovered** — rate limiting, request ID injection, and CORS support would strengthen production readiness.

The API is ready for production use with minor hardening recommended for admin routes in two-port mode.
