# Implementation Plan: BASTION Observability-First Enhancement

**Design Spec**: `docs/design/specs/2026-03-13-observability-first-design.md`
**Audit Source**: internal audit (source not committed)
**Workflow**: Implement -> Test -> Commit -> Q&A -> Plan next -> Context clear -> Repeat

---

## Execution Model

Each phase follows this cycle:
1. **Plan** — Review phase scope, assign tasks to agent team
2. **Implement** — Agent team executes in parallel where possible
3. **Test** — Run full test suite, verify new functionality
4. **Commit** — Stage and commit with descriptive message
5. **Q&A** — User reviews changes, asks questions, requests adjustments
6. **Context clear** — Fresh session for next phase

Phases are ordered by dependency: later phases consume endpoints/fields created by earlier phases.

---

## Phase 1: "The 10-Line Revolution" — Wire Hidden Data

**Depends on**: Nothing (foundation phase)
**Estimated agents**: 3 parallel

### Tasks

#### IMPORTANT: Pre-Implementation Verification
Before modifying any code, each agent MUST verify the current state of fields/methods referenced in the spec against the actual source code. The audit ran against a prior snapshot; some items (T1-01 `vram_ledger`, T1-02 `total_requests_served`) were already wired by the time of review.

#### Agent 1: Status Endpoint Wiring (`server.py`)
- [ ] VERIFY: Check if `vram_ledger` is already wired at ~line 602 (skip if so)
- [ ] VERIFY: Check if `total_requests_served` is already wired at ~line 599 (if so, add `total_dispatched` as SEPARATE field from `Scheduler._total_dispatched`)
- [ ] Add `swap_rate_level` from `Scheduler._swap_rate_level` to status (T1-03)
- [ ] Add `stall_reason` + `stall_time` to status response (T1-04)
- [ ] Add `_inflight_models` to status response (T1-05)
- [ ] Add circuit breaker state/failures to status (T1-08)
- [ ] Return `GPUConfig.max_vram_gb` in status (T1-10)
- [ ] Compute `gpu_is_safe` in handler using `gpu_status.is_safe(config.gpu)` and add to response
- [ ] Fix version mismatch: update `__init__.__version__` to match `pyproject.toml` (T1-11)
- [ ] DUPLICATE: Apply same changes to `create_admin_app()` status handler (~line 1183)

#### Agent 2: Model Serialization Fixes (`models.py`, `vram.py`)
- [ ] Convert `GPUStatus.vram_utilization_pct` to `@computed_field` (Pydantic v2)
- [ ] NOTE: `GPUStatus.is_safe()` takes a `gpu_config` param — keep as method, do NOT convert to computed_field. `gpu_is_safe` is computed in the status handler instead.
- [ ] Include `LoadedModel.details` in serialization (T1-07)
- [ ] Add new optional fields to `BrokerStatus`: `total_dispatched`, `swap_rate_level`, `stall_reason`, `stall_duration_seconds`, `inflight_models`, `circuit_breaker`, `gpu_is_safe`, `max_vram_gb`

#### Agent 3: New Endpoint + Dashboard Fix
- [ ] Create `GET /a2a/stats` endpoint calling `TaskStore.stats()` (T1-09) — in BOTH app factories
- [ ] Remove hardcoded `VRAM_BUDGET_GB = 26.0` from `dashboard.py`, read from API (T1-10) — sequence: server change first, then dashboard
- [ ] Write tests for all new status fields
- [ ] Write test for `/a2a/stats` endpoint

### Verification
```bash
python -m pytest tests/ -v
```
Confirm: `/broker/status` response includes all new fields. `/a2a/stats` returns store statistics.

---

## Phase 2: "Waking the Dead Metrics" — Prometheus + OTel

**Depends on**: Phase 1 (status fields must exist for gauge updater to read)
**Estimated agents**: 3 parallel

### Tasks

#### Agent 1: Scheduler Metric Call Sites (`scheduler.py`)
NOTE: `metrics.py` and `telemetry.py` have identically-named functions with DIFFERENT signatures.
Use qualified imports: `from bastion.metrics import record_queue_wait as metrics_queue_wait` etc.
- [ ] Call `metrics.record_queue_wait(model, tier, wait_seconds)` when request granted
- [ ] Call `metrics.update_queue_depth(depth)` on each scheduler tick
- [ ] Call `metrics.record_model_swap(from_model, to_model)` after swap in `_handle_swap_dispatch()` (~line 535) — NOTE: there is NO `_do_model_swap()` method
- [ ] Call `metrics.record_model_swap_duration(seconds)` wrapping swap timing in `_handle_swap_dispatch()`
- [ ] Call `metrics.record_cooldown_wait()` when cooldown blocks dispatch
- [ ] Wrap `grant_event.wait()` with `telemetry.record_queue_wait(request_id, model)` OTel context manager
- [ ] Wrap swap section in `_handle_swap_dispatch()` with `telemetry.record_model_swap(from_model, to_model)` OTel context manager

#### Agent 2: Non-Scheduler Metric Call Sites
- [ ] `health.py`: Call `update_gpu_temperature()` after nvidia-smi query
- [ ] `a2a.py`: Call `update_a2a_queue_depth()` on task create/complete
- [ ] `proxy.py`: Extend `metrics.observe_llm_ttft()` to proxy streaming path (already wired in A2A at `a2a.py:801`, needs proxy path too)
- [ ] `proxy.py`: Wrap httpx calls with `telemetry.record_inference(model, request_id)` OTel context manager

#### Agent 3: Periodic Gauge Updater + Tests
- [ ] Create `_gauge_update_loop()` in `server.py` (~30 lines)
  - Updates every 5s: VRAM usage, GPU temp, queue depth, circuit breaker state
  - Starts as background task alongside scheduler
- [ ] Write tests verifying metric helper functions are called
- [ ] Write tests for gauge updater loop

### Verification
```bash
python -m pytest tests/ -v
```
Confirm: `/broker/metrics` returns non-zero values for all 17 metrics when prometheus_client is installed. OTel spans appear in trace output when SDK installed.

---

## Phase 3: "The Plumbing Layer" — New Endpoints + Middleware

**Depends on**: Phase 1 (data must be accessible), Phase 2 (metrics must be active)
**Estimated agents**: 4 parallel

### Tasks

#### Agent 1: Wave 1 Endpoints — Diagnostics (`server.py`)
- [ ] `GET /broker/scheduler/diagnostics` — scheduler internals
- [ ] `GET /broker/queue/details` — per-request breakdown
- [ ] `GET /broker/inflight` — inflight models and pending ops
- [ ] Write tests for all three endpoints (auth, response shape, edge cases)

#### Agent 2: Wave 1 Endpoints — A2A + Residency (`server.py`, `a2a.py`)
- [ ] `GET /a2a/leases` — active leases with full state
- [ ] `GET /broker/residency` — resident models, VRAM, cache age
- [ ] `GET /broker/profiles` — session profiles from config
- [ ] Write tests for all three endpoints

#### Agent 3: Wave 2 Endpoints (`server.py`)
- [ ] `GET /broker/config` — running config with token redaction
- [ ] `GET /broker/audit` — JSONL audit log query with filters
- [ ] `GET /broker/vram-journal` — VRAM journal query
- [ ] `GET /broker/metrics?format=json` — JSON metrics export
- [ ] Write tests for all four endpoints

#### Agent 4: Middleware + Security Fixes
- [ ] RequestID middleware class (~20 lines) following MetricsMiddleware pattern
- [ ] Register RequestID middleware in both app factories
- [ ] Apply `RateLimitMiddleware` to `create_admin_app()` (1-line fix)
- [ ] Write tests for RequestID injection, passthrough, correlation
- [ ] Write test for admin rate limiting in two-port mode

### Verification
```bash
python -m pytest tests/ -v
```
Confirm: All 14 new endpoints return expected data. RequestID appears in response headers. Admin routes are rate-limited in two-port mode.

---

## Phase 4: "The Dashboard Sees All" — TUI Enhancement

**Depends on**: Phase 3 (new endpoints must exist for dashboard to consume)
**Estimated agents**: 3 parallel

### Tasks

#### Agent 1: Dashboard Data Integration (`dashboard.py`)
- [ ] Consume `/broker/scheduler/diagnostics` — swap rate level with color coding, cooldown state
- [ ] Consume `/broker/queue/details` — per-request age, priority, client in queue panel
- [ ] Consume enhanced `/broker/status` — all new fields (circuit breaker, inflight, stall)
- [ ] Remove hardcoded values, read from API (VRAM budget already done in Phase 1)

#### Agent 2: Dashboard Alerts + A2A (`dashboard.py`)
- [ ] Add visual alerts: swap_rate critical (red), circuit breaker open (banner), VRAM >90% (warning)
- [ ] Add stall duration alert (>30s)
- [ ] Consume `/a2a/stats` — pressure level, subscriber count
- [ ] Consume `/a2a/leases` — active leases panel
- [ ] Queue request age highlighting

#### Agent 3: SSE Streaming + Grafana Artifacts
- [ ] Create `GET /broker/status/stream` SSE endpoint in `server.py` (~30 lines)
- [ ] Optionally wire dashboard to consume SSE with polling fallback
- [ ] Create `config/grafana/bastion-dashboard.json`
- [ ] Create `config/prometheus/scrape.yml`
- [ ] Create `config/prometheus/alerts.yml`
- [ ] Write tests for SSE endpoint

### Verification
```bash
python -m pytest tests/ -v
```
Confirm: Dashboard shows all 12 new data dimensions. Alert conditions trigger visual changes. SSE endpoint streams events. Grafana artifacts are valid JSON/YAML.

---

## Phase 5: Integration Testing + Documentation

**Depends on**: All previous phases
**Estimated agents**: 2 parallel

### Tasks

#### Agent 1: Integration Tests
- [ ] End-to-end test: submit request -> verify it appears in queue/details -> verify metrics increment -> verify audit log entry -> verify RequestID correlation
- [ ] Verify all 14 new endpoints in two-port mode
- [ ] Verify backward compatibility: existing client code still works
- [ ] Run full test suite, fix any regressions

#### Agent 2: Documentation Update
- [ ] Update `docs/api.md` with all 14 new endpoints
- [ ] Update `docs/architecture.md` with observability layer description
- [ ] Update `README.md` with observability section (Prometheus, Grafana, dashboard)
- [ ] Update `config/broker.example.yaml` with any new config options
- [ ] Update `ROADMAP.md` — mark observability items as completed

### Verification
```bash
python -m pytest tests/ -v
```
Full green. Documentation matches implementation.

---

## Parallelization Map

```
Phase 1 (3 agents)  ──> Phase 2 (3 agents) ──> Phase 3 (4 agents) ──> Phase 4 (3 agents) ──> Phase 5 (2 agents)
```

Phases are SEQUENTIAL (not parallel) because:
- Phase 2 needs Phase 1's model changes to `BrokerStatus`
- Phases 1 and 2 both modify `server.py` — parallel execution causes merge conflicts
- Phase 3 needs endpoints from Phase 1 and metrics from Phase 2
- Phase 4 needs all new endpoints from Phase 3

Within each phase, agents run in parallel on different files.
Phase 3 depends on Phase 1 (needs wired status fields).
Phase 4 depends on Phase 3 (needs new endpoints).
Phase 5 depends on all phases.

## Total Agent Usage

| Phase | Agents | Focus |
|-------|--------|-------|
| 1 | 3 | Status wiring, model fixes, A2A stats |
| 2 | 3 | Scheduler metrics, non-scheduler metrics, gauge updater |
| 3 | 4 | Diagnostics endpoints, A2A endpoints, query endpoints, middleware |
| 4 | 3 | Dashboard data, dashboard alerts, SSE + Grafana |
| 5 | 2 | Integration tests, documentation |
| **Total** | **15 agents across 5 phases** | |

## Deliverables Checklist

At the end of all phases, verify:
- [ ] `/broker/status` returns 7+ new fields (vram_ledger, total_requests_served, swap_rate_level, stall_reason, inflight_models, circuit_breaker, max_vram_gb)
- [ ] All 17 Prometheus metrics emit data
- [ ] All 5 OTel spans produce traces
- [ ] 14 new API endpoints functional and authenticated
- [ ] RequestID middleware active on all routes
- [ ] Dashboard shows 12 additional data dimensions
- [ ] 6 alert conditions trigger visual changes
- [ ] SSE streaming endpoint operational
- [ ] Grafana dashboard JSON + Prometheus config shipped
- [ ] Admin routes rate-limited in two-port mode
- [ ] All tests pass
- [ ] Documentation updated
