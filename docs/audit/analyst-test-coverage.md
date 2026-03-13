# BASTION Test Coverage Analysis

**Analyst**: Test Coverage Analyst (Claude Opus 4.6)
**Date**: 2026-03-13
**Scope**: All source modules in `src/bastion/` and test files in `tests/`, plus `clients/bastion-client/`

---

## Executive Summary

BASTION has a substantial test suite with **29 test files** (excluding `__init__.py` and `conftest.py`) covering **20 source modules**. The suite contains approximately **450+ individual test functions** organized into **130+ test classes**. Coverage is strong for core business logic (queue, scheduler, VRAM, circuit breaker, A2A protocol) but has notable gaps in the dashboard TUI, server route handlers, CLI entry point, and A2A-specific metrics.

---

## 1. Module-by-Module Test Coverage Map

### Source Modules with Dedicated Test Files

| Source Module | Test File(s) | Test Classes | Test Functions | Coverage Rating |
|---|---|---|---|---|
| `a2a.py` | `test_a2a.py` | 8 | ~45 | HIGH |
| `audit.py` | `test_audit.py`, `test_audit_tiered.py` | 11 | ~30 | HIGH |
| `auth.py` | `test_auth.py` | 4 | ~18 | HIGH |
| `circuitbreaker.py` | `test_circuitbreaker.py` | 8 | ~28 | HIGH |
| `config.py` | `test_config.py` | 1 | ~10 | MEDIUM |
| `dashboard.py` | `test_dashboard.py` | 0 (module-level) | ~30 | LOW-MEDIUM |
| `health.py` | `test_health.py` | 3 | ~12 | HIGH |
| `metrics.py` | `test_metrics.py` | 5 | ~22 | MEDIUM |
| `models.py` | `test_models.py` | 7 | ~22 | MEDIUM |
| `proxy.py` | `test_proxy.py` | 4 | ~10 | MEDIUM |
| `queue.py` | `test_queue.py` | 5 | ~16 | HIGH |
| `ratelimit.py` | `test_ratelimit.py` | 2 | ~7 | MEDIUM |
| `scheduler.py` | `test_scheduler.py` | 9 | ~30 | HIGH |
| `taskstore.py` | `test_taskstore.py` | 8 | ~30 | HIGH |
| `telemetry.py` | `test_telemetry.py` | 2 | ~18 | MEDIUM |
| `vram.py` | `test_vram.py`, `test_vram_manager.py` | 14 | ~40 | HIGH |
| `watchdog.py` | `test_watchdog.py` | 3 | ~18 | HIGH |

### Source Modules WITHOUT Dedicated Test Files

| Source Module | Indirect Coverage | Coverage Rating |
|---|---|---|
| `__init__.py` | No tests (package init, typically minimal) | N/A |
| `__main__.py` | **NO tests at all** -- CLI entry point (`main()` with argparse + uvicorn) | NONE |
| `middleware.py` | Tested indirectly via `test_metrics.py` (TestMiddlewareExtraction, TestMiddlewareIntegration) | LOW-MEDIUM |
| `server.py` | Tested indirectly via `test_two_port.py` (app factory functions), `test_intent.py` (server internals), `test_dashboard.py` (record_recent_request) | LOW |

### Cross-Cutting / Integration Test Files (not mapped to a single module)

| Test File | Focus | Test Classes | Test Functions |
|---|---|---|---|
| `test_concurrency.py` | Cross-module concurrency safety | 7 | ~20 |
| `test_e2e_stress.py` | End-to-end stress tests (marked `@pytest.mark.e2e`) | 10 | ~18 |
| `test_error_boundaries.py` | Error handling across config, VRAM, GPU, proxy | 5 | ~20 |
| `test_intent.py` | Intent declaration/resolution across models + server | 8 | ~30 |
| `test_lease.py` | Model lease lifecycle (A2A handler leases) | 5 | ~28 |
| `test_residency.py` | Residency cache + co-resident scheduling | 6 | ~13 |
| `test_serialization.py` | Request serialization through scheduler | 5 | ~12 |
| `test_shutdown.py` | Graceful shutdown (scheduler + queue + systemd) | 4 | ~10 |
| `test_two_port.py` | Two-port mode (proxy vs admin app separation) | 9 | ~38 |

---

## 2. Functions and Classes Without Test Coverage

### Completely Untested Functions

#### `src/bastion/__main__.py`
- `main()` -- CLI entry point with argparse, config loading, uvicorn startup. **Zero test coverage.**

#### `src/bastion/server.py` -- Route Handlers (Untested Directly)
The following route handler functions inside `create_app()`, `create_proxy_app()`, and `create_admin_app()` have no direct unit tests. They are only exercised indirectly by `test_two_port.py` which checks route existence, not behavior:

- `broker_status()` -- returns BrokerStatus
- `broker_queue()` -- returns queue state
- `broker_health()` -- returns GPU health
- `broker_vram()` -- returns VRAM state
- `broker_livez()` / `broker_readyz()` -- liveness/readiness probes
- `broker_preload(request)` -- preload model
- `broker_unload(request)` -- unload model
- `broker_drain()` / `broker_resume()` -- drain mode control
- `broker_metrics()` -- Prometheus exposition
- `broker_watchdog()` -- watchdog status
- `broker_recent()` -- recent requests
- `broker_intent(request)` -- intent declaration
- `broker_intents()` -- list intents
- `broker_intent_complete(intent_id)` / `broker_intent_delete(intent_id)` -- intent lifecycle
- `a2a_create_task(request)` -- A2A task creation endpoint
- `a2a_get_task(task_id)` -- A2A task retrieval endpoint
- `a2a_stream_task(task_id)` -- SSE streaming endpoint
- `a2a_cancel_task(task_id)` -- task cancellation endpoint
- `a2a_lease_heartbeat(lease_id)` / `a2a_release_lease(lease_id)` -- lease endpoints
- `agent_card()` / `a2a_extended_card()` -- agent card endpoints
- `proxy_ollama(request, path)` -- catch-all proxy
- `root()` -- root endpoint
- `lifespan(app)` -- startup/shutdown lifecycle manager
- `record_recent_request()` -- partially tested (1 test in `test_dashboard.py`)
- `has_inflight()` / `inflight_count()` -- in-flight tracking helpers

#### `src/bastion/metrics.py` -- A2A Metrics (Untested)
The following A2A-specific metric functions have **zero test coverage**:
- `emit_a2a_task(skill, state)`
- `emit_a2a_error(method, error_code)`
- `observe_a2a_task_duration(skill, model, duration, state)`
- `observe_a2a_queue_wait(skill, model, wait_seconds)`
- `observe_llm_ttft(model, ttft_seconds)`
- `update_a2a_tasks_active(state, count)`
- `update_a2a_queue_depth(skill, model, depth)`
- `record_model_swap_duration(model, duration)`

#### `src/bastion/dashboard.py` -- TUI Components (Mostly Untested)
Only pure utility functions are tested (sparkline, color helpers, format_uptime, vram_bar, alert_panel). The following are **completely untested**:

- `BastionClient` class (HTTP polling client) -- all 10 methods
- All 14 panel `render_data()` methods:
  - `GPUPanel.render_data()`
  - `ModelsPanel.render_data()`
  - `QueuePanel.render_data()`
  - `SchedulerPanel.render_data()`
  - `ConnectionPanel.render_data()`
  - `CircuitBreakerPanel.render_data()`
  - `VRAMLedgerPanel.render_data()`
  - `A2ATaskPanel.render_data()`
  - `LeasePanel.render_data()`
  - `AuditStreamPanel.render_data()`
  - `WatchdogPanel.render_data()`
  - `TracePanel.render_data()`
  - `StatusBar.render_status()`
- `BastionDashboard` (main App class) -- compose, all action methods, refresh_data
- All modal screens (ConfirmActionModal, ModelSelectModal, HelpModal, FanControlModal, GPUProcessListModal, ConfirmGPUKillModal)
- `format_countdown()`, `format_bytes_gb()`, `format_bytes_mb()`, `cb_state_color()`, `a2a_state_color()`, `lease_state_color()`

#### `src/bastion/vram.py` -- Partially Untested
- `VRAMTracker.log_vram_snapshot()` -- only tested indirectly via scheduler mocking
- `VRAMTracker.close()` -- not directly tested
- `VRAMManager.release_model()` -- not directly tested
- `VRAMManager.reconcile()` -- not directly tested

#### `src/bastion/queue.py` -- Partially Untested
- `sweep_stale()` -- no direct tests (stale request cleanup)

#### `src/bastion/proxy.py` -- Partially Untested
- `OllamaProxy.close()` -- not directly tested

#### `src/bastion/telemetry.py` -- Context Manager Functions
- `record_queue_wait()` -- tested as no-op only, not with real OTel
- `record_model_swap()` -- tested as no-op only
- `record_inference()` -- tested as no-op only
- `shutdown_telemetry()` -- tested as no-op only
- `extract_trace_context()` -- tested as no-op only

---

## 3. Test Patterns Used

### Fixture Architecture

**Shared fixtures** (`tests/conftest.py` -- 18 fixtures):
- `test_config()` -- standard `BrokerConfig` with test-safe values
- `small_config()` -- config with max_queue_size=2 for overflow tests
- `queue()` -- pre-built `AffinityQueue`
- `vram_tracker()` -- pre-built `VRAMTracker`
- `gpu_status_safe()`, `gpu_status_hot()`, `gpu_status_unavailable()` -- GPU state variants
- `mock_gpu_safe()`, `mock_gpu_hot()` -- monkeypatched GPU query mocks
- `mock_ollama()` -- monkeypatched Ollama HTTP responses
- `make_request()` / `request_factory()` -- `QueuedRequest` builders
- `make_task_record()` / `task_record_factory()` -- `A2ATaskRecord` builders
- `task_store()` -- pre-built `TaskStore`
- `vram_manager()` -- pre-built `VRAMManager`
- `_isolate_audit_logger()` (autouse) -- isolates global audit state
- `_isolate_telemetry()` (autouse) -- isolates global telemetry state

**Module-specific fixtures** defined locally in test files:
- `test_scheduler.py` -- 7 fixtures (sched_config, dispatch_log, concurrent_config, cooldown_config, swap_config, evict_config, sync_config)
- `test_circuitbreaker.py` -- 4 fixtures (cb, disabled_breaker, breaker)
- `test_a2a.py` -- 5 fixtures (a2a_config, mock_vram_tracker, mock_scheduler, a2a_handler)
- `test_vram_manager.py` -- 3 fixtures (tracker, config, manager)
- `test_residency.py` -- 2 fixtures (residency_config, dispatch_log)
- `test_two_port.py` -- 2 fixtures (two_port_config, single_port_config)
- `test_e2e_stress.py` -- 4 session-scoped fixtures

### Async Testing

- **`@pytest.mark.asyncio`** is used extensively (~180 occurrences) for coroutine testing
- Applied across: scheduler, VRAM, circuit breaker, A2A, health, watchdog, proxy, taskstore, concurrency, residency, shutdown, lease, serialization, vram_manager, metrics, error_boundaries

### Mocking Strategy

- **Total mock usage**: ~227 occurrences across 18 files
- Primary tools: `unittest.mock.patch`, `unittest.mock.MagicMock`, `unittest.mock.AsyncMock`, `monkeypatch`
- Heavy mocking in: `test_a2a.py` (37 mock uses), `test_circuitbreaker.py` (22), `test_vram.py` (21), `test_watchdog.py` (19), `test_health.py` (18), `test_metrics.py` (18), `test_scheduler.py` (16)
- Monkeypatching used for: subprocess calls (`nvidia-smi`), HTTP clients, file system operations, environment variables

### Special Test Markers

- **`@pytest.mark.e2e`** -- 10 test classes in `test_e2e_stress.py` (session-scoped, requires running BASTION instance)
- **`@pytest.mark.asyncio`** -- ~180 occurrences for async test functions
- No `@pytest.mark.skip`, `@pytest.mark.xfail`, or `@pytest.mark.parametrize` markers detected

### Test Organization Pattern

- Class-based grouping: Most test files use `class TestXxx:` pattern to group related tests
- Some files use module-level functions (e.g., `test_dashboard.py` for utility function tests)
- Naming convention: `test_<module>.py` maps to `src/bastion/<module>.py`
- Cross-cutting concerns in dedicated files: `test_concurrency.py`, `test_error_boundaries.py`, `test_serialization.py`, `test_residency.py`, `test_shutdown.py`

---

## 4. Edge Cases and Error Paths

### Well-Tested Error Paths

| Area | Error Paths Tested |
|---|---|
| **Config loading** | Empty YAML, null YAML, wrong types, negative values, extra fields, missing file |
| **GPU/Health** | nvidia-smi timeout, not found, nonzero exit, garbled output, empty output |
| **VRAM** | Connection failure, HTTP errors, budget exceeded, GPU too hot, failed unload |
| **Circuit breaker** | Open circuit raises, failed probe reopens, connect errors, read timeouts, 5xx responses |
| **Queue** | Full queue rejection, dequeue from empty, wrong model dequeue, cancel nonexistent |
| **Scheduler** | GPU unsafe pauses, drain mode, stop timeout, double start |
| **TaskStore** | Invalid state transitions, terminal state transitions, nonexistent task updates, backpressure levels, TTL eviction |
| **A2A** | Unknown skill, missing skill_id, missing model, queue full, timeout, Ollama errors |
| **Auth** | Missing token, invalid token, bad header format, cross-auth isolation |
| **Rate limit** | Burst exceeded returns 429, retry-after header |
| **Audit** | Disk full resilience, write errors, emit before init |
| **Proxy** | Invalid JSON returns 400, queue full returns 503, timeout returns 504 |
| **Lease** | Stale fencing token, expired lease, idle timeout, released lease heartbeat, double release |
| **Concurrency** | Double complete, cancel-after-complete, concurrent creates at capacity |

### Untested Error Paths and Edge Cases

| Area | Missing Coverage |
|---|---|
| **Server lifespan** | Startup failure, shutdown with in-flight requests, partial component initialization failure |
| **Server route handlers** | Error responses from all `/broker/*` endpoints, malformed request bodies, auth failures on admin routes (behavior, not just existence) |
| **Proxy streaming** | NDJSON streaming interruption, partial response handling, connection drops mid-stream |
| **Dashboard client** | HTTP connection failures, timeout handling, malformed API responses |
| **Dashboard panels** | Rendering with missing/null data, extreme values, empty collections |
| **CLI entry point** | Invalid arguments, port conflicts, config file errors at startup |
| **Metrics A2A** | All A2A metric emission functions (8 functions completely untested) |
| **VRAM reconcile** | Mismatch between tracked and actual state, partial failures during reconciliation |
| **Queue sweep_stale** | Stale request cleanup with various age thresholds |
| **Watchdog** | Systemd socket interaction (sd_notify with real socket) |
| **Rate limit** | Token refill over time, concurrent requests from same IP |
| **Two-port mode** | Actual HTTP request routing behavior (only route existence is verified) |
| **Telemetry** | Real OTel SDK integration (only mocked OTel and no-op stubs tested) |

---

## 5. Client Library Coverage (`clients/bastion-client/`)

### Source Files
- `bastion_client/client.py` -- `BastionClient` class with 4 async methods
- `bastion_client/models.py` -- 4 Pydantic models

### Test Coverage (`clients/bastion-client/tests/test_client.py`)

| Class/Feature | Tests | Coverage |
|---|---|---|
| Tier mapping (stage to tier) | 7 tests | HIGH |
| IntentRequest construction | 4 tests | HIGH |
| BastionClient construction | 3 tests | MEDIUM |
| BastionClient.infer() | 4 tests | HIGH |
| BastionClient.declare_intent() | 2 tests | MEDIUM |
| BastionClient.check_vram() | 2 tests | MEDIUM |
| Context manager (close) | 1 test | LOW |

**Gaps**: No tests for error handling in client methods (connection refused, timeout, 5xx responses, malformed responses). The `close()` method has only one basic test.

---

## 6. Recommendations for Improving Coverage

### Priority 1 -- Critical Gaps (Highest Impact)

1. **Add `test_server.py`**: Create dedicated tests for route handler behavior (not just route existence). Test each `/broker/*` endpoint with valid and invalid inputs, verify response shapes, test error responses (404, 500), and test the `lifespan()` startup/shutdown lifecycle.

2. **Add `test_main.py`**: Test CLI argument parsing, config file resolution, and the `main()` function (mocking uvicorn.run). This is the application entry point and currently has zero coverage.

3. **Test A2A metrics functions**: The 8 untested A2A metric functions (`emit_a2a_task`, `emit_a2a_error`, `observe_a2a_task_duration`, `observe_a2a_queue_wait`, `observe_llm_ttft`, `update_a2a_tasks_active`, `update_a2a_queue_depth`, `record_model_swap_duration`) should follow the same pattern as the existing `TestMetricsIncrement` class.

### Priority 2 -- Important Gaps (Medium Impact)

4. **Dashboard panel rendering tests**: Add tests for each panel's `render_data()` method with edge cases (None values, empty lists, extreme numbers). These are pure functions that accept data dicts and return Rich `Table` objects -- easy to test without running the TUI.

5. **Dashboard utility function gaps**: Test `format_countdown()`, `format_bytes_gb()`, `format_bytes_mb()`, `cb_state_color()`, `a2a_state_color()`, `lease_state_color()` -- all are pure functions.

6. **Two-port mode HTTP behavior**: Extend `test_two_port.py` beyond route existence to actually make HTTP requests through `TestClient` and verify response content, status codes, and content types.

7. **Queue sweep_stale()**: Add tests for stale request cleanup with various age thresholds and edge cases (all stale, none stale, mixed).

8. **VRAMManager.reconcile() and release_model()**: Test the reconciliation logic that compares tracked vs. actual VRAM state.

### Priority 3 -- Nice to Have (Lower Impact)

9. **Add `@pytest.mark.parametrize`**: Several test classes repeat the same pattern with different inputs (e.g., `test_temp_color_green/yellow/orange/red`). These would benefit from parametrize to reduce boilerplate and ensure completeness.

10. **Proxy streaming edge cases**: Test NDJSON streaming interruption, connection drops, partial responses, and very large payloads.

11. **Client error handling**: Add tests for `BastionClient` methods when the server returns errors (connection refused, 500, timeout, malformed JSON).

12. **Rate limit temporal behavior**: Test token bucket refill over time and concurrent request behavior from the same IP.

13. **Telemetry with real-ish OTel**: The `TestWithMockedOTel` class is good but could be expanded to cover `record_queue_wait()`, `record_model_swap()`, and `record_inference()` context managers with mocked OTel SDK.

### Testing Infrastructure Improvements

14. **Consider adding coverage measurement**: Configure `pytest-cov` to generate coverage reports and set a minimum threshold (recommend targeting 85% line coverage as first milestone).

15. **Mark slow tests**: Some scheduler and concurrency tests use `asyncio.sleep`. Consider marking them or reducing sleep times for CI speed.

16. **Separate e2e tests in CI**: The `test_e2e_stress.py` tests are correctly marked with `@pytest.mark.e2e` but ensure CI runs them separately from unit tests to avoid flaky failures.

---

## 7. Test Count Summary

| Category | Count |
|---|---|
| Total test files | 29 |
| Total test classes | ~130 |
| Total test functions | ~450+ |
| Source modules with direct tests | 17/20 (85%) |
| Source modules with NO tests | 3 (`__init__.py`, `__main__.py`, partially `middleware.py`) |
| Async test functions | ~180 |
| Fixtures (shared conftest) | 18 |
| Fixtures (module-local) | ~26 |
| E2E stress test classes | 10 |
| Mock/patch usages | ~227 |

### Overall Assessment

**Estimated line coverage**: 65-75% (without `pytest-cov` to confirm)

The test suite is well-structured and thorough for core business logic. The primary coverage gaps are in the HTTP layer (server route handlers, dashboard HTTP client) and the CLI entry point. Addressing Priority 1 recommendations would likely bring estimated coverage to 80-85%.
