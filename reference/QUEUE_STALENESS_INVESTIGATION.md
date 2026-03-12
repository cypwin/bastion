# Queue Staleness Investigation — MCP Tool Latency

**Date:** 2026-03-09
**Investigator:** Claude Code (S14)
**Symptom:** BASTION queue becomes stale, causing significant lag for MCP tool use.

## Summary

Four root causes identified in the scheduler/queue/proxy pipeline that
contribute to request latency, particularly for MCP tool calls. Issues are
ordered by impact severity.

---

## Issue 1: Synchronous nvidia-smi blocks the asyncio event loop (CRITICAL)

**Files:** `src/bastion/health.py:32-42`, called from `scheduler.py:260`,
`vram.py:178`, `vram.py:206`, `vram.py:284`, `vram.py:492-496`

**Problem:** `query_gpu_status()` uses `subprocess.run()` — a synchronous
blocking call. When invoked from an async context (the scheduler loop, VRAM
tracker), it blocks the **entire asyncio event loop**, not just the calling
coroutine. All concurrent HTTP handling, proxy streaming, queue operations,
and socket I/O freeze until the subprocess returns.

**Call frequency:** Every scheduler tick (`_process_tick`), plus 4-6+ times
during a model swap path:

| Call site | nvidia-smi calls | Context |
|-----------|-------------------|---------|
| `_process_tick()` → `check_gpu_safe()` | 1 | Every tick |
| `log_vram_snapshot("pre_swap")` | 1 | Per swap |
| `can_load_model()` | 1-2 | Per swap (temp check + free VRAM check) |
| `log_vram_snapshot("model_unload")` | 1 per eviction | Per evicted model |
| `wait_for_vram_convergence()` | 2-20 (polling) | Per eviction |

**Latency impact:** Each nvidia-smi call takes ~50-200ms on a healthy system,
up to 5s on timeout. A swap with one eviction blocks the event loop for
**1-3 seconds cumulatively**. MCP requests arriving during this time cannot
even be read from the socket.

**Fix:** Convert `subprocess.run()` → `asyncio.create_subprocess_exec()`.
Convert `query_gpu_status()` and `get_vram_free_gb()` to async. Update all
callers. Also add `check_gpu_safe()` as async wrapper.

**Status:** FIXED (S14)

---

## Issue 2: Scheduler blocks for full inference duration on swap path (MAJOR)

**Files:** `src/bastion/server.py:226-239`

**Problem:** When `needs_swap=True` or same-model serialization applies,
`_dispatch_request()` blocks the scheduler loop on `done_event.wait()` for
the entire inference duration (potentially 30-300 seconds):

```python
if should_block:
    await asyncio.wait_for(done_event.wait(), timeout=timeout)
```

During this time, the scheduler cannot pick any new requests from the queue.
Any MCP request enqueued during a blocking dispatch sits idle.

**Design rationale:** Serializes GPU access during model swaps to prevent
PCIe power transient crashes on the GPU. The swap path must complete
(model loaded + inference done) before the scheduler can issue another swap.

**Latency impact:** Queue frozen for 30-300s per blocking inference. Co-resident
model requests could theoretically be dispatched during this time but are not
because the scheduler loop is awaiting the done event.

**Potential fix:** Decouple the scheduler loop from blocking dispatch. The
scheduler could grant the request and track the blocking constraint separately
(e.g., a "swap lock" that prevents new swaps but allows co-resident dispatch).
Requires careful design to maintain crash safety guarantees.

**Status:** OPEN — requires design discussion.

---

## Issue 3: Missing scheduler wake after non-blocking dispatch completes (MODERATE)

**Files:** `src/bastion/server.py:255-276`

**Problem:** `_cleanup_inflight()` (the background task for non-blocking
dispatches) decrements the in-flight counter when inference completes but
**never calls `_scheduler.notify()`**. The scheduler only discovers the freed
slot on its next `loop_interval_seconds` timeout (0.1s).

**Latency impact:** 0-100ms unnecessary delay per request for same-model
serialized requests waiting behind a co-resident inference.

**Fix:** Add `_scheduler.notify()` call in `_cleanup_inflight()` after
decrementing the inflight counter.

**Status:** FIXED (S14)

---

## Issue 4: MCP tool calls deprioritized by default (DESIGN)

**Files:** `src/bastion/proxy.py:370-400`

**Problem:** MCP clients (e.g., Claude Code tool use via Ollama) typically
don't set the `X-Broker-Priority` header, and their User-Agent doesn't
contain "ollama". They are classified as `AGENT` priority (base 50), while
interactive `ollama run` gets `INTERACTIVE` priority (base 100).

With `aging_rate=2.0`, an MCP request needs to wait **25 seconds** in the
queue before its effective priority matches a freshly-enqueued interactive
request.

**Design rationale:** Interactive terminal sessions (human typing) should
feel responsive. Agent/MCP requests are typically part of automated pipelines
where 25 seconds of aging is acceptable.

**Potential fix options:**
1. Allow MCP servers to set `X-Broker-Priority: interactive` in their config.
2. Add User-Agent pattern matching for known MCP clients.
3. Add a configurable `default_priority` in broker.yaml.
4. Reduce the priority gap (e.g., agent=75 instead of 50).

**Status:** OPEN — requires design discussion.

---

## Combined Worst-Case Scenario

MCP tool call arrives while a model swap + inference is in progress:

1. **Event loop blocked** by nvidia-smi subprocess (~200ms) — request can't
   even be read from socket.
2. **Scheduler blocked** on swap-path `done_event.wait()` — request enqueued
   but scheduler can't process it (30-300s).
3. **Swap completes**, scheduler wakes, but another model's request has higher
   priority due to aging — MCP request waits another tick.
4. **Scheduler wakes**, runs `check_gpu_safe()` — another nvidia-smi subprocess
   blocks event loop (~200ms).
5. **MCP request finally dispatched** — total latency: 30-300s queue wait +
   ~1s event loop blocking.

After fixes for issues #1 and #3, the event loop blocking is eliminated and
co-resident dispatch latency drops to near-zero. Issues #2 and #4 remain as
design discussions.

---

## Additional Fixes (S15: Queue Staleness & Stall Diagnostics)

The following fixes address ghost request accumulation, queue staleness, and
lack of diagnostics that caused the 58-pending-request incident:

| Fix | Bug | Status |
|-----|-----|--------|
| D | Drain mode doesn't reject new enqueues | FIXED (S15) |
| A | Grant timeout leaves ghost in queue + _pending_grants | FIXED (S15) |
| C | Dispatch exception silently drops request (dequeued, never served) | FIXED (S15) |
| B | Queue has zero TTL — requests sit forever | FIXED (S15) — 600s TTL, 60s sweep |
| E | No logging/UI explaining WHY scheduler can't dispatch | FIXED (S15) — stall diagnostics |
