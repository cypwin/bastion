# Phase 2b — S6: SWARM_BRAIN Convergence

> Paste this entire prompt into a fresh Claude Code session in `/home/cyprian/BASTION`.
> **Prerequisites**: Phase 1 (S3 + S4) must be committed. Verify with:
> ```bash
> git log --oneline -4  # Should show S3 and S4 commits
> ```

## Goal

Implement S6 from `ROADMAP.md`: build the BASTION-side infrastructure for SWARM_BRAIN integration — a pip-installable client library, session profiles, and a model intent API. We are NOT modifying SWARM_BRAIN itself; that's a separate task for the SWARM_BRAIN repo.

## Constraints

- Read `ROADMAP.md` section S6 for full specs.
- Read `CLAUDE.md` for project rules.
- Python: `/home/cyprian/miniforge3/envs/phenotype/bin/python`
- Never run tests automatically — print commands for me.
- `from __future__ import annotations` in every new `.py` file.

## Team Structure

Create **one team**: `swarm-convergence`

| Agent | Model | Role |
|-------|-------|------|
| `s6-lead` | opus | Read ROADMAP.md S6. Design bastion-client package structure, session profiles schema, and model intent API. The client library is a SEPARATE pip-installable package — plan its structure as a subdirectory `clients/bastion-client/`. |
| `s6-client` | sonnet | Create `clients/bastion-client/` package: `__init__.py`, `client.py` (~120 LOC) with `BastionClient` class: `declare_intent(profile, model_sequence)`, `infer(model, prompt, priority)`, `check_vram()`. Auto-injects `X-Broker-Priority` headers. `setup.py`/`pyproject.toml` for pip install. |
| `s6-server` | sonnet | Add session profiles to `models.py` + `config/broker.yaml` (named profiles like `council_pipeline` with model sequences). Add `POST /broker/intent` endpoint to `server.py` (~40 LOC). Wire priority mapping: council=INTERACTIVE, extraction=PIPELINE, embedding=BACKGROUND. |
| `s6-tests` | sonnet | Write `tests/test_intent.py` and `clients/bastion-client/tests/test_client.py`. Test: intent registration, priority header injection, session profile parsing. Run full suite. |

**S6 note**: We are NOT modifying SWARM_BRAIN itself in this phase — only building the BASTION-side infrastructure (server endpoints + client library). SWARM_BRAIN integration is a separate task for the SWARM_BRAIN repo.

## Cross-Team Coordination Notes

This session is one of three parallel Phase 2 sessions (S5, S6, S8). If running all three:

- **`server.py`** is shared with S5 (`/broker/recent`) and S8 (auth middleware, livez/readyz).
- **`models.py`** is shared with S8 (config extraction, is_safe fix).
- Add your `/broker/intent` endpoint and session profile models in clearly-demarcated sections with comments.
- If you detect merge conflicts from another session's changes, stop and report rather than resolving.

If running this session **standalone**, these notes can be ignored.

## Workflow

1. Create the team.
2. `s6-lead` reads ROADMAP.md S6 and designs the package structure (opus, plan mode).
3. `s6-client` and `s6-server` implement in parallel.
4. `s6-tests` writes and runs tests after implementation.
5. **Full regression**:
   ```
   /home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/ -v
   ```
6. Commit:
   ```
   git add <S6 files> && git commit -m "feat(S6): add bastion-client library, session profiles, and model intent API"
   ```

## Success Criteria

- [ ] `bastion-client` is pip-installable from `clients/bastion-client/`
- [ ] `BastionClient.infer()` auto-injects `X-Broker-Priority` header
- [ ] `POST /broker/intent` accepts model sequences and returns 200
- [ ] Session profiles parseable from `broker.yaml`
- [ ] ALL existing + Phase 1 tests still pass
