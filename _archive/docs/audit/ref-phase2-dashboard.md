# Phase 2a — S5: Dashboard Evolution

> Paste this entire prompt into a fresh Claude Code session in `/home/user/BASTION`.
> **Prerequisites**: Phase 1 (S3 + S4) must be committed. Verify with:
> ```bash
> git log --oneline -4  # Should show S3 and S4 commits
> ```

## Goal

Implement S5 from `ROADMAP.md`: evolve the TUI dashboard with live sparklines, alert panels, interactive keyboard actions, and a recent-requests endpoint.

## Constraints

- Read `ROADMAP.md` section S5 for full specs.
- Read `CLAUDE.md` for project rules.
- Python: `/home/user/miniforge3/envs/bastion/bin/python`
- Never run tests automatically — print commands for me.
- `from __future__ import annotations` in every new `.py` file.

## Team Structure

Create **one team**: `dashboard-evolution`

| Agent | Model | Role |
|-------|-------|------|
| `s5-lead` | opus | Read ROADMAP.md S5 + `src/bastion/dashboard.py`. Map existing dead code (sparkline function, history deques). Produce plan, coordinate, review. |
| `s5-widgets` | sonnet | Wire existing `sparkline()` (dashboard.py:85) into GPUPanel and QueuePanel. Add `queue_history` deque. Create AlertPanel with severity tiers (info/warn/critical). Create SafetyLimitsBar widget (VRAM budget visualization). |
| `s5-actions` | sonnet | Add keyboard-driven interactive actions: preload (`p`), unload (`u`), drain (`d`) with Textual modal confirmations. Add request trace viewer panel. Create `/broker/recent` endpoint in server.py (in-memory deque of last 50 requests). |
| `s5-tests` | haiku | Write `tests/test_dashboard.py`: test sparkline output, alert thresholds, SafetyLimitsBar ranges. Run full test suite. |

**S5 key note**: The sparkline function and history deques ALREADY EXIST but are never rendered. The primary work is wiring, not creating.

## Cross-Team Coordination Notes

This session is one of three parallel Phase 2 sessions (S5, S6, S8). If running all three:

- **`server.py`** is shared with S6 (`/broker/intent`) and S8 (auth middleware, livez/readyz).
- Add your `/broker/recent` endpoint in a clearly-demarcated section with comments.
- If you detect merge conflicts from another session's changes, stop and report rather than resolving.

If running this session **standalone**, these notes can be ignored.

## Workflow

1. Create the team.
2. `s5-lead` reads ROADMAP.md S5 and `src/bastion/dashboard.py`, produces plan (opus, plan mode).
3. `s5-widgets` and `s5-actions` implement in parallel.
4. `s5-tests` writes and runs tests after implementation.
5. **Full regression**:
   ```
   /home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v
   ```
6. Commit:
   ```
   git add <S5 files> && git commit -m "feat(S5): evolve TUI dashboard with sparklines, alerts, and interactive actions"
   ```

## Success Criteria

- [ ] Sparklines render in GPU and Queue panels using existing deque histories
- [ ] Alert panel shows severity-tiered alerts (warn at 85% VRAM, critical at 95%)
- [ ] Keyboard shortcuts (p/u/d) open modal confirmations
- [ ] `/broker/recent` returns last 50 requests
- [ ] ALL existing + Phase 1 tests still pass
