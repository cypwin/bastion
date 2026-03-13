# Phase 1 — Foundation: S3 (Scheduler Intelligence) + S4 (Observability)

> Paste this entire prompt into a fresh Claude Code session in `/home/user/BASTION`.
> These two sessions have zero dependencies and enable all subsequent phases.

## Goal

Implement S3 and S4 from `ROADMAP.md` in parallel using two agent teams, then commit. S3 is the #1 production fix (co-resident model cooldown waste). S4 provides the metrics infrastructure every later session needs.

## Constraints

- Read `ROADMAP.md` sections S3 and S4 for full specifications, deliverables tables, and technical decisions.
- Read `CLAUDE.md` for project rules (type hints, import order, `from __future__ import annotations`, archive-don't-delete, session-tagged commits).
- Python invocation: `/home/user/miniforge3/envs/bastion/bin/python`
- Never run tests automatically — print commands for me.
- All new files need `from __future__ import annotations`.

## Team Structure

Create **two teams** running in parallel:

### Team A: `scheduler-intelligence` (S3)

| Agent | Model | Role | Writes to |
|-------|-------|------|-----------|
| `s3-architect` | opus | Read ROADMAP.md S3. Design the residency cache, modified `needs_swap` logic, and `ResidencyState` model. Produce implementation plan, then review all code from teammates before final approval. | Plan only |
| `s3-scheduler` | sonnet | Implement scheduler.py changes: replace `_current_model: str` with residency-aware set, modify `needs_swap` (line 184-187) to check VRAM residency, skip cooldown for co-resident transitions (lines 189-203), update `_sync_current_model` (lines 274-295) to track all loaded models. | `src/bastion/scheduler.py` |
| `s3-vram` | sonnet | Implement residency cache in vram.py: add `ResidencyCache` class with 1s TTL wrapping `get_loaded_models()`, expose `is_model_resident(name)` method, invalidate cache on BASTION-initiated load/unload. Add `ResidencyState` to models.py. Update `config/broker.yaml` with `ollama_max_loaded_models` and `residency_cache_ttl_seconds`. | `src/bastion/vram.py`, `src/bastion/models.py`, `config/broker.yaml` |
| `s3-tests` | sonnet | Write `tests/test_residency.py` (~115 LOC): test co-resident skip-cooldown, eviction-needed swap triggers real cooldown, residency cache expiry, `ResidencyState` serialization, interaction with affinity bonus. After teammates finish, run the FULL test suite and report results. | `tests/test_residency.py` |

**S3 technical decisions (pre-resolved per ROADMAP.md):**
- Residency tracking: cached `/api/ps` with 1s TTL
- Cooldown: skip entirely for co-resident, with synchronous cache refresh as safety check
- LRU eviction: hybrid (`OLLAMA_MAX_LOADED_MODELS=3` + `keep_alive` management)

### Team B: `observability` (S4)

| Agent | Model | Role | Writes to |
|-------|-------|------|-----------|
| `s4-architect` | opus | Read ROADMAP.md S4. Design metrics module with lazy prometheus-client import (no-op fallbacks), audit log schema, middleware integration points. Produce implementation plan, review all code. | Plan only |
| `s4-metrics` | sonnet | Create `src/bastion/metrics.py` (~120 LOC): Prometheus registry with try/except import, no-op fallbacks. Counters: `bastion_requests_total`, `bastion_model_swap_total`, `bastion_cooldown_waits_total`. Histograms: `bastion_request_duration_seconds` (buckets: 0.1-300), `bastion_queue_wait_seconds` (buckets: 0.01-30). Gauges: `bastion_queue_depth`, `bastion_vram_used_bytes`, `bastion_gpu_temperature_celsius`. Create `src/bastion/middleware.py` (~80 LOC): FastAPI middleware recording duration/status/endpoint/model. Wire `/broker/metrics` endpoint into `server.py`. | `src/bastion/metrics.py`, `src/bastion/middleware.py`, `src/bastion/server.py` |
| `s4-audit` | sonnet | Create `src/bastion/audit.py` (~60 LOC): structured JSON-lines logger using Python `logging` + `RotatingFileHandler`. Events: `swap`, `vram_alert`, `queue_change`, `request_complete`. Fields: `timestamp`, `event`, `details`. Integrate emit calls into `scheduler.py` (swap path), `proxy.py` (request complete), `vram.py` (threshold events). | `src/bastion/audit.py`, instrument existing files |
| `s4-tests` | sonnet | Write `tests/test_metrics.py` and `tests/test_audit.py`. Test: metrics increment on request, histogram records duration, audit log writes valid JSON lines, no-op fallback when prometheus-client missing. Run FULL test suite after all code is written. | `tests/test_metrics.py`, `tests/test_audit.py` |

**S4 technical decisions (pre-resolved per ROADMAP.md):**
- Library: prometheus-client (already in pyproject.toml optional deps)
- Audit format: JSON lines with RotatingFileHandler
- Cardinality: cap model labels to configured models + `_other` catch-all

## Workflow

1. Create both teams simultaneously.
2. Architects on both teams read ROADMAP.md and produce plans (opus, plan mode).
3. After plan approval, implementers work in parallel within each team.
4. Test agents run after all implementation in their team is complete.
5. **Cross-team validation**: after both teams finish, run the FULL existing test suite to confirm no regressions:
   ```
   /home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v
   ```
6. If all tests pass, create TWO commits (one per session):
   ```
   git add <S3 files> && git commit -m "feat(S3): residency-aware scheduling with co-resident cooldown skip"
   git add <S4 files> && git commit -m "feat(S4): add Prometheus metrics, request middleware, and audit logging"
   ```

## Success Criteria

- [ ] Alternating requests between co-resident models skip cooldown (no 2s delay)
- [ ] Cold-load transitions still enforce full cooldown
- [ ] `ResidencyCache` with 1s TTL, invalidated on BASTION load/unload
- [ ] `curl localhost:11434/broker/metrics | grep bastion_` returns valid Prometheus exposition
- [ ] Audit log writes structured JSON lines on model swaps
- [ ] ALL existing tests (89) still pass
- [ ] New tests for residency and metrics pass
