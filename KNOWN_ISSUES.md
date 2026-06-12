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

_(none open — see "Resolved in v0.4.1" below)_

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

### A2A `_handle_status` swallows handler exceptions without logging (FIXED in v0.4.1)

- **Status:** Resolved (S130). The `except` block now calls
  `logger.exception("A2A status handler error (task=%s)", ...)` before the
  FAILED transition, and the handler no longer crashes on the VRAM
  state-unknown sentinel in the first place (answers with
  `vram_state: "unknown"`).
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

### Unauthenticated admin surface discloses build/config metadata (accepted risk pending ADR-006)

- **Location:** `GET /broker/version` (git SHA, boot time), `GET /broker/catalog`
  (`registry_source` config path — home directory redacted to `~` since S130).
- **Problem:** With `auth.enabled: false` (the default for localhost
  deployments) anything that can reach the port can read build identity and
  the redacted config path. On the reference deployment exposure is bounded
  by the nftables port lockdown; on other hosts it is operator
  responsibility.
- **Status:** Accepted risk until ADR-006 bearer-token auth lands in v0.5,
  which gates the entire `/broker/*` surface by default. Enable
  `auth.api_keys` today if the port is reachable beyond localhost.

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

## Resolved in v0.4.1

> v0.4.1 is the upcoming release: these items live under `[Unreleased]`
> in `CHANGELOG.md` until it is tagged.

### `VRAMTracker.get_loaded_models()` returns `[]` indistinguishable from "no models loaded"

- **Was:** `src/bastion/vram.py:133-135` returned `[]` on any HTTP exception,
  so every downstream consumer treated transient `/api/ps` failures as "VRAM
  is free" — exactly the misclassification that approved a second 31B load on
  top of an unflushed one during the S122-merge restart burst and crashed the
  5090 (downstream batch-client crash dossier, 2026-05-19).
- **Fix:** `get_loaded_models()` now returns `list[LoadedModel] | None`;
  `None` is the "state unknown" sentinel. Callers propagated:
  - `can_load_model()` — fail-closed: returns `(False, "VRAM state unknown…")`
  - `unload_model()` — poll continues until timeout instead of falsely
    confirming convergence on an empty `/api/ps` response
  - `ResidencyCache` — preserves the prior cache across a transient outage
    (stale-OK semantics); first cold-cache failure surfaces as `None`
  - `VRAMManager.reconcile(None)` — no-op (ledger preserved instead of wiped)
  - `Scheduler._process_tick` — bails out with `tracker_state_unknown` stall
    reason; next 100ms tick retries
  - `Scheduler._evict_for_model` — refuses to pick eviction candidates
    without ground truth; returns `False` so the caller retries
- **Regression tests:** `tests/test_vram.py::test_connection_failure_returns_none`,
  `::test_fail_closed_when_tracker_state_unknown`,
  `::test_unload_does_not_falsely_confirm_when_ps_unreachable`, and
  `tests/test_vram_state_unknown_extra.py::{TestResidencyCacheStateUnknown,
  TestVRAMManagerReconcileStateUnknown}`.

### Scheduler ignores `unload_model()` return value

- **Was:** `scheduler._unload_model()` returned `None` and ignored the bool
  from `vram.unload_model()`. The 901c910 fix made `unload_model()` honest
  about convergence, but the eviction loop kept paying for
  `wait_for_vram_convergence()` + `can_load_model()` on iterations where no
  VRAM was actually freed, and logged misleading "Cannot load X after evicting
  N models" lines where N counted *attempts* not successes.
- **Fix:** `_unload_model() -> bool` propagates the result. `_evict_for_model`
  now `continue`s on a False return (skips the convergence wait and
  can_load_model retry) so failed unloads don't masquerade as eviction
  progress. Defer-branches (active reservation, in-flight request) also
  return False — caller treats as "no VRAM freed."
- **Test:** `tests/test_scheduler_unload_gate.py::TestUnloadReturnGate`.

### `_cleanup_inflight` background task has no exception handler

- **Was:** `server.py::_cleanup_inflight` (the closure inside
  `_dispatch_request`) had no outer `try/except`. If `done_event.wait()`
  raised something other than `TimeoutError` (CancelledError, network
  error, attribute error), the task died and the `_inflight_models` counter
  stayed pinned above its true value forever — blocking the scheduler from
  ever evicting that model.
- **Fix:** body wrapped in `try/except Exception` with the decrement +
  scheduler `notify()` in `finally`. Inner `try/except` around the lock
  acquisition itself guards the decrement against unexpected lock state.
  Each block logs with `logger.exception(...)` so the real failure surfaces
  in logs instead of being swallowed.
- **Test:** `tests/test_cleanup_inflight_resilient.py::TestCleanupInflightResilience`
  (covers `done_event.wait()` raising `RuntimeError` and the happy path).

### Proxy enqueue bare `except Exception` reports any failure as "queue full"

- **Was:** `proxy.py` had a bare `except Exception` after the `except
  RuntimeError` handler that returned the same `503 "Broker queue full"`
  body and logged `"Queue full"` without `exc_info`. Programming bugs
  (AttributeError, TypeError) and infra failures all looked identical to
  clients (and identical in logs) — burned client backoff budgets on
  conditions retry can't fix.
- **Fix:** unexpected exceptions now log at `ERROR` with `exc_info=True`
  (real type + traceback) and return `500 "Internal broker error"`. Clients
  retain 503 for legitimate queue-full / drain conditions only.
- **Test:** `tests/test_proxy_enqueue_narrow_except.py::TestProxyEnqueueNarrowExcept`.

### `GET /broker/version` (new endpoint, paired with the fix above)

- **Was:** A2A batch clients had no way to detect
  that BASTION was redeployed mid-batch. Three S122 merges restarted the
  broker mid-batch and surfaced as four distinct error shapes downstream,
  each needing independent retry tuning.
- **Fix:** `GET /broker/version` returns `{version, git_sha, boot_time_unix,
  boot_time_iso}`. `git_sha` is captured at module load (env-var
  `BASTION_GIT_SHA` overrides; falls back to `git rev-parse HEAD`; final
  fallback `"unknown"`). Clients pin SHA at batch start and treat a change
  on retry as "infra in transition, longer backoff" rather than a normal
  5xx blip.
- **Test:** `tests/test_broker_version.py::TestBrokerVersion`.

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
