# Known Issues — BASTION

This file tracks bugs surfaced by the v0.4 test-coverage campaign that
were *not* fixed in that campaign. Each item is a real finding with a
recommended fix path. They're shipped as known-issues rather than
unknown-unknowns because the cost-of-investigation has already been
paid — Phase 5 work just needs to apply the fix and write a regression
test.

Severity legend:
- **Critical** — can cause data loss, system corruption, or the exact
  hardware crash class (RTX 5090 model-load-cycle) BASTION exists to
  prevent.
- **Important** — degraded service or misleading state; reliability
  matters but doesn't crash anything.
- **Minor** — debuggability, hygiene, or undocumented invariant.

Fixes for the **Critical** items below should land in v0.4.1; the
remainder can be folded into v0.5 work.

---

## Critical

### `VRAMTracker.get_loaded_models()` returns `[]` indistinguishable from "no models loaded"

- **Location:** `src/bastion/vram.py:133-135`
- **Problem:** Every downstream consumer (`can_load_model`, `reconcile`,
  scheduler's `_evict_for_model`, VRAM budget check) treats `[]` as "VRAM
  is free." If `/api/ps` is transiently unreachable, the broker silently
  thinks all models are unloaded and may approve a load that exceeds the
  24 GB budget — the exact crash failure mode BASTION is built to prevent.
- **Fix path:** either propagate the exception or return a sentinel
  `None` and have callers skip budget enforcement when the state is
  unknown. The latter is less invasive but every caller needs auditing.
- **Surfaced by:** silent-failure-hunter audit, v0.4 campaign.

---

## Important

### `_queue_sweep_loop` grants events for swept requests without distinguishing them

- **Location:** `src/bastion/server.py:237-239`
- **Problem:** When a stale request is swept, `grant_evt.set()` unblocks
  the waiting proxy handler, which then proceeds to forward to Ollama as
  if it had been legitimately granted. The proxy has no signal that the
  request was *swept* rather than *granted*, so it dispatches to Ollama
  (incrementing in-flight counters) for a request the scheduler never
  intended to run.
- **Fix path:** introduce a `swept` flag or separate "rejected" event
  type so the proxy handler returns 503/504 instead of forwarding.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### Scheduler ignores `unload_model()` return value

- **Location:** `src/bastion/vram.py:285-287` + `src/bastion/scheduler.py::_unload_model`
- **Problem:** The 901c910 fix made `unload_model()` *honest* about
  whether VRAM converged. But the scheduler's `_unload_model` only logs
  the return value — it doesn't gate the subsequent load path. Eviction
  can silently fail and the scheduler may then attempt to load a new
  model into full VRAM.
- **Fix path:** check the return value in `_unload_model` and abort the
  swap when unload reports `False`.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### `_cleanup_inflight` background task has no exception handler

- **Location:** `src/bastion/server.py:365`
- **Problem:** The task is responsible for decrementing `_inflight_models`
  and calling `_scheduler.notify()`. If `_inflight_lock` is None
  unexpectedly or any context manager raises, the task dies silently. The
  inflight counter stays incremented forever, blocking the scheduler
  from evicting that model.
- **Fix path:** wrap the task body in `try/except Exception` and ensure
  `_inflight_models` decrement happens in a `finally` block.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### A2A `create_lease` has TOCTOU window with `has_active_lease`

- **Location:** `src/bastion/a2a.py:1479-1517`
- **Problem:** "Single grant per model" semantics are enforced by the
  caller pattern `if not has_active_lease: create_lease(...)`. Two
  concurrent callers can both pass `has_active_lease` False before
  either calls `create_lease`.
- **Fix path:** introduce `try_create_lease(model, ...)` that atomically
  checks + creates under an internal lock.
- **Surfaced by:** concurrency-test agent, v0.4 campaign.

### `CircuitBreakerTransport` ignores `RemoteProtocolError` and `PoolTimeout`

- **Location:** `src/bastion/circuitbreaker.py:267-269`
- **Problem:** The `except` clause explicitly catches `ConnectError`,
  `ConnectTimeout`, `ReadTimeout`. Other httpx exceptions
  (`RemoteProtocolError`, `PoolTimeout`, etc.) propagate without
  affecting the breaker counter — connection-pool pressure and protocol-
  level corruption never trigger the breaker.
- **Fix path:** broaden the except clause OR wrap with a general handler
  that classifies non-transient httpx errors as failures.
- **Surfaced by:** Agent 2 (failure GPU + Ollama), v0.4 campaign.

### Scheduler's GPU-hot gate doesn't guard mid-swap

- **Location:** `src/bastion/scheduler.py::_process_tick` (gate at top of tick)
- **Problem:** `check_gpu_safe` is called at the top of `_process_tick`
  but not re-checked inside `_handle_swap_dispatch` before the actual
  `_dispatch_for_model` call. A GPU that transitions hot during the swap
  is unprotected — exactly the load-cycle the system exists to prevent.
- **Fix path:** re-check `check_gpu_safe` immediately before the dispatch
  call inside the swap path.
- **Surfaced by:** Agent 2 (failure GPU + Ollama), v0.4 campaign.

### Dashboard `BastionClient` swallows all errors silently

- **Location:** `src/bastion/dashboard/client.py:34-89`
- **Problem:** Six methods (`get_recent`, `get_queue`, `get_health`,
  `get_vram_ledger`, `get_watchdog`, `get_counters`, `get_thrashing`)
  have `except Exception: return []` / `return {}` with no logging. The
  dashboard renders empty panels on any error — auth failure, 404, network
  partition — with no indication that data is absent vs. truly empty.
- **Fix path:** log at DEBUG level with endpoint name and exception type
  so the dashboard log captures the failure even if the UI doesn't.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### Proxy enqueue bare `except Exception` reports any failure as "queue full"

- **Location:** `src/bastion/proxy.py:284-290`
- **Problem:** Any unexpected exception from `_enqueue_fn` (programming
  error, attribute error, ...) is silently reported to the client as
  "Broker queue full." The log message says "Queue full" regardless of
  cause, with no traceback.
- **Fix path:** narrow the exception handler to `RuntimeError` (the
  queue-full signal), log non-RuntimeError cases at ERROR with
  `exc_info=True`.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### A2A `_handle_status` swallows handler exceptions without logging

- **Location:** `src/bastion/a2a.py:918-923`
- **Problem:** `except Exception` block sets `record.error = str(e)` and
  transitions to FAILED, but no `logger.error` / `logger.exception`. A
  bug in the status handler fails tasks silently.
- **Fix path:** add `logger.exception("A2A status handler error
  (task=%s)", record.task_id)` before the state transition.
- **Surfaced by:** silent-failure-hunter, v0.4 campaign.

### `TaskStore.create` is not lock-protected

- **Location:** `src/bastion/taskstore.py:147`
- **Problem:** Safe under asyncio's single-loop because the writes don't
  cross an `await` boundary, but the safety is undocumented and fragile.
  Any threaded caller (anyio thread pool, executor offload) races on
  `self._active[task_id]` and `_active_timestamps`.
- **Fix path:** either document the asyncio-only contract explicitly or
  add a `threading.Lock`. Concurrency tests added in v0.4 only exercise
  the asyncio path.
- **Surfaced by:** concurrency-test agent, v0.4 campaign.

---

## Minor

### `VRAMManager._reclaim_expired_sync()` called outside lock in `reconcile()` and `status()`

- **Location:** `src/bastion/vram.py:557, 608`
- **Problem:** Same pattern as the C2 fix landed in v0.4 (`reserve()`
  moved reclaim inside the lock), but `reconcile()` and `status()` still
  call it outside. Lower risk than `reserve()` because these are diagnostic
  paths not on the budget hot-path, but still incorrect under concurrent
  callers.
- **Fix path:** wrap each call in `async with self._lock`.

### `audit.emit()` is a global no-op before `init_audit_logger()` is called

- **Location:** `src/bastion/audit.py:342-346`
- **Problem:** Any audit event during startup before `init_audit_logger`
  completes is silently discarded. Startup window is short so this is
  minor, but startup-ordering bugs become invisible.
- **Fix path:** buffer pre-init events in a small ring buffer and flush
  on init; OR log at WARNING when emit fires pre-init.

### `_safe_transition` debug-logs invalid transitions

- **Location:** `src/bastion/a2a.py:447-458`
- **Problem:** `KeyError` (task not in active store) or invalid
  `ValueError` transition logs at DEBUG. If the task state machine enters
  an inconsistent state due to a race, no one notices unless DEBUG logging
  is enabled.
- **Fix path:** raise log level to WARNING for the ValueError branch
  (the KeyError branch — already-compacted — is fine at DEBUG).

### `ResidencyCache.invalidate()` is not lock-protected

- **Location:** `src/bastion/vram.py:92-95`
- **Problem:** Writes `self._cache_timestamp = 0.0` without holding
  `self._lock`. Safe for asyncio (CPython attribute writes are atomic)
  but the assumption is undocumented and would break under any future
  threading.
- **Fix path:** add a one-line comment documenting the asyncio-only
  contract, OR take the lock for symmetry with other mutations.

### DELETE `/a2a/tasks/{id}` returns 404 for already-terminal tasks

- **Location:** `src/bastion/server.py` (A2A delete route)
- **Problem:** 409 Conflict is arguably more semantically correct than
  404 Not Found for a task that *exists* but is in a terminal state. The
  current 404 also confuses retry logic in clients that expect 404 to
  mean "this never existed."
- **Fix path:** distinguish "never existed" (404) from "already terminal"
  (409 or 200 idempotent). Low priority unless an A2A client surfaces
  the confusion in practice.

### Dev `httpx` dep was unpinned (FIXED in v0.4 Phase 4)

- **Status:** Resolved. `pyproject.toml` dev now reads
  `httpx>=0.27,<1.0` matching the dashboard extra.

---

## Resolved in v0.4

For completeness — campaign-surfaced bugs that DID get fixed in the
test campaign itself:

- **Queue `effective_priority` precision** — `time.time()` called per
  invocation inside the dequeue loop, breaking FIFO at equal
  `submitted_at`. Hypothesis INV3 surfaced it; fix snapshots `now` once
  per dequeue pass. Regression test:
  `tests/test_queue.py::test_fifo_at_identical_submitted_at`.
- **`CircuitBreakerTransport.handle_async_request` HALF_OPEN promotion**
  — `_state` never materialized to HALF_OPEN before forwarding, so
  failed probes didn't reset `_opened_at`. Regression test:
  `tests/test_failure_gpu_ollama.py::test_half_open_probe_failure_reopens_circuit`.
- **`_queue_sweep_loop` silent task death** — no outer `try/except`.
  Now wrapped with logged backoff matching `scheduler._loop()`.
- **`VRAMManager.reserve()` reclaim race** — `_reclaim_expired_sync()`
  was called outside the lock, allowing concurrent reservers to
  double-decrement `_reserved`. Now called inside the lock.
