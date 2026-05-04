# M58 BASTION Integration — Complexity Routing & Thrashing Detection

> **Date:** 2026-04-20
> **Status:** Approved
> **Source:** M58_BASTION_HANDOFF.md (from SWARM_BRAIN)
> **Scope:** BASTION-side implementation for M58 Smart Local Offloading

---

## 1. Problem Statement

SWARM_BRAIN M58 introduces smart offloading — routing sub-tasks (summarization,
classification, advisory composition) to local LLMs via BASTION instead of
consuming Claude API tokens. The client-side is complete (D0-D5 implemented).
BASTION needs to:

1. Route requests to the right model based on task complexity
2. Reject tasks that should go to Claude, not local models
3. Notify callers when model overrides occur
4. Detect and mitigate poorly-batched pipelines that cause GPU-damaging swap thrashing
5. Capture richer audit data for telemetry

## 2. Design Decisions (from brainstorming)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Routing config | Configurable in `broker.yaml` via `complexity_routing` section | Works with any installed model inventory |
| Model override | Always override client model when header present | BASTION is single source of truth for model selection |
| Override notification | Response headers only (`X-Model-Requested`, `X-Model-Routed`, `X-Routing-Reason`) | Non-breaking, works for streaming and non-streaming |
| Token count headers | Non-streaming only; streaming captured in audit log | Trailing HTTP headers have poor client support |
| Thrashing response | Configurable: warn-only (default) or strict (halt) | Safe for dev, protective for production |
| Thrashing thresholds | Data-informed from RTX 5090 crash investigation | Aligned with existing global swap rate thresholds |
| Batch/keep_alive handling | No extra work — existing protections sufficient | Ollama respects keep_alive, BASTION protects in-flight models |

## 3. Feature 1: Complexity-Based Model Routing

### 3.1 New Header: `X-Task-Complexity`

Set by SWARM_BRAIN's `OllamaClient` on every outgoing request:

```
X-Task-Complexity: simple | moderate | complex
```

Values map to a 0.0-1.0 complexity score via `ComplexityScorer`:
- `simple` (score < 0.30): classification, HyDE, fast extraction
- `moderate` (0.30-0.70): evaluation, composition, summarization
- `complex` (> 0.70): must go to Claude — rejected by client and BASTION

### 3.2 Configuration

New section in `broker.yaml`:

```yaml
complexity_routing:
  enabled: true
  routes:
    simple: "qwen3.5:9b"
    moderate: "qwen3.5:35b-a3b"
  complex_action: "reject"   # always HTTP 422
```

- Route model names must exist in the `models` section of `broker.yaml`
- `complex_action` is always `"reject"` — included for explicitness
- When `enabled: false`, header is ignored (backward-compatible)

### 3.3 Routing Behavior

**Location:** `proxy.py`, in `_handle_scheduled()`, after body parse but before enqueue.

1. Read `X-Task-Complexity` header from request
2. If absent or `complexity_routing.enabled` is false → use client-requested model (existing behavior)
3. If `complex` → reject immediately with HTTP 422:
   ```json
   {
     "error": "Task complexity 'complex' requires Claude, not local model. Route to API.",
     "complexity": "complex"
   }
   ```
4. If `simple` or `moderate` → override `payload["model"]` with configured route model
5. Store original model name for response headers and audit

The override happens before `QueuedRequest` creation, so the request enters the
correct per-model sub-queue in `AffinityQueue` automatically.

### 3.4 Header Interaction with Existing Headers

| Header | Role | Interaction |
|--------|------|-------------|
| `X-Task-Complexity` | Determines **which model** | Overrides client model selection |
| `X-Broker-Priority` | Determines **queue priority** | Independent — applied as before |
| `X-Agent-Id` | **Audit/tracking** only | Captured in audit log alongside routing data |

Priority: `X-Task-Complexity` is processed first (model selection), then
`X-Broker-Priority` (queue ordering). No conflicts possible.

## 4. Feature 2: Response Headers

### 4.1 Routing Notification Headers

Injected on every response where complexity routing occurred:

| Header | Value | Example |
|--------|-------|---------|
| `X-Model-Requested` | Original model from client payload | `qwen3:8b` |
| `X-Model-Routed` | Actual model BASTION selected | `qwen3.5:9b` |
| `X-Routing-Reason` | `complexity-{tier}` | `complexity-simple` |

**Streaming:** injected on the initial response (before first chunk).
**Non-streaming:** injected on the response.

When no routing override occurred (header absent or routing disabled), these
headers are not added.

### 4.2 Token Count Headers (Non-Streaming Only)

| Header | Source | Example |
|--------|--------|---------|
| `X-Prompt-Tokens` | Ollama `prompt_eval_count` from response JSON | `1423` |
| `X-Completion-Tokens` | Ollama `eval_count` from response JSON | `256` |

**Streaming:** not feasible as response headers (token counts appear in final
chunk only). Token counts captured in audit log instead.

**Non-streaming:** extracted from Ollama's JSON response before forwarding to
client.

## 5. Feature 3: Audit Log Enrichment

### 5.1 New Fields on `EVENT_REQUEST_COMPLETE`

```json
{
  "event": "request_complete",
  "agent_id": "jury_agent",
  "task_complexity": "simple",
  "model_requested": "qwen3:8b",
  "model_routed": "qwen3.5:9b",
  "routing_applied": true,
  "prompt_tokens": 1423,
  "completion_tokens": 256,
  "...existing fields..."
}
```

| Field | Source | When present |
|-------|--------|--------------|
| `agent_id` | `X-Agent-Id` header | When header present |
| `task_complexity` | `X-Task-Complexity` header | When header present |
| `model_requested` | Original client model | When routing override applied |
| `model_routed` | BASTION-selected model | When routing override applied |
| `routing_applied` | Boolean | Always (false if no override) |
| `prompt_tokens` | Ollama `prompt_eval_count` | Both streaming and non-streaming |
| `completion_tokens` | Ollama `eval_count` | Both streaming and non-streaming |

For streaming responses, token counts are extracted from the final NDJSON chunk
(`"done": true`) during stream passthrough.

### 5.2 New Event: `EVENT_THRASHING`

Emitted when the thrashing detector issues a warn or halt verdict:

```json
{
  "event": "thrashing",
  "agent_id": "ingestion_agent",
  "verdict": "warn",
  "swap_ratio": 0.67,
  "window_size": 12,
  "models_in_window": ["qwen3.5:9b", "qwen3.5:35b-a3b", "qwen3.5:9b", "..."],
  "estimated_penalty_seconds": 84.0
}
```

## 6. Feature 4: Per-Agent Swap Thrashing Detector

### 6.1 Motivation

The RTX 5090 crash investigation (Sessions S58-S62) established empirical
thresholds:

| Swap Rate | Observed Behavior |
|-----------|-------------------|
| < 4 swaps/min | Stable operation |
| 4-6 swaps/min | Warning zone — occasional driver slowdowns |
| 6-8 swaps/min | Danger zone — nvidia-smi timeouts |
| > 8 swaps/min | **Crash zone — system reboots within 4-6 min** |

Root cause crash: 175 model swaps in 7 minutes (25/min) from unbatched jury
evaluation. D0 fixed this on the client side, but BASTION should also detect
and prevent poorly-batched access patterns from any client.

### 6.2 Architecture

**New module:** `src/bastion/thrashing.py`

**`ThrashingDetector` class:**
- Maintains a per-agent sliding window of recent requests
- Keyed by `X-Agent-Id` header, falls back to source IP
- Each entry: `(timestamp, model_name, was_swap: bool)`
- On each request, computes swap ratio over the window
- Returns `ThrashingVerdict`: `ok | warn | halt`

**Swap determination:** the detector is fed actual swap events from the
scheduler (not just request model names), so it tracks real GPU swaps, not
just model diversity in the request stream.

### 6.3 Configuration

```yaml
scheduler:
  thrashing_detection:
    enabled: true
    mode: "warn"                # "warn" or "strict"
    window_size: 12             # last N requests per agent
    warn_swap_ratio: 0.5        # 6/12 swaps → ~4 swaps/min at typical rate
    halt_swap_ratio: 0.75       # 9/12 swaps → ~6 swaps/min (matches global critical)
    cooloff_seconds: 30         # halt duration before retrying
    min_requests_before_eval: 6 # don't judge until 6 requests seen
```

**Threshold rationale:**
- `warn_swap_ratio: 0.5` aligns with global warn threshold (4 swaps/min)
- `halt_swap_ratio: 0.75` aligns with global critical threshold (6 swaps/min)
- `min_requests_before_eval: 6` prevents false positives during legitimate
  startup sequences where an agent loads a few different models before settling

### 6.4 Response Behavior

**`ok`:** proceed normally, no extra headers.

**`warn`:** proceed, inject response header:
```
X-Swap-Penalty-Warning: swap_ratio=0.67; estimated_overhead_seconds=84; suggestion="batch requests by model to reduce swap penalties"
```

Estimated overhead computed from swap cost data:
- Large model swap: ~14s (12s load + 2s unload)
- Medium model swap: ~8.4s (7s load + 1.4s unload)
- Uses configured model VRAM to estimate swap cost

**`halt` (strict mode only):** reject with HTTP 429:
```json
{
  "error": "Pipeline suspended — swap thrashing detected",
  "swap_ratio": 0.83,
  "window_size": 12,
  "estimated_overhead_seconds": 112,
  "cooloff_seconds": 30,
  "suggestion": "Reorganize calls to batch by model. Current pattern causes ~14s GPU penalty per swap."
}
```

Agent enters cooloff period. Requests during cooloff receive the same 429 with
remaining cooloff time.

### 6.5 Integration Points

- **`proxy.py`**: call `detector.check(agent_id, model)` before enqueue;
  handle verdict (inject header or reject)
- **`scheduler.py`**: call `detector.record_swap(agent_id, from_model, to_model)`
  when actual model swaps occur
- **`server.py`**: instantiate `ThrashingDetector` in lifespan, pass to proxy
  and scheduler; add `thrashing_warnings` and `thrashing_halts` counts to
  `/broker/status` response

## 7. Scope Exclusions

| Item | Reason |
|------|--------|
| Batch-awareness / keep_alive tracking | Existing in-flight protection + Ollama's native keep_alive is sufficient |
| Changes to AffinityQueue | Model override before enqueue means requests flow to correct sub-queue already |
| Changes to eviction logic | Current VRAM manager handles new workloads correctly |
| New admin endpoints | Thrashing stats added to existing `/broker/status` |
| Streaming token trailing headers | Poor client support; audit log captures instead |

## 8. File Change Summary

| File | Change | Lines (est.) |
|------|--------|-------------|
| `models.py` | `ComplexityRoutingConfig`, `ThrashingDetectionConfig`, extend `BrokerConfig` | ~40 |
| `proxy.py` | Read header, override model, inject response headers, call detector | ~80 |
| `audit.py` | Extend `build_audit_event()` with new fields | ~20 |
| `thrashing.py` | **New** — `ThrashingDetector`, `ThrashingVerdict`, per-agent window | ~150 |
| `server.py` | Instantiate detector, wire to proxy/scheduler, extend `/broker/status` | ~30 |
| `scheduler.py` | Feed swap events to detector | ~10 |
| `config/broker.yaml` | `complexity_routing` section, `thrashing_detection` under `scheduler` | ~20 |
| `tests/test_complexity_routing.py` | **New** — routing logic, header injection, 422 rejection | ~120 |
| `tests/test_thrashing.py` | **New** — window management, ratio calc, verdicts, cooloff | ~150 |

**Estimated total:** ~620 lines across 9 files (2 new, 7 modified).

## 9. Testing Strategy

### Unit Tests

- **`test_complexity_routing.py`:**
  - Header present → model override applied correctly
  - Header absent → client model used (backward compat)
  - `complex` → HTTP 422 rejection
  - Invalid header value → ignored (client model used)
  - Routing disabled in config → passthrough
  - Response headers injected correctly (streaming + non-streaming)
  - Token count headers on non-streaming responses

- **`test_thrashing.py`:**
  - Window fills correctly per agent
  - Swap ratio calculated accurately
  - `min_requests_before_eval` respected — no verdict before threshold
  - `warn` verdict at correct ratio
  - `halt` verdict at correct ratio (strict mode only)
  - `halt` not issued in `warn` mode
  - Cooloff timer: requests rejected during cooloff, accepted after
  - Multiple agents tracked independently
  - Window slides correctly (old entries evicted)

### Integration Tests

- Proxy end-to-end: request with `X-Task-Complexity: simple` → correct model
  in `QueuedRequest`, response has `X-Model-Routed` header
- Audit log: `EVENT_REQUEST_COMPLETE` contains new fields
- Thrashing + scheduler: rapid alternating model requests → warn/halt emitted

## 10. Verification Checklist (from M58 Handoff)

- [ ] BASTION routes `X-Task-Complexity: simple` to configured fast model
- [ ] BASTION routes `X-Task-Complexity: moderate` to configured quality model
- [ ] BASTION rejects `X-Task-Complexity: complex` with HTTP 422
- [ ] Absent header = backward-compatible (client-selected model)
- [ ] `X-Broker-Priority: pipeline` requests are lower priority than interactive
- [ ] Audit log captures `X-Agent-Id` and `X-Task-Complexity`
- [ ] Model affinity: `keep_alive` respected (no force-unload during active batch)
- [ ] GPU safety: offloaded tasks don't cause VRAM overcommit during jury eval
- [ ] Circuit breaker: BASTION returns 503 (not hang) when models unavailable
- [ ] Thrashing detector warns at 50% swap ratio per agent
- [ ] Thrashing detector halts at 75% swap ratio in strict mode
- [ ] Response headers: `X-Model-Requested`, `X-Model-Routed`, `X-Routing-Reason`
- [ ] Token count headers on non-streaming responses
- [ ] Token counts in audit log for streaming responses
