# Design Spec: BASTION Observability-First Enhancement

**Date**: 2026-03-13
**Status**: Draft (post-review revision 1)
**Reviewed by**: Spec reviewer agent (Opus 4.6) — 3 CRITICAL, 5 HIGH findings addressed below
**Origin**: Comprehensive audit by 11 specialist agents (3 scouts + 8 analysts)
**Synthesis**: internal audit (164 findings; source not committed)

---

## Problem Statement

BASTION computes far more data than it exposes. The audit identified:
- 10+ computed fields never surfaced via API (2 previously identified items — `vram_ledger` and `total_requests_served` — have since been wired)
- 8 fully dead + 1 partially wired Prometheus metrics (17 defined, 8 emitting)
- 3 of 5 OpenTelemetry spans defined but never used (2 are active in A2A path)
- 17 scheduler/queue internal fields invisible to operators
- 6 rich state structures reduced to simple counts before returning
- The primary crash-prevention signal (`swap_rate_level`) is invisible

The infrastructure for full observability exists. The gap is wiring.

## Design Goal

Surface all hidden internal state through three layers: API endpoints, Prometheus metrics/OTel traces, and the Textual TUI dashboard. No new dependencies. No architectural changes. Roughly 460 lines of code + config artifacts.

## Success Criteria

1. `/broker/status` returns all computed fields (vram_ledger, total_requests_served, swap_rate_level, inflight_models, circuit_breaker_state)
2. All 17 Prometheus metrics actively emit data
3. All 5 OpenTelemetry spans produce traces
4. 14 new API endpoints expose all hidden state
5. Dashboard consumes all new endpoints and shows 12 additional data dimensions
6. RequestID middleware enables end-to-end request correlation
7. Grafana dashboard JSON and alerting rules shipped as config artifacts
8. All existing tests pass; new tests cover new endpoints

---

## Section 1: "The 10-Line Revolution"

Wire hidden data fields into existing API responses. ~30 lines total.

> **Review note (C2, C3)**: Items T1-01 (`vram_ledger`) and T1-02 (`total_requests_served`) were found to be **already wired** in the current codebase. `vram_ledger` is wired at `server.py:602` via `_vram_manager.status()`. `total_requests_served` is wired at `server.py:599` to `_proxy._requests_served`. These are removed from the task list below. Implementers MUST verify each remaining item against current code before changing it.

### Changes

| Item | File | Change | Lines |
|------|------|--------|-------|
| ~~T1-01~~ | ~~`server.py`~~ | ~~Already wired (`server.py:602`)~~ | 0 |
| ~~T1-02~~ | ~~`server.py`~~ | ~~Already wired (`server.py:599`) — also add `total_dispatched` as separate field from `Scheduler._total_dispatched` (dispatched vs. served are different counts)~~ | 2 |
| T1-03 | `server.py` | Add `swap_rate_level` to status response from `Scheduler._swap_rate_level` | 3-5 |
| T1-04 | `server.py` | Add `stall_reason` + `stall_time` to status response | 5-10 |
| T1-05 | `server.py` | Add `_inflight_models` to status response | 2 |
| T1-06 | `models.py` | Convert `GPUStatus.vram_utilization_pct` to `@computed_field`. Note: `is_safe()` takes a parameter (`gpu_config`) so it CANNOT be a computed field — instead compute `gpu_is_safe: bool` in the status handler and add to `BrokerStatus` | 5 |
| T1-07 | `vram.py`, `models.py` | Include `LoadedModel.details` in serialization | 2 |
| T1-08 | `server.py` | Add circuit breaker state/failures/opened_at to status | 5 |
| T1-09 | `server.py` | New `GET /a2a/stats` endpoint calling `TaskStore.stats()` | 5-10 |
| T1-10 | `server.py`, `dashboard.py` | Return `GPUConfig.max_vram_gb` in status; remove dashboard hardcode | 3 |
| T1-11 | `__init__.py` | Fix version mismatch: `__version__` says `0.1.0` but `pyproject.toml` says `0.2.0` | 1 |

### Models Impact

`BrokerStatus` in `models.py` needs new optional fields:
- `total_dispatched: int | None` (distinct from `total_requests_served`)
- `swap_rate_level: str | None`
- `stall_reason: str | None`
- `stall_duration_seconds: float | None`
- `inflight_models: dict[str, int] | None`
- `circuit_breaker: dict | None`
- `gpu_is_safe: bool | None`
- `max_vram_gb: float | None`

These should be optional with `None` default for backward compatibility.

---

## Section 2: "Waking the Dead Metrics"

Activate 8 fully dormant Prometheus metrics + extend 1 partial, wire 3 dormant OTel spans, add periodic gauge updater. ~75 lines.

> **Review note (C1)**: There is no `_do_model_swap()` method. The swap logic lives in `_handle_swap_dispatch()` at `scheduler.py:445`. Timing and OTel spans should wrap the section around lines 532-579 where `_last_swap_time` and `_total_swaps` are updated.
>
> **Review note (H1)**: `metrics.py` and `telemetry.py` both define functions named `record_queue_wait()` and `record_model_swap()` with DIFFERENT signatures. Metrics versions record histogram observations; telemetry versions are context managers creating spans. Implementers must call BOTH at appropriate sites using qualified imports (e.g., `from bastion.metrics import record_queue_wait as metrics_record_queue_wait`).

### Prometheus Call Sites

| Metric | Helper (`metrics.py`) | Call Site | File |
|--------|--------|----------|------|
| `bastion_queue_wait_seconds` | `metrics.record_queue_wait(model, tier, wait_seconds)` | When request granted: `now - queued_at` | `scheduler.py` |
| `bastion_queue_depth` | `metrics.update_queue_depth(depth)` | Each scheduler tick | `scheduler.py` |
| `bastion_model_swap_total` | `metrics.record_model_swap(from_model, to_model)` | After swap in `_handle_swap_dispatch()` (~line 535) | `scheduler.py` |
| `bastion_model_swap_duration_seconds` | `metrics.record_model_swap_duration(seconds)` | Wrap swap section in `_handle_swap_dispatch()` with timing | `scheduler.py` |
| `bastion_cooldown_waits_total` | `record_cooldown_wait()` | When cooldown blocks dispatch | `scheduler.py` |
| `bastion_vram_used_bytes` | `update_vram_usage()` | Periodic gauge updater | `server.py` |
| `bastion_gpu_temperature_celsius` | `update_gpu_temperature()` | After nvidia-smi query | `health.py` |
| `bastion_a2a_queue_depth` | `update_a2a_queue_depth()` | On task create/complete | `a2a.py` |
| `bastion_llm_time_to_first_token` | `metrics.observe_llm_ttft(seconds)` | **PARTIAL** — already wired in A2A path (`a2a.py:801`), extend to proxy streaming path | `proxy.py` |

### OpenTelemetry Spans (use `telemetry.*` context managers)

| Span | Helper (`telemetry.py`) | File | Wraps |
|------|--------|------|-------|
| `bastion.scheduler.queue_wait` | `telemetry.record_queue_wait(request_id, model)` | `scheduler.py` | `grant_event.wait()` |
| `bastion.scheduler.model_swap` | `telemetry.record_model_swap(from_model, to_model)` | `scheduler.py` | Swap section in `_handle_swap_dispatch()` |
| `bastion.ollama.inference` | `telemetry.record_inference(model, request_id)` | `proxy.py` | httpx calls to Ollama |

### Periodic Gauge Updater

New `async def _gauge_update_loop()` in `server.py` (~30 lines):
- Runs every 5 seconds as background task
- Updates: VRAM usage, GPU temperature, queue depth, circuit breaker state
- Pattern: same as existing scheduler loop startup

---

## Section 3: "The Dashboard Sees All"

Enhance TUI to consume all new data. 12 new data dimensions, 6 alert conditions, SSE streaming. ~100 lines in `dashboard.py`.

### New Data Dimensions

| Panel | Data | Source Endpoint |
|-------|------|----------------|
| GPU | Fan speed, clock, PCIe gen, P-state | Enhanced nvidia-smi query |
| Queue | Per-request age, priority, client | `GET /broker/queue/details` |
| Queue | Stall duration | `GET /broker/scheduler/diagnostics` |
| Scheduler | Swap rate level with color coding | `GET /broker/scheduler/diagnostics` |
| Scheduler | Total dispatched (actual count) | `GET /broker/status` (fixed) |
| VRAM | Per-model breakdown from ledger | `GET /broker/status` (wired vram_ledger) |
| VRAM | Budget from config (not hardcoded) | `GET /broker/status` (max_vram_gb) |
| Models | Quantization, family, params | `GET /broker/status` (LoadedModel.details) |
| A2A | Pressure level, subscribers, tombstones | `GET /a2a/stats` |
| A2A | Active leases | `GET /a2a/leases` |
| Circuit Breaker | State, failure count | `GET /broker/status` (circuit_breaker) |
| Cooldown | Effective value, remaining time | `GET /broker/scheduler/diagnostics` |

### Alert Conditions

Visual alerts (color changes / banners) for:
1. `swap_rate_level == "critical"` — red scheduler panel
2. Circuit breaker opens — alert banner
3. TaskStore pressure == "overloaded" — A2A panel warning
4. Queue request age > threshold — highlight aged requests
5. VRAM utilization > 90% of budget — VRAM panel warning
6. Stall duration > 30s — scheduler panel alert

### SSE Streaming (Progressive Enhancement)

- New `GET /broker/status/stream` SSE endpoint in `server.py` (~30 lines)
- Dashboard optionally consumes via `httpx.AsyncClient.stream()`
- Falls back to polling if SSE unavailable
- Live A2A batch progress via existing `/a2a/tasks/{id}/stream`

---

## Section 4: "The Plumbing Layer"

12 new endpoints (after deducting `/a2a/stats` which moves to Phase 1), RequestID middleware, Grafana artifacts. ~250 lines.

> **Review note (H3, M5)**: Phases 1 and 2 both modify `server.py`. If run in parallel, `server.py` changes must be serialized or merged carefully. Also, every route handler is duplicated between `create_app()` and `create_admin_app()` — consider extracting handlers into standalone functions to avoid 2x duplication for all new endpoints.

### Wave 1 — Direct Data Exposure

| Endpoint | Lines | Returns |
|----------|-------|---------|
| `GET /broker/scheduler/diagnostics` | ~15 | current_model, stall_reason, stall_duration, swap_rate_level, swaps_in_window, effective_cooldown, cooldown_remaining, total_dispatched |
| `GET /broker/queue/details` | ~20 | Per-request: id, model, tier, age_seconds, effective_priority, endpoint, client_info |
| `GET /broker/inflight` | ~10 | inflight_models, pending_grants count, pending_completions count |
| `GET /a2a/leases` | ~15 | Active leases with full state |
| `GET /broker/residency` | ~10 | Resident models, per-model VRAM, cache age, staleness |
| `GET /broker/profiles` | ~5 | Session profiles from config |

### Wave 2 — Minor Logic

| Endpoint | Lines | What |
|----------|-------|------|
| `GET /broker/config` | ~15 | Running config, auth tokens redacted |
| `GET /broker/audit` | ~30 | JSONL audit log with filters (?limit, ?since, ?event_type) |
| `GET /broker/vram-journal` | ~20 | Parse VRAM journal JSONL with limit |
| `GET /broker/metrics?format=json` | ~25 | Prometheus metrics as JSON |
| `GET /broker/status/stream` | ~30 | SSE push-based status |

### Wave 3 — Operational

| Endpoint | Lines | What |
|----------|-------|------|
| `POST /broker/config/reload` | ~50 | Hot-reload broker.yaml — MUST validate config before applying (reject negative cooldowns, headroom > total VRAM, etc.). Rollback on validation failure. |

### RequestID Middleware (~20 lines)

- New class following `MetricsMiddleware` pattern
- Injects `X-Request-ID` (UUID4) if not present
- Stored in request state for audit logger, metrics labels, OTel span attributes
- Enables: audit log entry <-> Prometheus label <-> OTel trace correlation

### Rate Limiting Fix (1 line)

Apply existing `RateLimitMiddleware` to `create_admin_app()` in two-port mode at `server.py` ~line 1172:
```python
app.add_middleware(RateLimitMiddleware, config=config.rate_limit)
```

### Grafana Artifacts (config-only)

| File | Content |
|------|---------|
| `config/grafana/bastion-dashboard.json` | Dashboard with panels for all 17+ metrics |
| `config/prometheus/scrape.yml` | Scrape config snippet |
| `config/prometheus/alerts.yml` | Alert rules: swap_rate critical, circuit breaker open, VRAM >90%, stall >30s |

---

## Architecture Constraints

- **No new dependencies** — everything uses existing FastAPI, Pydantic, httpx, Textual
- **No external DB** — all state remains in-memory (project rule)
- **Backward compatible** — new fields are optional with `None` defaults
- **No breaking changes** — existing endpoints return same shape plus new fields
- **Existing patterns** — all new code follows established patterns in the codebase

## Testing Strategy

- Each new endpoint gets at least one happy-path test and one auth test
- Metric emission verified via mock/spy on helper functions
- Dashboard changes tested via existing Textual test patterns
- RequestID middleware tested for injection, passthrough, and correlation
- All existing tests must continue to pass

---

## Non-Goals (with follow-up notes)

- Multi-GPU support (separate design)
- A2A orchestration enhancements (separate design)
- Client library expansion (separate design — but note the 13 new endpoints will need corresponding client methods eventually)
- Full config validation constraints (separate design — but `/broker/config/reload` MUST validate before applying to prevent runtime crashes)
