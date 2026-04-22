# BASTION Production Roadmap — Master Prompt

> Paste this into a new Claude Code session to continue the production roadmap.
> Last updated: 2026-04-08 after completing Phase 1 + Phase 2 + Phase 3.1 + Phase 3.2.

---

## What is BASTION?

BASTION (Batch Affinity Scheduler for Throttled Inference on Ollama Networks) is a system-level GPU inference broker. It sits as a transparent HTTP proxy in front of Ollama on port 11434, preventing GPU crashes from concurrent model loading. Features: affinity queue with priority tiers, VRAM budget enforcement, model scheduling with cooldown, A2A agent interface, TUI dashboard.

- **Repo**: `/home/user/BASTION`
- **Version**: 0.3.0
- **License**: MIT
- **Python**: 3.11+
- **Stack**: FastAPI + uvicorn + httpx + Pydantic v2 + PyYAML

## What's been completed

### Phase 1 (v0.3.0) — PyPI-Ready ✅

- `src/bastion/paths.py` — XDG directory resolution (`~/.local/share/bastion/`, env var overrides)
- Replaced all hardcoded `/tmp/` paths in `audit.py`, `vram.py`, `server.py`
- GPU auto-detection via nvidia-smi (`total_vram_gb: 0` = auto-detect, fallback to 8GB)
- `--init-config` CLI flag generates `~/.config/bastion/broker.yaml`
- Fan control graceful degradation (`fan_control_available()`, hidden in modal when absent)
- Config search path: `/etc/bastion/` Linux-only guard
- `CHANGELOG.md` (Keep a Changelog format)
- Python 3.13 in CI, updated classifiers/URLs in `pyproject.toml`

### Phase 2 (v0.3.0) — Any NVIDIA GPU ✅

- **GPU backend abstraction**: `src/bastion/gpu/` package
  - `base.py` — `GPUBackend` protocol (`query_status`, `get_vram_free_gb`, `query_processes`)
  - `nvidia.py` — nvidia-smi implementation (extracted from `health.py`)
  - `stub.py` — no-op backend for GPU-less systems
  - `__init__.py` — `detect_backend()` factory, `get_backend()` singleton
- `health.py` now delegates to `get_backend()` (backward-compatible public API)
- `watchdog.py` `_check_gpu()` uses GPU backend
- `dashboard/collectors.py` `query_gpu_processes()` uses GPU backend
- `src/bastion/discovery.py` — `--detect-models` CLI queries Ollama, prints YAML, guides to ollama.com/library
- `config.py` `_apply_env_overrides()` — 11 BASTION_* env vars for Docker/CI
- Improved startup messages (Ollama not running, no models, nvidia-smi missing)
- Example config ships with `models: {}` and guidance comments

### Phase 3.1 — Docker Image ✅

- `Dockerfile` — multi-stage build (python:3.12-slim), non-root `bastion` user, `.[persistence]` built-in
- `docker-compose.yml` — BASTION + Ollama with GPU, health checks, restart policies, named volumes
- `.dockerignore` — lean build context
- Desktop launcher (`scripts/launch_dashboard.sh`) hardened:
  - GPU device node creation (`nvidia-modprobe`)
  - Systemd Ollama detection (prefers systemctl over nohup)
  - nftables-aware health checks (`sg bastion` for port 11435 access)
  - Lock file prevents duplicate launches
  - Runs BASTION under `bastion` group
- `docs/deployment.md` — comprehensive guide (desktop, systemd, Docker, troubleshooting)

### Phase 3.2 — SQLite Persistence ✅

- `src/bastion/persistence.py` — DatabaseManager, PersistentAuditLog, PersistentTaskStore, PersistentQueue
- Schema migrations (versioned, WAL mode)
- Dual-write: in-memory stays primary, SQLite is durable archive
- PersistentTaskStore wired into A2A handler via `task_store` parameter
- 20 passing tests (`tests/test_persistence.py`)
- Config, env vars, paths, pyproject.toml extra all in place

### Key commits (Phase 3)

```
23099bc feat(docker): add .dockerignore for lean build context
7a6660d feat(docker): add multi-stage Dockerfile with persistence
eaf5e4d feat(docker): add docker-compose for BASTION + Ollama stack
c88bc55 feat(persistence): wire PersistentTaskStore into A2A handler
3194121 docs: add comprehensive deployment guide
```

## Current project structure

```
src/bastion/
├── __init__.py          # v0.3.0
├── __main__.py          # CLI: --config, --init-config, --detect-models, --port, etc.
├── a2a.py               # A2A protocol handler
├── audit.py             # Tiered JSONL audit logging (paths via bastion.paths)
├── auth.py              # API key + bearer token auth
├── circuitbreaker.py    # Three-state circuit breaker
├── config.py            # YAML loader + GPU auto-detect + env var overrides
├── discovery.py         # --detect-models (Ollama model discovery)
├── gpu/                 # GPU backend abstraction (Phase 2)
│   ├── __init__.py      # detect_backend(), get_backend(), set_backend()
│   ├── base.py          # GPUBackend protocol
│   ├── nvidia.py        # NvidiaBackend (nvidia-smi)
│   └── stub.py          # StubBackend (no-op)
├── dashboard/           # Textual TUI (package, not single file)
│   ├── __init__.py      # Dashboard CLI entry
│   ├── app.py           # BastionDashboard main app
│   ├── client.py        # httpx client for broker API
│   ├── collectors.py    # System metrics (CPU, mem, net, GPU processes)
│   ├── helpers.py       # Formatting helpers (sparkline, colors)
│   ├── modals.py        # Fan control, process kill, help modals
│   ├── panels_broker.py # Queue, scheduler, alerts, circuit breaker
│   ├── panels_gpu.py    # GPU, models, VRAM ledger
│   ├── panels_secondary.py # A2A tasks, audit, leases, traces
│   ├── panels_system.py # CPU, memory, network, temperature
│   └── statusbar.py     # Status bar + safety limits bar
├── health.py            # GPU health (delegates to gpu.get_backend())
├── metrics.py           # Prometheus (optional, no-op fallback)
├── middleware.py         # Request metrics middleware
├── models.py            # All Pydantic models (GPUConfig auto-detect defaults)
├── paths.py             # XDG path resolution (data_dir, config_dir, audit_log_path, etc.)
├── proxy.py             # Transparent Ollama proxy
├── queue.py             # AffinityQueue
├── ratelimit.py         # Per-IP rate limiting
├── scheduler.py         # Scheduling loop
├── server.py            # FastAPI app factory + admin routes
├── taskstore.py         # A2A task store
├── telemetry.py         # OpenTelemetry (optional, no-op fallback)
├── vram.py              # VRAM tracker (journal via bastion.paths)
└── watchdog.py          # Ollama monitor + systemd sd_notify

clients/bastion-client/  # Python client library (v0.1.0)
config/broker.example.yaml  # Example config (models: {}, auto-detect GPU)
tests/                   # 621+ passing tests
```

## Key decisions (do not revisit)

- **Target audience**: Linux + NVIDIA GPU users first. macOS/AMD best-effort later.
- **Persistence**: Optional SQLite for audit/task/queue. In-memory stays default.
- **Fan control**: Opt-in power feature, gracefully hidden when prerequisites absent.
- **In-memory state**: Intentional for single-machine scope. No external DB.
- **GPU abstraction**: `bastion.gpu` package with protocol — seam exists for future ROCm/Metal.
- **Model discovery**: Don't assume any specific models. Guide users via `--detect-models`.

## Patterns to follow

- `from __future__ import annotations` in every `.py` file
- Type hints on all functions
- Import order: stdlib → third-party → local (`bastion.*`)
- Git commits: `feat(scope): description` format
- Optional deps use try/except with no-op fallbacks (see `metrics.py`, `telemetry.py`)
- Never delete files — archive to `_archive/`
- Never run tests automatically — print the command for the user
- Tests don't require live Ollama or GPU — all mocked

## Test command

```bash
/home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v \
  --ignore=tests/test_e2e_stress.py \
  --ignore=tests/test_dashboard.py \
  --ignore=tests/test_observability_phase1.py
```

Note: `test_dashboard.py` and `test_observability_phase1.py` have pre-existing import errors from the dashboard package refactor (they import from the old single-file `bastion.dashboard`). These need updating but are not blocking.

## Remaining phases

### Phase 3: Docker and Persistence (v0.5.0)

**Goal**: Docker-ready. Optional SQLite persistence for audit and task recovery.

#### 3.1 Docker Image ✅ (completed 2026-04-07)

#### 3.2 Optional SQLite Persistence ✅ (completed 2026-04-07)

#### 3.3 Examples Directory
- `examples/basic-proxy/README.md` — minimal setup (5 lines to start)
- `examples/docker-compose/` — docker-compose.yml + README
- `examples/python-client/` — using bastion-client library
- `examples/priority-tiers/` — multi-client priority demo

#### 3.4 Client Library Polish (bastion-client v0.2.0)
- `clients/bastion-client/` — add `chat()`, `embed()` methods
- Add sync wrapper class (currently async-only)
- Add retry logic with exponential backoff
- Publish to PyPI as `bastion-client`

#### Phase 3 Exit Criteria
- [x] `docker run` with `--gpus all` works
- [x] `persistence.enabled: true` → state survives restart
- [ ] Examples directory has 3+ working examples
- [ ] Client library on PyPI

---

### Phase 4: Community Polish (v0.6.0)

**Goal**: Professional open-source project. Easy to contribute to.

#### 4.1 GitHub Repo Polish
- `.github/ISSUE_TEMPLATE/bug_report.yml` — structured form (GPU model, OS, Ollama version)
- `.github/ISSUE_TEMPLATE/feature_request.yml`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `SECURITY.md`
- Clean up root directory (move `to_del_measure_vram.sh` to `_archive/`)

#### 4.2 Pre-commit Hooks
- `.pre-commit-config.yaml` — ruff lint+format, mypy, trailing whitespace, YAML lint
- Document in CONTRIBUTING.md

#### 4.3 Documentation Site
- mkdocs-material on GitHub Pages
- Pages: Quick Start, Configuration, API Reference, Architecture, Platform Support, Troubleshooting
- Auto-generated API reference from docstrings

#### 4.4 Release Automation
- `.github/workflows/release.yml` — triggered by git tags
- Builds wheel → PyPI (trusted publishing)
- Builds Docker image → ghcr.io
- Auto-generates GitHub Release with changelog

#### 4.5 Platform Support Docs + Test Fixes
- `docs/platform-support.md` — what works where, fan control requirements
- Fix `tests/test_dashboard.py` and `tests/test_observability_phase1.py` import errors
- Add `pytest-cov` + `mypy` to dev deps and CI (deferred from Phase 2)

#### Phase 4 Exit Criteria
- [ ] New contributor: fork → install → test → PR in < 15 minutes
- [ ] Docs site live on GitHub Pages
- [ ] Releases automated via git tags
- [ ] All tests pass (no import errors)

---

### Phase 5: Toward 1.0 (v1.0.0)

**Goal**: Stability guarantees and public API freeze.

#### 5.1 API Stability Audit
- **Stable**: proxy behavior, `/broker/status`, `/broker/livez`, `/broker/readyz`, CLI flags, YAML schema
- **Beta**: A2A endpoints, client library
- **Internal**: dashboard, scheduler implementation
- Document in `docs/api-stability.md`

#### 5.2 Security Review
- Audit subprocess calls for injection risks
- Review auth token handling (already SHA-256 hashed)
- Add CORS configuration
- Rate limiting on proxy routes (currently admin-only)

#### 5.3 Performance Benchmarks
- `benchmarks/` directory with latency/throughput scripts
- Measure proxy overhead (target: < 5ms added latency)
- CI job to catch regressions

#### 5.4 1.0 Release Criteria
- All Phase 1-4 complete
- No known crash bugs
- API stability documented
- 3+ months of v0.x without breaking changes
- External users have validated it

## Explicitly deferred (do not implement)

- Multi-GPU support (aspirational, stays in ROADMAP.md)
- Non-Ollama backends (vLLM, llama.cpp)
- AMD ROCm / Intel Arc GPU backends (seam exists in `bastion.gpu`)
- macOS / Windows support (best-effort after Linux is solid)
- MCP integration (future track)
- Clustering / distributed broker (single-machine scope for 1.0)
