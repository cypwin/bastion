# Pre-Launch Audit Design

**Date:** 2026-04-08
**Status:** Approved
**Goal:** Comprehensive multi-agent code review and documentation audit before public GitHub launch

## Context

BASTION v0.3.0 is feature-complete for public release. Before setting up the GitHub repository and publishing to PyPI, a thorough audit is needed to ensure:

- New users can install and configure BASTION without friction
- Edge cases are handled properly across all API endpoints
- Async/concurrency patterns are sound (no races, no orphaned state)
- Security boundaries are correct for the threat model
- Documentation matches the actual code
- Dashboard panels show useful, accurate, real-time data
- Test coverage has no critical gaps

## Review Architecture

### Phase 1: Parallel Review (8 agents, read-only)

All agents run simultaneously. No code changes during this phase. Each produces a structured findings report saved to `docs/design/reviews/2026-04-08-<agent-name>.md`.

### Phase 2: Merge & Prioritize

Consolidate all 8 reports into a single master report at `docs/design/reviews/2026-04-08-pre-launch-audit.md`. Deduplicate cross-agent findings with references.

### Phase 3: User Review

User reviews master report, approves/defers/rejects each finding.

### Phase 4: Fix Pass

Worktree branch for approved fixes. Subagent-driven, one agent per category. Tests + lint verified.

### Phase 5: User Stories (optional follow-up)

Role-play scenarios to validate fixes improved the experience.

## Findings Format

Each agent report uses this structure:

```markdown
## [Agent Name] Review

### Critical (blocks launch)
- Finding with file:line reference and explanation

### Important (should fix before launch)
- ...

### Minor (nice to have)
- ...

### Observations (not actionable, but worth knowing)
- ...
```

## Agent Team

### Agent 1 — Onboarding & Setup

**Persona:** Developer who just discovered BASTION, has an NVIDIA GPU + Ollama installed.

Reviews:
- README quickstart: do the commands work? Missing steps?
- `pip install bastion-broker` -> `bastion --help` -> first config -> first request path
- `--detect-models` and config generation flow
- `config/broker.example.yaml` -> `config/broker.yaml` copy-and-edit experience
- All 4 example directories: correct API references? Would they run?
- Docker path: does `docker compose up` work from README instructions?
- Error messages when things go wrong (Ollama not running, wrong port, no GPU)

### Agent 2 — API & Edge Cases

Reviews every endpoint in `server.py` and `a2a.py`:
- Malformed JSON bodies, missing required fields, wrong types
- Empty strings, null values, negative numbers where positive expected
- Concurrent requests to same model, different models
- Queue full (503), timeout (504), Ollama down (502) paths
- Streaming: partial NDJSON, client disconnect mid-stream
- Priority header: invalid tier names, missing header, case sensitivity
- Intent declaration: duplicate intents, expired intents
- A2A: task state machine transitions, lease expiry race conditions

### Agent 3 — Async & Concurrency Safety

Reviews:
- Every `asyncio.Lock` / `asyncio.Event` — coverage complete? Unguarded shared state?
- VRAM ledger: assume/confirm/forget — crash leaving orphaned assumptions?
- Scheduler loop: two scheduling cycles overlapping?
- Task store compaction: concurrent read during compaction?
- Circuit breaker state transitions under concurrent failures
- Watchdog: nvidia-smi hangs forever (beyond 5s timeout)?
- Graceful shutdown: all background tasks properly cancelled?

### Agent 4 — Security & Hardening

Reviews:
- Auth bypass: admin endpoints reachable without API key when auth enabled?
- Rate limiter: IP spoofing via X-Forwarded-For? Reset attacks?
- Header injection via `X-Broker-Priority` or other user-controlled headers
- Audit log: malicious request causing log injection (JSONL line break)?
- Config exposure: does `/broker/status` leak sensitive config?
- A2A tokens: empty token list = open access — clear enough?
- Request body size: any limits? OOM via huge prompt?

### Agent 5 — Docs & Packaging

Reviews:
- Cross-reference every claim in README/api.md/architecture.md against code
- Version consistency: pyproject.toml, `__init__.py`, CHANGELOG, README
- All URLs: identify every GitHub/PyPI reference needing update at launch
- Missing standard files: CODE_OF_CONDUCT.md, .github/FUNDING.yml
- CONTRIBUTING.md: does described workflow match actual CI/CD?
- Classifier accuracy in pyproject.toml
- Client library README at `clients/bastion-client/`

### Agent 6 — Dashboard: Panel Inventory

Reviews every widget in the dashboard package:
- Enumerate all panels across compact/standard/full layouts
- For each panel: what data displayed, what endpoint feeds it, real or placeholder?
- GPU panel: temperature, VRAM bar, fan speed, power draw — live or stubbed?
- Queue panel: all priority tiers shown? Aging visualization?
- A2A panel: task states, lease info, SSE connection status
- Alert panel: thresholds, triggers, usefulness
- StatusBar: connection state, stale indicator, layout mode tag

### Agent 7 — Dashboard: Operator UX

Reviews:
- Every keybinding listed in SafetyLimitsBar — does each work?
- Interactive actions: [p]reload, [u]nload, [d]rain, [r]efresh, [g]pu-kill, [s]vc-restart
- Disconnected state: stale data display?
- Layout switching: [1] compact, [2] standard, [3] full — smooth transitions?
- Error modals: what does operator see when action fails?
- Sparkline history: configurable via `--sparkline-width` and `--history-len`?
- Help modal: is [h]elp comprehensive and accurate?

### Agent 8 — Test Coverage Gaps

Reviews:
- Map every public function/method in `src/bastion/` to its test(s)
- Identify untested public API surface
- Mock fidelity: do mock Ollama responses match real Ollama response format?
- Edge case tests for concerns from agents 2-4
- Test isolation: execution order dependencies? Shared state?
- Async test safety: proper pytest-asyncio usage, no unfinished coroutines

## Codebase Reference

```
src/bastion/           — 39 modules, ~13,500 LOC
tests/                 — 33 files, ~13,900 LOC, 724 tests
docs/                  — 7 documentation files
config/                — broker.yaml, broker.example.yaml
clients/bastion-client/ — Python client library v0.2.0
examples/              — 4 quickstart examples
.github/workflows/     — ci.yml, release.yml
```

Key large files:
- `a2a.py` (75.7 KB) — A2A protocol, largest module
- `server.py` (71.9 KB) — FastAPI app + admin routes
- `dashboard/app.py` (32.1 KB) — TUI main
- `scheduler.py` (30.5 KB) — Scheduling loop
- `vram.py` (24.1 KB) — VRAM tracking

## Success Criteria

- All Critical findings addressed before launch
- All Important findings addressed or explicitly deferred with rationale
- Master report committed as permanent record
- Tests pass (724+), lint clean
