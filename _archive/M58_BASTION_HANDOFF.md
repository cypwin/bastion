# M58 BASTION Handoff Report

> Comprehensive handoff from the-batch-client to the BASTION repo for implementing
> the BASTION-side requirements of Milestone 58: Smart Local Offloading.
>
> **Generated:** 2026-04-19
> **Updated:** 2026-04-19 (post-implementation — all D0-D5 complete)
> **Status:** ALL DELIVERABLES IMPLEMENTED (D0 merged, D1-D5 code written, pending commit)
> **the-batch-client source docs:**
> - Design spec: `docs/design/specs/2026-04-19-m58-smart-offloading-design.md`
> - D0 plan: `docs/design/plans/2026-04-19-m58-d0-batch-llm-calls.md`
> - D1-D5 plans: `docs/design/plans/2026-04-19-m58-d{1..5}-*.md`
> - ADR: `docs/adr/ADR-004-bastion-complexity-routing.md`
> - ROADMAP entry: `ROADMAP/ROADMAP.md` section "M58: Smart Local Offloading via BASTION"
> - Session log: `ROADMAP/session_logs/S91_m58_d0_batch_llm_calls.md`

---

## 1. Executive Summary

**M58 ("Smart Local Offloading via BASTION")** reduces Claude API token consumption
by 20-40% by systematically offloading sub-tasks to local LLMs via the BASTION
proxy. Claude remains the agent brain for complex reasoning and multi-tool
orchestration; local models handle evaluation, summarization, classification, and
monitoring tasks.

### What BASTION needs to implement

1. **Read `X-Task-Complexity` header** on incoming requests and route to
   appropriate models (D4 — the primary BASTION deliverable)
2. **Circuit breaker / graceful degradation** — return appropriate HTTP errors
   when models are unavailable so clients can fall back
3. **Audit logging** for offloaded requests — track which agents offload what,
   with what quality
4. **Model affinity** — keep summarization models resident when multiple agents
   are offloading within a short window
5. **Adjust slot management expectations** — D0 changed call patterns from many
   short slots to fewer, longer-held slots

---

## 2. M58 Deliverable Overview

| Deliverable | Summary | Status | BASTION Impact |
|-------------|---------|--------|----------------|
| **D0** | Batch LLM calls — `generate_batch()` | **COMPLETE** (merged to main, commit `6bab8b5e`) | Fewer but longer VRAM slot holds |
| **D1** | `summarize_for_context` MCP tool | **COMPLETE** (6th tool in `ollama-server/server.py`, 30K truncation guard) | New workload: code/prose summarization |
| **D2** | Auto-summary checkpoints at 50K token intervals | **COMPLETE** (`ContextGauge` + `AutoSummaryHandler`, stores as memory + .md) | New workload: periodic checkpoint summaries |
| **D3** | LLM-assisted monitoring (stagnation + advisory) | **COMPLETE** (`_llm_classify()` + `_compose_advisory_llm()`, `EnrichmentBundle.advisory` field) | New workload: classification + advisory composition |
| **D4** | `X-Task-Complexity` header routing | **COMPLETE** (header on `generate`/`chat`/`generate_batch` + MCP tools, `complexity_score_to_header()`, ADR-004) | **PRIMARY BASTION DELIVERABLE** |
| **D5** | Offloading telemetry | **COMPLETE** (`OffloadTracker` + `OffloadEvent`, `TelemetryWriter` integration, `/api/offload/summary` endpoint) | Needs BASTION audit log access |

**Tests:** 5 new test files (~85 test functions): `test_mcp_ollama.py`, `test_m58_d2_context_gauge.py`, `test_m58_d3_monitoring.py`, `test_m58_d4_ollama_client.py`, `test_m58_d5_offload_tracker.py`

**Commit status:** D0 merged to main. D1-D5 code is written on main working tree (uncommitted) — parallel agents wrote to main checkout due to worktree tool permission constraints. Pending commit + merge.

---

## 3. D0: Batch LLM Calls (COMPLETE) — Impact on BASTION

### What changed

Before D0, every LLM procedure called `ollama.generate()` once per item. For a
jury council evaluating 10 memories with 6 juror models, that was 60 individual
VRAM slot acquires. After D0, the pattern is **model-first batching**:

```
BEFORE (memory-first, N*6 slot acquires):
  memory_1: [granite.generate(), llama.generate(), mistral.generate(), ...]
  memory_2: [granite.generate(), llama.generate(), mistral.generate(), ...]
  ...
  = N * 6 VRAM slot round-trips

AFTER (model-first, 6 slot acquires total):
  granite:  generate_batch([prompt_mem1, prompt_mem2, ...])  -> 1 slot acquire
  llama:    generate_batch([prompt_mem1, prompt_mem2, ...])  -> 1 slot acquire
  mistral:  generate_batch([prompt_mem1, prompt_mem2, ...])  -> 1 slot acquire
  ...
  = 6 VRAM slot acquires (independent of N memories)
```

### Files modified (all in the-batch-client)

| File | Change |
|------|--------|
| `peers/swarm-memory/swarm_memory/server/jury.py` | New `_council_vote_batch()`, batch `_check_relevance_self_consistency()` |
| `peers/swarm-memory/swarm_memory/pathway_discovery.py` | `label_all_features()` 4-pass batch pattern |
| `peers/swarm-memory/swarm_memory/consolidation.py` | Session synthesis Phase 2 batched |
| `peers/swarm-orchestrator/swarm_orchestrator/rsi/research_processor.py` | `_llm_decompose_batch()` for N>=3 reports |

### BASTION implications

- **Longer slot holds**: Each model now holds its VRAM slot for the duration of
  processing ALL items in the batch (e.g., all 10 memories), not just one.
  Typical hold time went from ~5s to ~30-120s depending on batch size.
- **`keep_alive="5m"` or `"10m"`**: All batch calls set `keep_alive` to prevent
  model unloading between prompts within the same batch. BASTION should respect
  this and not force-unload during active batches.
- **Fewer model swaps**: With model-first batching, each model stays loaded for
  the full batch. The ThreadPoolExecutor still runs juror models in parallel
  (up to 6 threads), but each thread holds its slot longer.
- **Expected pattern**: 6 concurrent slot acquires (one per juror model), each
  held for N * ~5s, rather than N * 6 sequential short acquires.

---

## 4. D4: BASTION Routing Awareness (PRIMARY BASTION DELIVERABLE)

This is the deliverable that requires BASTION-side implementation.

### New Header: `X-Task-Complexity`

**Client-side** (implemented in the-batch-client, verified in code):

```
X-Task-Complexity: simple | moderate | complex
```

The header is set on every outgoing Ollama request from `OllamaClient` in
`peers/swarm-memory/swarm_memory/ollama.py`. The complexity value comes from the
`ComplexityScorer` in `swarm_orchestrator/model_router.py`, which maps a 0.0-1.0
score to the header:

```python
def complexity_score_to_header(score: float) -> str:
    if score < 0.30:
        return "simple"      # deterministic + ollama tiers
    elif score <= 0.70:
        return "moderate"    # haiku + sonnet tiers
    else:
        return "complex"     # opus tier -> should go to Claude
```

**Note:** The client-side also rejects `complex` before sending to BASTION
(returns None with a warning log). But BASTION should also enforce this as a
safety net.

### Required BASTION Routing Rules

| `X-Task-Complexity` | Model to Route To | VRAM | Rationale |
|---------------------|-------------------|------|-----------|
| `simple` | `qwen3.5:9b` (or `qwen3:8b` ~5.5 GB) | ~6 GB | Classification, HyDE, fast extraction |
| `moderate` | `qwen3.5:35b-a3b` (or `qwen3:30b-a3b` ~19.5 GB) | ~20 GB | Evaluation, composition, summarization |
| `complex` | **REJECT with HTTP 422** | N/A | Must go to Claude, not local model |
| *(absent)* | Existing behavior (client-selected model) | varies | Backward compatibility — no header = no routing |

**HTTP 422 response format** (suggested):
```json
{
  "error": "Task complexity 'complex' requires Claude, not local model. Route to API.",
  "complexity": "complex"
}
```

### Existing Headers Already in Use

These headers are already being sent by the-batch-client clients. BASTION should
continue respecting them alongside the new `X-Task-Complexity`:

| Header | Introduced | Purpose | Example Values |
|--------|-----------|---------|----------------|
| `X-Broker-Priority` | Pre-M56 | Request priority scheduling | `pipeline`, `interactive` |
| `X-Agent-Id` | M56 | Per-agent tracking and audit | `jury_agent`, `extract_agent` |
| `X-Task-Complexity` | **M58-D4 (new)** | Model routing by task complexity | `simple`, `moderate`, `complex` |

### Header Interaction Rules

1. `X-Task-Complexity` determines **which model** to use
2. `X-Broker-Priority` determines **queue priority** (pipeline < interactive)
3. `X-Agent-Id` is for **audit/tracking** only
4. If `X-Task-Complexity` is absent, BASTION uses the client-requested model
   (backward compatible)
5. If `X-Task-Complexity` conflicts with the client-requested model (e.g.,
   `simple` but client requests `qwen3:30b`), BASTION should **override** with
   the complexity-appropriate model (or at minimum log a warning)

---

## 5. New Workloads Introduced by D1-D3

### D1: Summarization (`summarize_for_context`) — IMPLEMENTED

- **New MCP tool** in `ollama-server/server.py` (Tool 6, line ~376)
- **Input**: Raw text (`context` param, up to 30K chars) + focus `query` string
- **Output**: ~500-token focused digest (system prompt: "Focus ONLY on aspects relevant to the user's query")
- **Default model**: `OLLAMA_DEFAULT_MODEL` env var (falls back to `qwen2.5:32b`)
- **Frequency**: 5-10 times per agent session
- **BASTION load**: Low — short, infrequent requests
- **Expected header**: `X-Task-Complexity: moderate` (for code), `simple` (for prose)
- **Truncation guard**: Inputs > 30K chars (`MAX_CONTEXT_CHARS`) are truncated; `truncated: true` in response
- **Error handling**: Returns `{"status": "error", "error": "..."}` for empty inputs

### D2: Auto-Summary Checkpoints — IMPLEMENTED

- **`ContextGauge`**: `peers/swarm-orchestrator/swarm_orchestrator/auto/context_gauge.py` ��� fires `on_threshold(gauge, checkpoint_number)` at 50K token intervals (configurable). Safe: exceptions in callbacks are caught and logged.
- **`AutoSummaryHandler`**: `peers/swarm-orchestrator/swarm_orchestrator/auto/auto_summary.py` — on threshold, generates summary via local model, stores as memory (tier 1, session scope) + writes checkpoint `.md` file.
- **Model**: `qwen3.5:9b` (fast tier, configurable)
- **Input**: Recent tool calls + outputs (~2-5K tokens)
- **Output**: 3-5 sentence summary (~256 tokens)
- **Frequency**: ~1-3 times per session per agent
- **BASTION load**: Very low
- **Expected header**: `X-Task-Complexity: simple`

### D3: Monitoring Sub-Tasks — IMPLEMENTED

Two new workloads, both background (zero Claude tokens):

**Stagnation Classification** (`StagnationDetector._llm_classify()` at `stagnation.py:323`):
- **Trigger**: Called when heuristic detection finds ambiguous patterns
- **Model**: configurable via `_classify_model` (default fast tier)
- **Input**: ~10 recent state entries (~500 tokens)
- **Output**: Single classification label (~20 tokens)
- **Frequency**: Rare (only ambiguous stagnation events)
- **Expected header**: `X-Task-Complexity: simple`
- **Graceful degradation**: If `self._ollama` is None, returns None (heuristic-only mode)

**Memory Advisory Composition** (`MemoryAdvisor._compose_advisory_llm()` at `advisor.py:132`):
- **Trigger**: `MemoryAdvisor.advise()` called before subagent dispatch
- **New field**: `EnrichmentBundle.advisory` — natural-language advisory text surfaced in `to_prompt_injection()`
- **Model**: configurable (default fast tier)
- **Input**: Task description + top 10 retrieved memories (~1.5K tokens)
- **Output**: 3-5 sentence advisory (~256 tokens)
- **Frequency**: Once per subagent spawn (5-20 per session)
- **Expected header**: `X-Task-Complexity: moderate`
- **Priority header**: `X-Broker-Priority: pipeline` (lower than interactive)
- **Graceful degradation**: Falls back to template-based composition if Ollama unavailable

---

## 6. D5: Telemetry Requirements from BASTION

### What the-batch-client tracks

`OffloadTracker` (`peers/swarm-orchestrator/swarm_orchestrator/telemetry/offload_tracker.py`) records every offloaded call. Dashboard endpoint at `GET /api/offload/summary` returns live session aggregation (in `dashboard/server.py:1237`):

```python
@dataclass
class OffloadEvent:
    timestamp: float
    task_type: str           # "summarize", "classify", "advisory", "checkpoint", "jury_batch"
    offloaded_to: str        # model name as reported by BASTION
    input_tokens_est: int    # chars / 4
    output_tokens_est: int
    tokens_saved_est: int    # what Claude would have processed
    latency_ms: float
    quality_score: Optional[float]
    agent_id: Optional[str]
    session_id: Optional[str]
```

### What BASTION could provide (optional but valuable)

- **Audit log export**: Which models actually served which requests (to validate
  that complexity routing is working correctly)
- **Actual token counts**: BASTION has access to Ollama's `prompt_eval_count`
  and `eval_count` from response metadata. Exposing these in response headers
  (e.g., `X-Prompt-Tokens`, `X-Completion-Tokens`) would replace the
  client-side `chars/4` heuristic with exact counts.
- **Model swap events**: When BASTION had to swap models to serve a request,
  logging this would help identify model affinity optimization opportunities.

---

## 7. VRAM Coexistence & Model Affinity

### Current VRAM Profiles (RTX 5090, 32 GB, ~26 GB budget)

From `docs/BASTION_LLM_INVENTORY.md`:

| Profile | Models | Measured VRAM | Headroom |
|---------|--------|---------------|----------|
| `council` | granite + llama + mistral-nemo + nomic | ~20.6 GB | ~11.4 GB |
| `council+backup` | council + qwen2.5-coder | ~26.3 GB | ~5.7 GB |
| `primary` | qwen3:30b + nomic | ~20.9 GB | ~11.1 GB |
| `fast_batch` | qwen3:14b + qwen3:8b | ~15 GB | |
| `extraction_pair` | qwen3:30b + nuextract | ~22 GB | |

`nomic-embed-text` (0.4 GB) coexists with ALL profiles.

### New M58 Model Affinity Considerations

With M58 offloading, BASTION will see new patterns:

1. **Summarization bursts**: Multiple `summarize_for_context` calls in quick
   succession (agent reading several large files). Keep `qwen3-coder:30b`
   resident during these bursts (client sets `keep_alive="5m"`).

2. **Monitoring background**: `qwen3.5:9b` used intermittently for
   classification and advisory. Since it's small (~6 GB), consider keeping it
   always-resident alongside the embedding model (~7 GB total).

3. **Jury + offloading conflict**: Jury council needs 3-4 models simultaneously
   (~20-26 GB). During jury evaluation, offloading requests should be queued
   (lower priority via `X-Broker-Priority: pipeline`) rather than causing
   model swaps.

---

## 8. Graceful Degradation Contract

Every offloading call in the-batch-client wraps the Ollama request in a try/except:

```python
try:
    result = ollama.generate(prompt=..., model=..., keep_alive="5m")
except Exception:
    logger.warning("Local model unavailable, falling back to Claude/skip")
    result = None  # or fallback
```

**BASTION's responsibility:**
- Return proper HTTP error codes (422 for complex, 503 for unavailable)
- Never hang — use timeouts so the client can fall back
- If BASTION itself is down, the client's `httpx` timeout (default 300s) kicks in

**Client-side guarantees:**
- All offloading is optional — if BASTION returns an error, the operation
  either falls back to Claude or is skipped gracefully
- No offloading call is in the critical path — agent sessions function without
  local models (just with higher Claude token usage)

---

## 9. Security Integration (M56 Foundation)

M58 builds on M56's security hardening:

- **All requests go through BASTION** (port 11434), never direct Ollama
- **`X-Agent-Id` header** set on all requests (added in M56)
- **BASTION audit logging** tracks which agents offload what
- **GPU safety gating**: BASTION should prevent offloaded tasks from
  overheating GPU during heavy pipeline runs (existing capability)
- **`use_mmap: false`**: Always set in request options to prevent PCIe DMA
  stress on RTX 5090 Blackwell GPU. BASTION should not override this.

---

## 10. Verification Criteria (BASTION-side)

These are the BASTION-side items from the M58 verification checklist in the
ROADMAP:

- [ ] BASTION routes `X-Task-Complexity: simple` to `qwen3.5:9b` (or configured fast model)
- [ ] BASTION routes `X-Task-Complexity: moderate` to `qwen3.5:35b-a3b` (or configured quality model)
- [ ] BASTION rejects `X-Task-Complexity: complex` with HTTP 422
- [ ] Absent `X-Task-Complexity` header = backward-compatible (client-selected model)
- [ ] `X-Broker-Priority: pipeline` requests are lower priority than interactive
- [ ] Audit log captures `X-Agent-Id` and `X-Task-Complexity` for all offloaded requests
- [ ] Model affinity: `keep_alive` is respected (no force-unload during active batch)
- [ ] GPU safety: offloaded tasks don't cause VRAM overcommit during jury evaluation
- [ ] Circuit breaker: BASTION returns 503 (not hang) when models are unavailable
- [ ] When BASTION is down, client-side timeout triggers graceful degradation

---

## 11. Execution Recommendation

### For BASTION implementation

1. **Start with `X-Task-Complexity` routing** (D4 counterpart) — this is the
   core BASTION change and can be implemented independently of the-batch-client's
   D1-D3 work
2. **Add complexity-based model routing rules** — read header, select model
3. **Add 422 rejection for `complex`** — safety net
4. **Update audit logging** to capture the new header
5. **Test model affinity** during batch operations — ensure `keep_alive` is
   respected

### Integration testing

All the-batch-client client-side code is implemented. To test end-to-end:

1. **Commit + merge** D1-D5 code in the-batch-client (currently uncommitted on main working tree)
2. **Implement BASTION routing** — read `X-Task-Complexity`, route to models
3. **Integration test**: Send request with `X-Task-Complexity: simple` through
   BASTION, verify it reaches `qwen3.5:9b` (not the client-requested model)
4. **Run the-batch-client test suite**: `pytest tests/test_m58_d*.py tests/test_mcp_ollama.py -v`

### What BASTION can do immediately

1. Implement `X-Task-Complexity` routing — client code is ready, header will
   appear on all requests once the-batch-client D1-D5 are committed
2. Optimize slot management for longer batch holds (D0 already on main)
3. Expose `X-Prompt-Tokens` / `X-Completion-Tokens` response headers (useful for D5 telemetry)
4. Add per-type avg latency tracking keyed by `X-Task-Complexity` value

---

## 12. Reference: the-batch-client File Locations

| Purpose | File Path |
|---------|-----------|
| OllamaClient (HTTP wrapper, headers, `task_complexity`) | `peers/swarm-memory/swarm_memory/ollama.py` |
| `generate_batch()` implementation | `ollama.py` lines 351-426 |
| `complexity_score_to_header()` helper | `ollama.py` line 90 |
| Jury council (model-first batching) | `peers/swarm-memory/swarm_memory/server/jury.py` |
| ComplexityScorer (0.0-1.0 scoring) | `peers/swarm-orchestrator/swarm_orchestrator/model_router.py` |
| Ollama MCP server (6 tools incl. `summarize_for_context`) | `ollama-server/server.py` |
| ContextGauge (D2) | `peers/swarm-orchestrator/swarm_orchestrator/auto/context_gauge.py` |
| AutoSummaryHandler (D2) | `peers/swarm-orchestrator/swarm_orchestrator/auto/auto_summary.py` |
| StagnationDetector + `_llm_classify()` (D3) | `peers/swarm-orchestrator/swarm_orchestrator/stagnation.py` |
| MemoryAdvisor + `_compose_advisory_llm()` (D3) | `peers/swarm-orchestrator/swarm_orchestrator/advisor.py` |
| OffloadTracker + OffloadEvent (D5) | `peers/swarm-orchestrator/swarm_orchestrator/telemetry/offload_tracker.py` |
| Dashboard `/api/offload/summary` endpoint (D5) | `peers/swarm-orchestrator/swarm_orchestrator/dashboard/server.py` line 1237 |
| TelemetryWriter (D5 foundation) | `peers/swarm-orchestrator/swarm_orchestrator/telemetry/jsonl_writer.py` |
| ADR-004 (BASTION complexity routing) | `docs/adr/ADR-004-bastion-complexity-routing.md` |
| BASTION LLM inventory (models, VRAM) | `docs/BASTION_LLM_INVENTORY.md` |
| M58 design spec | `docs/design/specs/2026-04-19-m58-smart-offloading-design.md` |
| M56 BASTION client tests | `tests/test_m56_bastion_client.py` |
| M58 D2 tests (ContextGauge) | `tests/test_m58_d2_context_gauge.py` |
| M58 D3 tests (monitoring) | `tests/test_m58_d3_monitoring.py` |
| M58 D4 tests (OllamaClient headers) | `tests/test_m58_d4_ollama_client.py` |
| M58 D5 tests (OffloadTracker) | `tests/test_m58_d5_offload_tracker.py` |
| D1 tests (summarize_for_context) | `tests/test_mcp_ollama.py` |

---

## 13. Appendix: Design Principles (from spec)

1. **Claude is the brain, local models are the hands.** Claude handles tool
   orchestration, multi-file reasoning, and architectural decisions. Local
   models handle evaluation, summarization, classification, and extraction.
2. **Graceful degradation.** If BASTION/Ollama is unavailable, fall back to
   Claude or skip the operation. Never crash.
3. **Measure before optimizing.** D5 telemetry must track token savings and
   quality scores to validate that offloading is worth it.
4. **Reuse existing infrastructure.** The project has StagnationDetector,
   MemoryAdvisor, ModelRouter, TelemetryWriter, and CheckpointManager. Build
   on these, don't duplicate.

---

## 14. Implementation Deviations from Plans

All implementations closely follow the design spec. Notable deviations:

| Aspect | Plan | Implementation | Impact on BASTION |
|--------|------|----------------|-------------------|
| D1 default model | `qwen3-coder:30b` for code, `qwen3.5:9b` for prose | Single default via `OLLAMA_DEFAULT_MODEL` env var (no code/prose split) | BASTION complexity routing handles model selection instead |
| D2 wiring | Hook into `auto_trigger.py` or `autonomy_manager.py` | `AutoSummaryHandler` is standalone; wiring to trigger system deferred | No BASTION impact |
| D3 MemoryAdvisor | Advisory as separate output | `EnrichmentBundle.advisory` field + surfaced in `to_prompt_injection()` | No BASTION impact — richer context for agents |
| D4 complex rejection | Client returns None for `generate()`, error dict for MCP tools | Exactly as planned | BASTION should still reject 422 as safety net |
| D5 dashboard | New Workbench tab panel | REST endpoint only (`/api/offload/summary`), no dedicated tab | No BASTION impact |
| Parallel execution | 5 worktree branches | All written to main checkout (tool permission constraints in background agents) | No BASTION impact |

**Known issue**: Background subagents can't get tool permissions in this
environment. Future parallel implementation sessions should use Docker
orchestrator. This is a process note, not a code issue.

---

*Document complete. All M58 D0-D5 implementations verified against source code
as of 2026-04-19. BASTION can proceed with routing implementation.*
