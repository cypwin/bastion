# Phase 5: BASTION Test Hardening

> **Use this prompt to start a clean Claude Code session with a 2-agent team.**

## Context

BASTION (Batch Affinity Scheduler for Throttled Inference on Ollama Networks) is a system-wide
GPU/LLM broker for NVIDIA GPUs. It sits as a transparent HTTP proxy on port 11434, forwarding
to Ollama on port 11435. The codebase is feature-complete after 5 implementation phases.

**What's complete:**
- Full proxy + scheduler + queue + VRAM management (21 source files in `src/bastion/`)
- A2A agent interface with SSE streaming, batch inference, reservations, hybrid leases
- Auth (API key + bearer token), rate limiting, circuit breaker
- Tiered audit logging, Prometheus metrics (no-op stubs), OpenTelemetry (no-op stubs)
- Watchdog process monitor with systemd sd_notify integration
- TaskStore dual-store architecture with compaction, TTL, backpressure
- Two-port architecture (proxy on 11434, admin+A2A on configurable admin_port)
- Textual TUI dashboard (14 panels, 1721 lines) with GPU, queue, A2A, circuit breaker views
- Documentation: `docs/api.md`, `docs/architecture.md`, `CLAUDE.md` (all current)
- 28 test files, 637+ tests collected, all passing

**What needs hardening:**
The test suite has broad coverage but specific gaps in three areas: scheduler internals
(swap rate limiter, concurrent dispatch edge cases), circuit breaker transport layer
(httpx transport integration, half-open probe serialization), and VRAM edge cases
(convergence timeout, reservation TTL expiry, double-release safety).

## Instructions

Spin up a **2-agent team** to work in parallel on the workstreams below. Each agent should:
1. Read `CLAUDE.md` first for project conventions
2. Run the full test suite at the start to confirm green baseline:
   `/home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -q`
3. Coordinate via the task list to avoid merge conflicts on shared files

---

## Workstream A — Scheduler & Circuit Breaker Tests (agent: `test-scheduler`)

### A1. Scheduler swap rate limiter tests
File: `tests/test_scheduler.py` (add to existing)

Test the dynamic swap rate limiter in `src/bastion/scheduler.py`:
- Swap rate under normal threshold (< 4/min) uses default 2s cooldown
- Swap rate at warn threshold (4-5/min) escalates to 5s cooldown
- Swap rate at critical threshold (>= 6/min) escalates to 10s cooldown
- Level transitions are logged (verify via caplog)
- Rolling window correctly ages out old swaps
- Rate limiter resets after cooldown period

### A2. Concurrent dispatch edge cases
File: `tests/test_scheduler.py` or `tests/test_concurrency.py` (add to existing)

Test the two-phase dispatch in `src/bastion/scheduler.py` and `src/bastion/server.py`:
- Phase 1 (non-blocking): co-resident models dispatch concurrently up to `max_concurrent_dispatches`
- Phase 1: same-model in-flight request serializes (not concurrent)
- Phase 2 (blocking): non-resident model swap serializes dispatch
- `has_inflight()` returns correct state during and after dispatch
- `inflight_count()` tracks total across models
- `_cleanup_inflight()` fires correctly on non-blocking path timeout

### A3. Circuit breaker transport tests
File: `tests/test_circuitbreaker.py` (add to existing)

Test `CircuitBreakerTransport` in `src/bastion/circuitbreaker.py`:
- Transport wraps httpx calls with circuit breaker check/record
- `CircuitOpenError` is raised when circuit is open
- Half-open probe serialization: only one probe request at a time
- Transport correctly records success/failure for state transitions
- A2A handler receives `CircuitOpenError` and transitions task to failed
- JSON-RPC -32050 error returned when circuit is open during `create_task`

### A4. Scheduler model eviction tests
File: `tests/test_scheduler.py` (add to existing)

- Eviction skips models with in-flight requests
- Eviction skips models with active reservations (`has_active_reservation`)
- Eviction skips models with active leases (`has_active_lease`)
- Least-useful model is selected for eviction
- VRAM reservation is created before eviction and committed/released correctly

---

## Workstream B — VRAM & Store Edge Cases (agent: `test-vram`)

### B1. VRAM edge case tests
File: `tests/test_vram_manager.py` (add to existing)

Test `VRAMManager` in `src/bastion/vram.py`:
- Reserve when exactly at budget limit (should fail)
- Reserve when 1 byte under budget (should succeed)
- Double-release of same reservation (should not crash or double-free)
- Concurrent reserve calls serialize via semaphore
- Reservation TTL expiry reclaims VRAM
- `wait_for_vram_convergence()` timeout path (nvidia-smi never converges)
- `wait_for_vram_convergence()` success path (VRAM delta < 1 MB)
- Status dict includes all reservation details

### B2. TaskStore stress tests
File: `tests/test_taskstore.py` (add to existing)

- 100 concurrent `create()` calls don't corrupt internal state
- Backpressure transitions: NORMAL -> PRESSURE -> OVERLOADED -> PRESSURE -> NORMAL
- Hysteresis: PRESSURE -> NORMAL requires < 70% (not just < 80%)
- `TaskStoreFullError` at capacity with correct `retry_after`
- Completed store FIFO eviction when exceeding `completed_maxsize`
- Tombstone capacity enforcement
- Periodic sweep correctly cleans all three stores
- SSE subscriber cleanup for orphaned tasks

### B3. Safe transition race tests
File: `tests/test_taskstore.py` or `tests/test_a2a.py` (add to existing)

- `_safe_transition` after task already compacted (cancel + complete race)
- `_safe_transition` with invalid transition (e.g., `completed -> working`)
- `_safe_transition` with unknown task_id
- `update_state` correctly validates transition map
- `CompactedResult.from_record` preserves artifacts as immutable tuple
- `CompactedResult.from_record` extracts text summary (truncated to 500 chars)
- `CompactedResult.from_record` handles missing/empty artifacts

### B4. VRAM tracker edge cases
File: `tests/test_vram.py` (add to existing)

- `can_load_model` with unknown model (uses `default_vram_estimate_gb`)
- `can_load_model` when Ollama `/api/ps` is unreachable
- `get_loaded_models` when Ollama returns empty list
- `unload_model` polling timeout (model never leaves `/api/ps`)
- VRAM tracker close cleans up httpx client

---

## Coordination Notes

- **Shared files**: Both agents write different test files. No merge conflicts expected.
- **Test baseline**: Both agents should verify 637+ tests passing before and after their changes.
- **Commit convention**: `test(S12): description`
- **Python path**: `/home/user/miniforge3/envs/bastion/bin/python`
- **Never delete files** -- archive to `_archive/` if replacing
- **Type hints required** on all new functions
- **`from __future__ import annotations`** in every new `.py` file
- **Never run tests automatically** -- print the pytest command for the user
