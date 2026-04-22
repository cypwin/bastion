# Phase 2c — S8: Production Hardening

> Paste this entire prompt into a fresh Claude Code session in `/home/user/BASTION`.
> **Prerequisites**: Phase 1 (S3 + S4) must be committed. Verify with:
> ```bash
> git log --oneline -4  # Should show S3 and S4 commits
> ```

## Goal

Implement S8 from `ROADMAP.md`: add authentication, rate limiting, circuit breaker, extract all 14 hardcoded values into config, and fix the `is_safe` bug. This is the heaviest session with 7 deliverables.

## Constraints

- Read `ROADMAP.md` section S8 + Appendix B (hardcoded values) + Appendix C (is_safe bug) for full specs.
- Read `CLAUDE.md` for project rules.
- Python: `/home/user/miniforge3/envs/bastion/bin/python`
- Never run tests automatically — print commands for me.
- `from __future__ import annotations` in every new `.py` file.

## Team Structure

Create **one team**: `production-hardening`

| Agent | Model | Role |
|-------|-------|------|
| `s8-lead` | opus | Read ROADMAP.md S8 + Appendix B (hardcoded values) + Appendix C (is_safe bug). Plan the config extraction, auth middleware, and circuit breaker. This session has 7 deliverables — sequence them to avoid merge conflicts. |
| `s8-auth` | sonnet | Create `src/bastion/auth.py` (~80 LOC): bearer-token middleware for `/broker/*`. Keys from `broker.yaml` `auth:` section. Proxy routes (`/api/*`) remain open. Create `src/bastion/ratelimit.py` (~70 LOC): token-bucket per client IP, configurable per tier. Returns 429 with `Retry-After`. |
| `s8-circuit` | sonnet | Create `src/bastion/circuitbreaker.py` (~90 LOC): three-state machine (closed/open/half-open). Wrap Ollama HTTP calls in proxy.py. Configurable failure_threshold (default 5) and recovery_timeout (default 30s). Add graceful degradation: cached `/api/tags`, clear error messages. |
| `s8-config` | sonnet | Extract all 14 hardcoded values from Appendix B into `broker.yaml` with backward-compatible defaults. Fix `GPUStatus.is_safe` bug (Appendix C): make `/broker/health` use `check_gpu_safe(config.gpu)` instead of the hardcoded 85C check. Add `/broker/livez` and `/broker/readyz` endpoints. Add request body validation to proxy.py. |
| `s8-tests` | sonnet | Write `tests/test_auth.py`, `tests/test_ratelimit.py`, `tests/test_circuitbreaker.py`. Test: auth rejects unauthenticated, rate limiter returns 429, circuit breaker opens after N failures and recovers. Verify is_safe bug fix. Run full suite. |

## Internal Sequencing

This session has the most file overlap internally. Sequence work carefully:

1. **`s8-auth`** and **`s8-circuit`** work in parallel (separate new files, no overlap).
2. **`s8-config`** works LAST — it touches `server.py`, `models.py`, `proxy.py`, and `broker.yaml` which other agents may also modify. It should wait for `s8-auth` and `s8-circuit` to finish before extracting hardcoded values from shared files.
3. **`s8-tests`** runs after all implementation is done.

## Cross-Team Coordination Notes

This session is one of three parallel Phase 2 sessions (S5, S6, S8). If running all three:

- **`server.py`** is shared with S5 (`/broker/recent`) and S6 (`/broker/intent`).
- **`models.py`** is shared with S6 (session profiles).
- **`proxy.py`** is only touched by S8 (circuit breaker, validation, config extraction).
- **S8-config should run LAST** across all three sessions if possible, since it extracts hardcoded values from files that S5 and S6 also modify.
- If you detect merge conflicts from another session's changes, stop and report rather than resolving.

If running this session **standalone**, just be aware that S5/S6 may have added endpoints to `server.py` and models to `models.py` that you'll need to work around.

## Workflow

1. Create the team.
2. `s8-lead` reads ROADMAP.md S8 + Appendices B and C, produces sequenced plan (opus, plan mode).
3. `s8-auth` and `s8-circuit` implement in parallel (new files only).
4. `s8-config` implements after `s8-auth` and `s8-circuit` are done (touches shared files).
5. `s8-tests` writes and runs tests after all implementation.
6. **Full regression**:
   ```
   /home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v
   ```
7. Commit:
   ```
   git add <S8 files> && git commit -m "feat(S8): add auth, rate limiting, circuit breaker, extract hardcoded config"
   ```

## Success Criteria

- [ ] `/broker/status` returns 401 without auth token (when auth enabled)
- [ ] Rate limiter returns 429 with `Retry-After` after burst exceeded
- [ ] Circuit breaker opens after 5 failures, recovers through half-open
- [ ] `GPUStatus.is_safe` bug fixed — health endpoint matches scheduler threshold
- [ ] All 14 hardcoded values configurable via `broker.yaml`
- [ ] `/broker/livez` and `/broker/readyz` respond
- [ ] ALL existing + Phase 1 tests still pass
