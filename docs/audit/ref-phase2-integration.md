# Phase 2 — Integration: S5 (Dashboard) + S6 (the-batch-client) + S8 (Hardening)

> Paste this entire prompt into a fresh Claude Code session in `/home/user/BASTION`.
> **Prerequisites**: Phase 1 (S3 + S4) must be committed. Verify with:
> ```bash
> git log --oneline -4  # Should show S3 and S4 commits
> ```

## Goal

Implement S5, S6, and S8 from `ROADMAP.md` in parallel using three agent teams. S5 evolves the TUI dashboard. S6 unifies the-batch-client's GPU management under BASTION. S8 adds auth, rate limiting, circuit breaker, and extracts 14 hardcoded values.

## Constraints

- Read `ROADMAP.md` sections S5, S6, S8, and Appendix B+C for full specs.
- Read `CLAUDE.md` for project rules.
- Python: `/home/user/miniforge3/envs/bastion/bin/python`
- Never run tests automatically — print commands for me.
- `from __future__ import annotations` in every new `.py` file.

## Team Structure

Create **three teams** running in parallel:

### Team C: `dashboard-evolution` (S5)

| Agent | Model | Role |
|-------|-------|------|
| `s5-lead` | opus | Read ROADMAP.md S5 + `src/bastion/dashboard.py`. Map existing dead code (sparkline function, history deques). Produce plan, coordinate, review. |
| `s5-widgets` | sonnet | Wire existing `sparkline()` (dashboard.py:85) into GPUPanel and QueuePanel. Add `queue_history` deque. Create AlertPanel with severity tiers (info/warn/critical). Create SafetyLimitsBar widget (VRAM budget visualization). |
| `s5-actions` | sonnet | Add keyboard-driven interactive actions: preload (`p`), unload (`u`), drain (`d`) with Textual modal confirmations. Add request trace viewer panel. Create `/broker/recent` endpoint in server.py (in-memory deque of last 50 requests). |
| `s5-tests` | haiku | Write `tests/test_dashboard.py`: test sparkline output, alert thresholds, SafetyLimitsBar ranges. Run full test suite. |

**S5 key note**: The sparkline function and history deques ALREADY EXIST but are never rendered. The primary work is wiring, not creating.

### Team D: `swarm-convergence` (S6)

| Agent | Model | Role |
|-------|-------|------|
| `s6-lead` | opus | Read ROADMAP.md S6. Design bastion-client package structure, session profiles schema, and model intent API. The client library is a SEPARATE pip-installable package — plan its structure as a subdirectory `clients/bastion-client/`. |
| `s6-client` | sonnet | Create `clients/bastion-client/` package: `__init__.py`, `client.py` (~120 LOC) with `BastionClient` class: `declare_intent(profile, model_sequence)`, `infer(model, prompt, priority)`, `check_vram()`. Auto-injects `X-Broker-Priority` headers. `setup.py`/`pyproject.toml` for pip install. |
| `s6-server` | sonnet | Add session profiles to `models.py` + `config/broker.yaml` (named profiles like `council_pipeline` with model sequences). Add `POST /broker/intent` endpoint to `server.py` (~40 LOC). Wire priority mapping: council=INTERACTIVE, extraction=PIPELINE, embedding=BACKGROUND. |
| `s6-tests` | sonnet | Write `tests/test_intent.py` and `clients/bastion-client/tests/test_client.py`. Test: intent registration, priority header injection, session profile parsing. Run full suite. |

**S6 note**: We are NOT modifying the-batch-client itself in this phase — only building the BASTION-side infrastructure (server endpoints + client library). the-batch-client integration is a separate task for the the-batch-client repo.

### Team E: `production-hardening` (S8)

| Agent | Model | Role |
|-------|-------|------|
| `s8-lead` | opus | Read ROADMAP.md S8 + Appendix B (hardcoded values) + Appendix C (is_safe bug). Plan the config extraction, auth middleware, and circuit breaker. This session has 7 deliverables — sequence them to avoid merge conflicts. |
| `s8-auth` | sonnet | Create `src/bastion/auth.py` (~80 LOC): bearer-token middleware for `/broker/*`. Keys from `broker.yaml` `auth:` section. Proxy routes (`/api/*`) remain open. Create `src/bastion/ratelimit.py` (~70 LOC): token-bucket per client IP, configurable per tier. Returns 429 with `Retry-After`. |
| `s8-circuit` | sonnet | Create `src/bastion/circuitbreaker.py` (~90 LOC): three-state machine (closed/open/half-open). Wrap Ollama HTTP calls in proxy.py. Configurable failure_threshold (default 5) and recovery_timeout (default 30s). Add graceful degradation: cached `/api/tags`, clear error messages. |
| `s8-config` | sonnet | Extract all 14 hardcoded values from Appendix B into `broker.yaml` with backward-compatible defaults. Fix `GPUStatus.is_safe` bug (Appendix C): make `/broker/health` use `check_gpu_safe(config.gpu)` instead of the hardcoded 85C check. Add `/broker/livez` and `/broker/readyz` endpoints. Add request body validation to proxy.py. |
| `s8-tests` | sonnet | Write `tests/test_auth.py`, `tests/test_ratelimit.py`, `tests/test_circuitbreaker.py`. Test: auth rejects unauthenticated, rate limiter returns 429, circuit breaker opens after N failures and recovers. Verify is_safe bug fix. Run full suite. |

## Cross-Team Communication

These three teams modify overlapping files:
- `server.py` — touched by S5 (`/broker/recent`), S6 (`/broker/intent`), S8 (auth middleware, livez/readyz)
- `models.py` — touched by S6 (session profiles), S8 (config extraction, is_safe fix)
- `proxy.py` — touched by S8 (circuit breaker, validation, config extraction)

**Conflict prevention strategy**:
1. S8-config works LAST on shared files (after S5 and S6 merge their endpoints).
2. Each team adds endpoints to `server.py` in separate, clearly-demarcated sections with comments.
3. The team lead (you, the orchestrator) reviews all changes to shared files before committing.
4. If agents detect a merge conflict, they message the orchestrator rather than resolving it themselves.

## Workflow

1. Create all three teams simultaneously.
2. Architects read ROADMAP.md and produce plans (opus, plan mode).
3. Implementers work in parallel within each team.
4. S8-config agent waits for S5 and S6 to finish server.py changes before extracting hardcoded values.
5. Test agents run after implementation.
6. **Full regression**: run ALL tests (existing + new from Phase 1 + new from Phase 2):
   ```
   /home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v
   ```
7. Commit per session:
   ```
   git add <S5 files> && git commit -m "feat(S5): evolve TUI dashboard with sparklines, alerts, and interactive actions"
   git add <S6 files> && git commit -m "feat(S6): add bastion-client library, session profiles, and model intent API"
   git add <S8 files> && git commit -m "feat(S8): add auth, rate limiting, circuit breaker, extract hardcoded config"
   ```

## Success Criteria

**S5:**
- [ ] Sparklines render in GPU and Queue panels using existing deque histories
- [ ] Alert panel shows severity-tiered alerts (warn at 85% VRAM, critical at 95%)
- [ ] Keyboard shortcuts (p/u/d) open modal confirmations
- [ ] `/broker/recent` returns last 50 requests

**S6:**
- [ ] `bastion-client` is pip-installable from `clients/bastion-client/`
- [ ] `BastionClient.infer()` auto-injects `X-Broker-Priority` header
- [ ] `POST /broker/intent` accepts model sequences and returns 200
- [ ] Session profiles parseable from `broker.yaml`

**S8:**
- [ ] `/broker/status` returns 401 without auth token (when auth enabled)
- [ ] Rate limiter returns 429 with `Retry-After` after burst exceeded
- [ ] Circuit breaker opens after 5 failures, recovers through half-open
- [ ] `GPUStatus.is_safe` bug fixed — health endpoint matches scheduler threshold
- [ ] All 14 hardcoded values configurable via `broker.yaml`
- [ ] `/broker/livez` and `/broker/readyz` respond
- [ ] ALL existing + Phase 1 tests still pass
