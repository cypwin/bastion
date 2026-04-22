# Security & Resilience Analysis -- BASTION

**Generated**: 2026-03-13
**Analyst**: Security & Resilience Analyst (Claude Opus 4.6)
**Scope**: Authentication, rate limiting, circuit breaker, watchdog, audit logging, A2A trust boundaries, input validation, systemd integration
**Files Analyzed**: auth.py, ratelimit.py, circuitbreaker.py, watchdog.py, audit.py, server.py, a2a.py, proxy.py, middleware.py, models.py, taskstore.py, config/broker.yaml

---

## Executive Summary

BASTION implements a multi-layered security and resilience architecture with **five distinct defense mechanisms**: API key authentication, per-IP rate limiting, a three-state circuit breaker, process health monitoring (watchdog), and tiered audit logging. The implementation is competent and well-structured, but the analysis reveals **17 security findings** across four severity levels, and **12 resilience gaps** that could affect production readiness.

**Overall Security Posture**: MODERATE. The foundations are solid, but several critical gaps exist: no timing-safe token comparison, no token rotation mechanism, no RBAC, unbounded bucket growth in the rate limiter, X-Forwarded-For spoofability, and open proxy routes with no authentication option. The system is designed for trusted-network deployment and would require hardening for adversarial environments.

**Overall Resilience Posture**: STRONG. The circuit breaker, bulkhead semaphore, backpressure mechanisms, and watchdog provide good protection against cascading failures. Key gaps include lack of memory leak detection in the watchdog, no automatic circuit breaker escalation to the scheduler, and no recovery strategy beyond drain/resume.

---

## 1. Authentication Analysis

### 1.1 What Exists

**File**: `/home/cyprian/BASTION/src/bastion/auth.py` (105 lines)

Two authentication dependencies using FastAPI's security framework:

1. **Admin API key auth** (`make_admin_key_dependency`): Protects `/broker/*` routes with `Authorization: Bearer <token>` validation against a static `frozenset` of keys from `config.auth.api_keys`.
2. **A2A bearer token auth** (`make_a2a_token_dependency`): Protects `/a2a/*` routes with bearer token validation against `config.a2a.tokens`.

**Strengths**:
- Uses FastAPI `Depends()` pattern (per-router, not middleware) -- avoids BaseHTTPMiddleware pitfalls with streaming and contextvars.
- Stores `admin_authenticated` and `admin_token` on `request.state` for downstream use.
- Both auth layers gracefully bypass when disabled (`config.auth.enabled = false` or empty token lists).
- Proxy routes (`/api/*`) are intentionally open for Ollama client compatibility.

### 1.2 What Is Missing

**FINDING SEC-01 [HIGH]: No timing-safe token comparison**

```python
# auth.py line 61
if token not in valid_keys:
```

Token validation uses Python's `in` operator on a `frozenset`, which performs standard string comparison. This is vulnerable to timing side-channel attacks where an attacker can determine token length and character-by-character content by measuring response time differences. The `hmac.compare_digest()` function should be used instead.

Similarly for A2A tokens at line 96:
```python
if credentials.credentials not in valid_tokens:
```

**FINDING SEC-02 [HIGH]: No API key rotation mechanism**

Tokens are loaded once at startup from `config.auth.api_keys` (a `frozenset` created at dependency construction time). There is no endpoint to rotate keys, no key expiry, and no way to revoke a compromised key without restarting the entire service. The hardening phase doc (`ref-phase2-hardening.md`) lists success criteria but does not mention key rotation as a deliverable.

**FINDING SEC-03 [MEDIUM]: No RBAC or scoped tokens**

All admin API keys grant identical access to every `/broker/*` endpoint. There is no distinction between:
- Read-only operations (`/broker/status`, `/broker/queue`, `/broker/health`)
- Dangerous write operations (`/broker/preload`, `/broker/unload`, `/broker/drain`)

A compromised read-only monitoring token can drain the scheduler or force-unload models.

Similarly, all A2A tokens grant identical access. No per-token scoping to specific skills or models.

**FINDING SEC-04 [MEDIUM]: Proxy routes permanently unauthenticated**

`/api/*` routes have no authentication option even in high-security deployments. While this is intentional for Ollama client compatibility, there is no configuration toggle to require auth on proxy routes. In two-port mode, the proxy port (11434) is completely open.

**FINDING SEC-05 [LOW]: No token identity tracking**

Auth tokens are validated but not identified. When multiple tokens exist in `api_keys`, the audit log can hash the token for identity (via `audit.hash_identity()`), but the auth layer does not pass token identity to the audit system. The `request.state.admin_token` is set but never consumed by audit middleware.

### 1.3 Auth Configuration in broker.yaml

```yaml
auth:
  enabled: false          # Disabled by default
  api_keys: []            # Empty list = no auth even if enabled
```

The default configuration ships with auth disabled. This is appropriate for local development but represents a deployment risk if not explicitly enabled in production.

---

## 2. Rate Limiting Analysis

### 2.1 What Exists

**File**: `/home/cyprian/BASTION/src/bastion/ratelimit.py` (163 lines)

A token-bucket algorithm implemented as `BaseHTTPMiddleware`:
- Per-client-IP buckets that refill at `requests_per_minute / 60` tokens per second
- Burst capacity via configurable bucket size
- Returns `429 Too Many Requests` with `Retry-After` header
- IP extraction from `X-Forwarded-For` header (first IP) or `request.client.host`

**Strengths**:
- Clean token-bucket implementation with `__slots__` optimization on `_TokenBucket`
- Async lock (`asyncio.Lock`) prevents race conditions
- `Retry-After` header provides client guidance
- Disabled by default with clean bypass path

### 2.2 What Is Missing

**FINDING SEC-06 [HIGH]: X-Forwarded-For header is spoofable**

```python
# ratelimit.py lines 116-118
forwarded = request.headers.get("X-Forwarded-For")
if forwarded:
    return forwarded.split(",")[0].strip()
```

Any client can set `X-Forwarded-For: 1.2.3.4` to appear as a different IP, completely bypassing rate limiting. There is no trusted proxy list, no validation that the request actually came through a reverse proxy, and no option to disable X-Forwarded-For parsing. An attacker can rotate through arbitrary IPs on every request.

**FINDING SEC-07 [MEDIUM]: Unbounded bucket dictionary growth**

```python
# ratelimit.py line 99
self._buckets: dict[str, _TokenBucket] = {}
```

New buckets are created for every unique client IP but never evicted. An attacker spoofing X-Forwarded-For headers with random IPs can grow this dictionary unboundedly, causing memory exhaustion. There is no max-entries limit, no LRU eviction, and no periodic cleanup of stale buckets.

**FINDING SEC-08 [MEDIUM]: Rate limiting not applied to admin routes in two-port mode**

As identified by the API Surface Scout, `create_admin_app()` at server.py line 1172 adds only `MetricsMiddleware`, not `RateLimitMiddleware`:

```python
# server.py line 1172
app.add_middleware(MetricsMiddleware)
# No RateLimitMiddleware added
```

Admin endpoints (`/broker/preload`, `/broker/drain`) can trigger expensive GPU operations and are not rate-limited in two-port mode.

**FINDING SEC-09 [LOW]: No per-model or per-user rate limiting**

Rate limiting is purely per-IP. There is no:
- Per-model rate limiting (prevent one model from monopolizing the queue)
- Per-authenticated-user rate limiting (token-based quotas)
- Per-endpoint rate limiting (different limits for `/api/generate` vs `/api/tags`)
- Adaptive rate limiting based on system load (e.g., reduce limits when queue is deep)

### 2.3 Rate Limit Configuration

```yaml
rate_limit:
  enabled: false
  requests_per_minute: 60
  burst: 10
```

Disabled by default. The 60 RPM default is reasonable for a single-GPU broker.

---

## 3. Circuit Breaker Analysis

### 3.1 What Exists

**File**: `/home/cyprian/BASTION/src/bastion/circuitbreaker.py` (336 lines)

A comprehensive three-state circuit breaker implementation with three components:

1. **CircuitBreaker** (core state machine): CLOSED -> OPEN -> HALF_OPEN -> CLOSED
   - Trips after `failure_threshold` consecutive failures (default 5)
   - Recovery timeout before half-open probe (default 30s)
   - Async lock for thread safety
   - Caches last successful `/api/tags` response for graceful degradation

2. **CircuitBreakerTransport** (httpx transport wrapper): Wraps all outgoing Ollama requests
   - 5xx responses recorded as failures and raise `OllamaBackendError`
   - Connection errors (`ConnectError`, `ConnectTimeout`, `ReadTimeout`) recorded as failures
   - Streaming responses record success on connection establishment
   - Bypasses when `config.enabled = false`

3. **BulkheadSemaphore** (concurrency limiter): Limits concurrent Ollama calls (default 5)
   - Prevents overwhelming a recovering backend during half-open state
   - Exposes `active_count` and `max_concurrent` properties

**Strengths**:
- The transport-level integration means ALL outgoing requests go through the breaker, regardless of code path.
- Graceful degradation with cached `/api/tags` response when Ollama is down.
- The `CircuitOpenError` includes `recovery_remaining` for client retry guidance.
- A2A handler fast-fails with JSON-RPC error code -32050 when circuit is open.

### 3.2 What Could Be Improved

**FINDING RES-01 [MEDIUM]: Half-open allows unlimited concurrent probes**

When the circuit transitions from OPEN to HALF_OPEN, the `call()` method checks the effective state and lets calls through:

```python
# circuitbreaker.py lines 177-179
if effective is _State.HALF_OPEN:
    # Transition into half-open officially so only one probe runs
    self._state = _State.HALF_OPEN
```

The comment says "only one probe runs" but the lock is released before `func()` executes (lines 182-189). Multiple concurrent requests can all see HALF_OPEN and all proceed as probes. A single-probe enforcement mechanism (e.g., a flag or semaphore inside the lock) would prevent thundering herd on recovery.

**FINDING RES-02 [LOW]: Circuit breaker does not feed into scheduler decisions**

The circuit breaker state is exposed via `/broker/health` (as a string), but the scheduler does not consult it. When the circuit is OPEN, the scheduler continues trying to dispatch requests. These requests will fail immediately at the proxy layer, but the scheduler wastes cycles and creates confusing stall diagnostics.

The watchdog already integrates with the scheduler via `drain()`/`resume()` callbacks, but the circuit breaker has no such integration.

**FINDING RES-03 [LOW]: No progressive failure thresholds**

The circuit breaker has a single `failure_threshold` (5). There is no:
- Warning state before opening (e.g., 3 failures = log warning, 5 = trip)
- Sliding window failure rate (e.g., "5 failures in 60 seconds" rather than "5 consecutive")
- Different thresholds for different error types (timeout vs. 5xx vs. connection refused)

### 3.3 Circuit Breaker Integration Points

The circuit breaker is well-integrated across the system:
- **proxy.py**: CB instance created in `OllamaProxy.__init__()`, state checked before forwarding
- **server.py**: CB shared between proxy and A2A via `CircuitBreakerTransport`
- **a2a.py**: Fast-fail on `create_task()` when circuit is open
- **server.py `/broker/health`**: Reports circuit state
- **server.py `/broker/readyz`**: Returns 503 when circuit is open

---

## 4. Watchdog & Process Monitor Analysis

### 4.1 What Exists

**File**: `/home/cyprian/BASTION/src/bastion/watchdog.py` (326 lines)

Two subsystems:

1. **Systemd sd_notify integration**: Sends READY, WATCHDOG, STOPPING, STATUS messages via Unix datagram socket. Safe no-ops when not under systemd.

2. **ProcessMonitor**: Async background task that periodically checks:
   - **Ollama health**: HTTP GET to Ollama root endpoint with configurable timeout (default 5s)
   - **GPU responsiveness**: `nvidia-smi --query-gpu=temperature.gpu` with subprocess timeout (default 5s)

**Health determination**:
- `consecutive_ollama_failures >= failure_threshold` (default 3) -> unhealthy
- `consecutive_gpu_timeouts >= failure_threshold` (default 3) -> unhealthy
- On transition to unhealthy: calls `on_unhealthy` callback (scheduler.drain)
- On recovery to healthy: calls `on_healthy` callback (scheduler.resume)

**Strengths**:
- Graceful handling of missing nvidia-smi (`FileNotFoundError`)
- Kills hung nvidia-smi processes after timeout
- Clean async lifecycle (start/stop with task cancellation)
- Latency tracking for both Ollama and GPU checks
- WatchdogStatus model exposed via `/broker/watchdog`

### 4.2 What Is Missing

**FINDING RES-04 [MEDIUM]: No memory leak detection**

The watchdog checks Ollama responsiveness and GPU temperature, but does not monitor:
- BASTION's own RSS/heap memory usage (Python `resource` module or `/proc/self/status`)
- Ollama process memory growth (which could indicate a leak in the backend)
- TaskStore size growth (backpressure is internal but not watchdog-monitored)
- Rate limiter bucket count growth (SEC-07)

A slowly growing memory leak could eventually OOM-kill the process with no warning.

**FINDING RES-05 [MEDIUM]: No GPU VRAM monitoring in watchdog**

The watchdog queries GPU temperature but not VRAM usage. The `nvidia-smi` query only requests `temperature.gpu`. It could also query `memory.used`, `memory.total`, and `utilization.gpu` to detect:
- VRAM exhaustion approaching (before OOM crash)
- GPU utilization anomalies (stuck at 100% or unexpected 0%)
- VRAM fragmentation (high usage but models report lower usage via Ollama)

**FINDING RES-06 [LOW]: No systemd watchdog heartbeat emission**

The `init_watchdog()` and `notify_ready()`/`notify_stopping()` functions are called at startup/shutdown, but `notify_watchdog()` (the periodic heartbeat) is never called from the ProcessMonitor loop. If systemd's `WatchdogSec` is configured, systemd will kill the process for missing heartbeats even if BASTION is healthy.

The ProcessMonitor loop (lines 208-249) should call `notify_watchdog()` on each healthy iteration.

**FINDING RES-07 [LOW]: Watchdog does not detect model corruption**

If Ollama loads a corrupt model or a model that produces garbage output, the watchdog has no way to detect this. The health check only verifies Ollama responds to HTTP, not that inference produces valid results. A "canary prompt" mechanism could detect model corruption.

### 4.3 Systemd Integration

**What exists**:
- `init_watchdog()`: Creates sd_notify socket from `NOTIFY_SOCKET` env var
- `notify_ready()`: Sends `READY=1` at startup
- `notify_stopping()`: Sends `STOPPING=1` at shutdown
- `notify_status()`: Sends human-readable status text
- Abstract socket support (`@` prefix)

**What could be enhanced**:
- Periodic `WATCHDOG=1` heartbeats (see RES-06)
- `RELOADING=1` for config reload support
- `EXTEND_TIMEOUT_USEC` for long model loads that exceed WatchdogSec
- Integration with `systemd-journal` structured logging (key=value format)

---

## 5. Audit Logging Analysis

### 5.1 What Exists

**File**: `/home/cyprian/BASTION/src/bastion/audit.py` (340 lines)

A tiered audit logging system with three levels:

| Tier | Content |
|------|---------|
| 1 | Timestamps, event type, request details, identity hashes, source IP |
| 2 (default) | Tier 1 + SHA-256 content hashes of prompt/response |
| 3 (opt-in) | Tier 2 + raw prompt/response text |

**Identity tracking**:
- `hash_identity(token)`: SHA-256 of bearer token (never stores raw tokens)
- `hash_content(text)`: SHA-256 of prompt/response text
- `a2a_identity`: Agent name, skill ID, task ID, context ID

**Storage**:
- JSONL format to `/tmp/bastion-audit.jsonl`
- `RotatingFileHandler`: 10 MB per file, 5 backup files
- Separate Python logger (`bastion.audit`) with `propagate=False`

**Emission points** (discovered via code search):
- `proxy.py`: `EVENT_REQUEST_COMPLETE` on every scheduled request
- `a2a.py`: `a2a_infer_complete`, `a2a_batch_infer_complete`, `a2a_preload_complete`
- `scheduler.py`: Model swap events
- `server.py`: Queue sweep events
- `vram.py`: VRAM snapshot events

### 5.2 What Is Missing

**FINDING SEC-10 [HIGH]: Audit log stored in /tmp (world-readable, volatile)**

```python
# audit.py line 158
log_path: str = "/tmp/bastion-audit.jsonl"
```

The audit log is written to `/tmp/bastion-audit.jsonl` by default, which:
- Is world-readable on most Linux systems (any local user can read identity hashes, source IPs, and event metadata)
- Is volatile (cleared on reboot; `tmpfiles.d` may also clean it)
- At tier 3, contains raw prompts and responses in a world-readable location
- Has no file permission restrictions (created with default umask)

The path is configurable via `init_audit_logger(log_path=...)` but the default is insecure and the config file (`broker.yaml`) does not expose a `log_path` option.

**FINDING SEC-11 [MEDIUM]: No auth failure audit events**

Failed authentication attempts (401 responses) are logged via Python's standard logger (`logger.warning`) but not emitted as structured audit events. An attacker brute-forcing API keys would not appear in the audit log stream. Only the application log would show warnings.

**FINDING SEC-12 [MEDIUM]: Incomplete audit coverage**

Several security-relevant events are not audited:
- Rate limit violations (429 responses) -- no `audit.emit()` call in `ratelimit.py`
- Circuit breaker state transitions (open/half-open/closed) -- only Python logging
- Admin actions (`/broker/drain`, `/broker/resume`, `/broker/preload`, `/broker/unload`) -- no audit events
- A2A lease creation, heartbeat, release -- no audit events
- Intent declaration/completion -- no audit events
- Watchdog state transitions (healthy->unhealthy) -- only Python logging

**FINDING SEC-13 [LOW]: No audit log integrity protection**

Audit logs are plain JSONL files with no:
- HMAC or digital signature per line (tampering detection)
- Sequence numbers (deletion detection)
- Forward-secure hash chain (append-only verification)
- Write-once storage option (immutable audit trail)

### 5.3 Could Audit Logs Feed Security Analytics?

Yes, the tiered structure is well-designed for analytics:

**What is possible today**:
- Identity correlation: Track all requests from the same bearer token (via `auth_identity_hash`)
- Content deduplication: Same `prompt_hash` appearing from different sources = prompt sharing or injection
- Latency anomaly detection: Unusual `dispatch_duration_seconds` could indicate model loading issues
- A2A agent tracking: `a2a_identity.context_id` links related tasks

**What would need to be added**:
- Auth failure events (SEC-11) for brute-force detection
- Rate limit events for DDoS pattern analysis
- Request ID correlation (no `X-Request-ID` middleware exists, as noted by API Surface Scout)
- Source IP tracking on proxy requests (currently only available in tiered events, not basic `emit()`)

---

## 6. A2A Protocol Security Analysis

### 6.1 Trust Boundaries

The A2A protocol has three trust tiers implemented via the Agent Card disclosure model:

| Tier | Endpoint | Auth | Exposed Data |
|------|----------|------|--------------|
| 1 (Public) | `/.well-known/agent-card.json` | None | Generic identity, skill categories, security schemes |
| 2 (Authenticated) | `/a2a/extended-card` | Bearer token | Model list, VRAM sizes, input/output schemas |
| 3 (Admin) | `/broker/status` | Admin API key | Queue depth, GPU hardware, VRAM ledger |

**Strengths**:
- Three-tier card disclosure prevents infrastructure leakage to unauthenticated agents
- Public card does NOT expose model names, VRAM data, queue depth, or GPU info
- Extended card requires A2A token authentication

### 6.2 Token Management

**FINDING SEC-14 [HIGH]: A2A open access by default**

```yaml
a2a:
  enabled: true
  tokens: []    # Empty = open access
```

When `a2a.tokens` is empty (the default), all A2A endpoints are completely unauthenticated. Any client can create tasks, submit inference requests, list active tasks, and cancel tasks. This is documented but represents a significant risk if A2A is enabled without configuring tokens.

**FINDING SEC-15 [MEDIUM]: No per-agent identity in A2A**

All A2A tokens grant identical access. There is no:
- Agent identity associated with tokens (which agent is making this request?)
- Per-agent task isolation (agent A can see and cancel agent B's tasks)
- Per-agent model restrictions (agent A limited to specific models)
- Per-agent batch size limits

### 6.3 Task Store Security

The `TaskStore` (taskstore.py) implements several defensive measures:
- **Backpressure**: Three levels (normal/pressure/overloaded) with hysteresis
- **Capacity bounds**: 10,000 active tasks, 50,000 completed, 10,000 tombstones
- **TTL enforcement**: Active tasks expire after 1 hour (configurable)
- **State machine validation**: Only legal transitions allowed (e.g., SUBMITTED->WORKING->COMPLETED)

**Potential concern**: Task IDs are UUID4 hex (12 chars = 48 bits of entropy). An attacker who can enumerate or guess task IDs can retrieve other agents' results. However, with 48 bits this requires ~2^24 attempts to find a valid ID by birthday attack, which rate limiting would prevent.

### 6.4 Lease Security

Model leases (`ModelLease`) include fencing tokens for zombie prevention:
- Monotonically increasing fencing token counter
- Heartbeat requires matching fencing token (prevents stale clients)
- Triple eviction: request count, absolute TTL, idle timeout

**No finding**: The fencing token mechanism is well-designed for its purpose.

---

## 7. Input Validation & Request Security

### 7.1 What Exists

- **Request body size limit**: `proxy.max_request_body_bytes = 10 MB` enforced in `proxy.py` line 118
- **JSON parsing with error handling**: Returns 400 on malformed JSON (proxy.py line 143)
- **Batch size validation**: `max_batch_size = 50` enforced in `a2a.py` line 965
- **Model validation**: `_handle_preload` checks `model in config.models` (a2a.py line 1273)
- **Reservation limits**: `reservation_max_requests = 100` enforced (a2a.py line 1283)

### 7.2 What Is Missing

**FINDING SEC-16 [MEDIUM]: No prompt injection protection**

Prompts are passed directly to Ollama without any sanitization or filtering. While BASTION is a proxy (not an LLM application), it could provide:
- Optional system prompt injection detection (patterns like "ignore previous instructions")
- Content filtering hooks (blocklist/allowlist patterns)
- Prompt length limits separate from body size limits

This is a deliberate design choice (transparent proxy), but worth noting for deployments where BASTION serves untrusted clients.

**FINDING SEC-17 [LOW]: No model name validation on proxy routes**

Proxy routes (`/api/generate`, `/api/chat`) accept any model name string. The scheduler will queue and attempt to load models not in the `config.models` registry. While Ollama itself will reject unknown models, this means:
- Queue slots are wasted on requests for non-existent models
- A malicious client can fill the queue (512 slots) with requests for garbage model names
- The scheduler will attempt swaps that will fail, consuming cooldown time

A2A routes do validate model names (`model not in self._config.models`), but proxy routes do not.

---

## 8. Phase 2 Hardening Plan vs Implementation

### 8.1 What Was Planned (ref-phase2-hardening.md)

The hardening plan specified 7 deliverables:

| Deliverable | Status |
|-------------|--------|
| Bearer-token auth for `/broker/*` | IMPLEMENTED (auth.py) |
| Token-bucket rate limiting per IP | IMPLEMENTED (ratelimit.py) |
| Three-state circuit breaker | IMPLEMENTED (circuitbreaker.py) |
| Config extraction (14 hardcoded values) | IMPLEMENTED (all values in broker.yaml) |
| Fix `GPUStatus.is_safe` bug | IMPLEMENTED (uses configurable thresholds) |
| `/broker/livez` endpoint | IMPLEMENTED (server.py) |
| `/broker/readyz` endpoint | IMPLEMENTED (server.py, checks CB state) |

### 8.2 What Was Not Fully Implemented

1. **Auth middleware was replaced with dependencies**: The plan called for "bearer-token middleware for `/broker/*`" but the implementation correctly uses FastAPI `Depends()` instead of `BaseHTTPMiddleware`. This is an improvement over the plan.

2. **Rate limiting "configurable per tier"**: The plan mentioned per-tier rate limiting, but the implementation is per-IP only. No tier-based differentiation exists.

3. **Request body validation**: The plan mentioned "add request body validation to proxy.py" which was implemented as a body size check. No schema validation or content validation was added.

4. **No mention of key rotation**: The hardening plan does not address key rotation, token scoping, or RBAC -- these remain unimplemented.

---

## 9. Resilience Architecture Summary

### 9.1 Defense-in-Depth Layers

```
Layer 1: Rate Limiting (per-IP token bucket)
   |
Layer 2: Authentication (Bearer token, per-router)
   |
Layer 3: Request Validation (body size, JSON parsing)
   |
Layer 4: Queue Backpressure (max 512 requests, TTL sweep)
   |
Layer 5: Scheduler Safety (cooldown, swap rate limiter, GPU checks)
   |
Layer 6: Circuit Breaker (transport-level, 3-state)
   |
Layer 7: Bulkhead (concurrency semaphore, max 5)
   |
Layer 8: Watchdog (Ollama health, GPU responsiveness)
   |
Layer 9: Systemd Integration (sd_notify, WatchdogSec)
   |
Layer 10: Audit Logging (tiered, identity-tracked)
```

### 9.2 Failure Scenarios and Current Response

| Scenario | Detection | Response | Gap |
|----------|-----------|----------|-----|
| Ollama crashes | Watchdog HTTP ping fails | Drain scheduler after 3 failures | No automatic restart trigger |
| GPU lockup | nvidia-smi timeout | Drain scheduler | No systemd restart notification |
| VRAM exhaustion | check_gpu_safe() in scheduler | Skip dispatch when VRAM > 95% | No preemptive eviction signal |
| Queue flood | max_queue_size = 512 | Reject with 503 | No adaptive rate limiting |
| Token brute force | Python logger warning | No active response | No account lockout, no audit event |
| DDoS (IP spoofing) | Rate limiter bucket creation | None (buckets grow unbounded) | Memory exhaustion risk |
| Model load failure | Circuit breaker failure count | Trip after 5 failures | No per-model breaker |
| Task store exhaustion | Backpressure levels | 503 with retry_after | Not watchdog-monitored |
| Audit log full | RotatingFileHandler | Rotate to backups | Only 50 MB total, world-readable |

---

## 10. Findings Summary Table

### Security Findings

| ID | Severity | Component | Finding |
|----|----------|-----------|---------|
| SEC-01 | HIGH | auth.py | No timing-safe token comparison (vulnerable to timing attacks) |
| SEC-02 | HIGH | auth.py | No API key rotation mechanism (restart required to change keys) |
| SEC-03 | MEDIUM | auth.py | No RBAC or scoped tokens (all keys have full access) |
| SEC-04 | MEDIUM | server.py | Proxy routes permanently unauthenticated (no opt-in auth) |
| SEC-05 | LOW | auth.py | Auth token identity not passed to audit system |
| SEC-06 | HIGH | ratelimit.py | X-Forwarded-For spoofable (no trusted proxy validation) |
| SEC-07 | MEDIUM | ratelimit.py | Unbounded bucket dictionary growth (memory exhaustion risk) |
| SEC-08 | MEDIUM | server.py | No rate limiting on admin routes in two-port mode |
| SEC-09 | LOW | ratelimit.py | No per-model, per-user, or adaptive rate limiting |
| SEC-10 | HIGH | audit.py | Audit log in /tmp (world-readable, volatile, no permissions) |
| SEC-11 | MEDIUM | auth.py | No auth failure audit events (brute force invisible) |
| SEC-12 | MEDIUM | audit.py | Incomplete audit coverage (rate limits, CB transitions, admin actions) |
| SEC-13 | LOW | audit.py | No audit log integrity protection (no HMAC/signatures) |
| SEC-14 | HIGH | config | A2A open access by default (empty tokens = no auth) |
| SEC-15 | MEDIUM | a2a.py | No per-agent identity in A2A (shared access, no isolation) |
| SEC-16 | MEDIUM | proxy.py | No prompt injection protection or content filtering |
| SEC-17 | LOW | proxy.py | No model name validation on proxy routes (queue pollution) |

### Resilience Findings

| ID | Severity | Component | Finding |
|----|----------|-----------|---------|
| RES-01 | MEDIUM | circuitbreaker.py | Half-open allows unlimited concurrent probes (thundering herd) |
| RES-02 | LOW | circuitbreaker.py | Circuit breaker state not fed into scheduler decisions |
| RES-03 | LOW | circuitbreaker.py | No progressive failure thresholds or sliding window |
| RES-04 | MEDIUM | watchdog.py | No memory leak detection (BASTION or Ollama) |
| RES-05 | MEDIUM | watchdog.py | No GPU VRAM monitoring in watchdog checks |
| RES-06 | LOW | watchdog.py | No periodic systemd watchdog heartbeat emission |
| RES-07 | LOW | watchdog.py | No model corruption detection (canary prompt) |

---

## 11. Recommendations by Priority

### Immediate (Security-Critical)

1. **Use `hmac.compare_digest()` for token comparison** (SEC-01): Replace `token not in valid_keys` with constant-time comparison loop.

2. **Restrict audit log path and permissions** (SEC-10): Default to `/var/log/bastion/audit.jsonl` (or configurable), create with `0600` permissions, expose `log_path` in broker.yaml.

3. **Add trusted proxy configuration** (SEC-06): Add `rate_limit.trusted_proxies: list[str]` config. Only parse X-Forwarded-For when request comes from a trusted IP.

4. **Add LRU eviction to rate limiter buckets** (SEC-07): Cap at 10,000 entries with LRU eviction of oldest buckets.

### Short-Term (Operational Hardening)

5. **Emit auth failure audit events** (SEC-11): Add `audit.emit("auth_failure", {...})` in auth.py when returning 401.

6. **Add rate limiting to admin app** (SEC-08): Add `RateLimitMiddleware` in `create_admin_app()`.

7. **Add systemd watchdog heartbeat** (RES-06): Call `notify_watchdog()` at the end of each healthy ProcessMonitor loop iteration.

8. **Add VRAM monitoring to watchdog** (RES-05): Extend nvidia-smi query to include `memory.used,memory.total`.

9. **Limit half-open concurrent probes** (RES-01): Add `_probing` flag inside the lock to allow only one request during HALF_OPEN.

### Medium-Term (Feature Enhancements)

10. **Implement API key rotation endpoint** (SEC-02): `POST /broker/auth/rotate` to add/remove keys at runtime without restart.

11. **Add read-only vs read-write token scoping** (SEC-03): Distinguish admin token permissions by prefix convention or config mapping.

12. **Expand audit coverage** (SEC-12): Add audit events for rate limiting, circuit breaker transitions, admin actions, and lease lifecycle.

13. **Add memory monitoring to watchdog** (RES-04): Track RSS via `/proc/self/status` and rate limiter bucket count.

14. **Add per-model circuit breakers** (RES-03): Individual model failure tracking (some models may be corrupt while others work).

### Long-Term (Advanced Security)

15. **RBAC with scoped tokens** (SEC-03, SEC-15): Token-to-permission mapping with model-level and skill-level access control.

16. **Audit log integrity** (SEC-13): HMAC chain or forward-secure hash for tamper detection.

17. **Prompt filtering hooks** (SEC-16): Optional content policy engine with configurable rules.

---

## 12. Key Files Reference

| File | Path | Security Role |
|------|------|---------------|
| auth.py | `/home/cyprian/BASTION/src/bastion/auth.py` | API key + bearer token validation |
| ratelimit.py | `/home/cyprian/BASTION/src/bastion/ratelimit.py` | Per-IP token-bucket rate limiting |
| circuitbreaker.py | `/home/cyprian/BASTION/src/bastion/circuitbreaker.py` | Three-state circuit breaker + transport wrapper + bulkhead |
| watchdog.py | `/home/cyprian/BASTION/src/bastion/watchdog.py` | Process monitor + systemd sd_notify |
| audit.py | `/home/cyprian/BASTION/src/bastion/audit.py` | Tiered audit logging with identity hashing |
| server.py | `/home/cyprian/BASTION/src/bastion/server.py` | App factory, route auth, middleware stack |
| a2a.py | `/home/cyprian/BASTION/src/bastion/a2a.py` | A2A protocol, task lifecycle, leases |
| proxy.py | `/home/cyprian/BASTION/src/bastion/proxy.py` | Transparent proxy, request validation |
| models.py | `/home/cyprian/BASTION/src/bastion/models.py` | Config models (AuthConfig, RateLimitConfig, etc.) |
| taskstore.py | `/home/cyprian/BASTION/src/bastion/taskstore.py` | Task store with backpressure and TTL |
| middleware.py | `/home/cyprian/BASTION/src/bastion/middleware.py` | Metrics collection middleware |
| broker.yaml | `/home/cyprian/BASTION/config/broker.yaml` | Default configuration |
| ref-phase2-hardening.md | `/home/cyprian/BASTION/docs/audit/ref-phase2-hardening.md` | Hardening plan (7 deliverables) |

---

**End of Report**

Generated by Security & Resilience Analyst
Session: S0 (Audit Phase)
Builds on: scout-code-cartography.md, scout-api-surface.md, scout-data-models.md
