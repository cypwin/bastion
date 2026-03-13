# Phase 3 — Protocol: S7 (A2A Agent Interface)

> Paste this entire prompt into a fresh Claude Code session in `/home/user/BASTION`.
> **Prerequisites**: Phase 1 (S3 + S4) and Phase 2 (S5 + S6 + S8) must be committed. Verify:
> ```bash
> git log --oneline -6  # Should show S3, S4, S5, S6, S8 commits
> ```

## Goal

Implement S7 from `ROADMAP.md`: full A2A (Agent-to-Agent) protocol support. This turns BASTION from an internal proxy into a discoverable GPU broker that any A2A-compliant agent can find and use. S9 (Multi-GPU) is deferred as aspirational — it requires hardware we may not have.

## Constraints

- Read `ROADMAP.md` section S7 for full spec and all 4 technical decisions.
- The existing agent card placeholder at `server.py` `/.well-known/agent-card.json` must be evolved, not replaced.
- `a2a-sdk[http-server]>=0.3` is already declared in `pyproject.toml:35`.
- Python: `/home/user/miniforge3/envs/bastion/bin/python`
- `from __future__ import annotations` in every new `.py` file.

## Team Structure

Create **one team** (A2A is deeply interconnected — splitting across teams would create more coordination overhead than benefit):

### Team F: `a2a-protocol` (S7)

| Agent | Model | Role |
|-------|-------|------|
| `a2a-architect` | opus | Read ROADMAP.md S7 thoroughly. Study the existing agent card at server.py:244-278. Study the A2A SDK API (use WebSearch if needed for current a2a-sdk docs). Design the task lifecycle state machine, skill routing, and integration with AffinityQueue. Produce detailed implementation plan. Review all code. |
| `a2a-core` | sonnet | Create `src/bastion/a2a.py` (~200 LOC): A2A protocol handler using a2a-sdk. Task lifecycle: submitted -> working -> completed/failed. Route tasks to skill handlers. Integrate with AffinityQueue — A2A tasks become QueuedRequest entries. Update `server.py` to mount A2A routes at `/a2a/*`. Make agent card dynamic (report current VRAM, loaded models, queue depth). |
| `a2a-skills` | sonnet | Implement skill handlers: (1) `infer` — single-prompt inference via scheduler queue, (2) `batch_infer` (~100 LOC) — N prompts for same model, single model load guaranteed, partial results on failure, (3) `preload`/reservation (~80 LOC) — reserve model for N requests with timeout safety net (default 10min), scheduler defers eviction. |
| `a2a-streaming` | sonnet | Implement A2A streaming (~80 LOC): SSE transport bridging A2A streaming protocol to BASTION's NDJSON streaming from Ollama. Implement A2A auth (~40 LOC): bearer token validation for `/a2a/*` routes, tokens in `broker.yaml` under `a2a.tokens`, agent card endpoint stays public. Implement capability negotiation (~40 LOC): dynamic card based on runtime state. |
| `a2a-tests` | sonnet | Write `tests/test_a2a.py` (~150 LOC): test task lifecycle (submit/working/complete/fail), batch inference with partial failures, reservation creation and expiry, dynamic agent card reflects state, auth rejects invalid tokens, SSE streaming emits events. Mock Ollama backend for deterministic tests. Run FULL suite. |

**S7 technical decisions (pre-resolved per ROADMAP.md):**
- Use A2A SDK (already declared as optional dep)
- Async task model with SSE streaming
- Partial results for batch inference (per-prompt status)
- Request-count-based reservations with timeout safety net

## Workflow

1. Create team.
2. Architect reads ROADMAP.md S7, studies a2a-sdk, produces plan.
3. After plan approval, `a2a-core` implements the protocol handler and server integration first (other agents depend on the routing skeleton).
4. `a2a-skills` and `a2a-streaming` work in parallel once the core skeleton is in place.
5. `a2a-tests` writes tests after implementation, runs full suite.
6. **E2E validation** (manual, print commands for me):
   ```bash
   # Discover BASTION via A2A
   pip install "a2a-sdk[http-client]>=0.3"
   python -c "
   from a2a.client import A2AClient
   c = A2AClient('http://localhost:11434')
   card = c.agent_card()
   print(f'Agent: {card.name}')
   print(f'Skills: {[s.id for s in card.skills]}')
   "
   ```
7. Commit:
   ```
   git add <S7 files> && git commit -m "feat(S7): implement A2A protocol with batch inference, reservations, and SSE streaming"
   ```

## Success Criteria

- [ ] Agent card at `/.well-known/agent-card.json` is dynamic (shows current VRAM, loaded models)
- [ ] `POST /a2a/tasks` creates a task, returns task ID
- [ ] Single-prompt inference task completes through full lifecycle
- [ ] Batch inference with 5 prompts returns 5 results (partial success on failure)
- [ ] Model reservation prevents eviction for N requests, auto-expires after timeout
- [ ] SSE streaming delivers token-by-token output for long inference
- [ ] Unauthenticated `/a2a/tasks` returns 401; valid bearer token returns 200
- [ ] ALL existing tests (S1-S6, S8) still pass
- [ ] New A2A tests pass

---
