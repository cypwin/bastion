# CLAUDE.md — BASTION

> Auto-loaded every session. Keep concise.

## Project Identity

**BASTION** — Batch Affinity Scheduler for Throttled Inference on Ollama Networks.
System-wide GPU/LLM broker that prevents crashes from concurrent Ollama access.
Sits as a transparent HTTP proxy on port 11434, forwarding to Ollama on port 11435.

Three layers:
1. **Ollama Proxy** — transparent reverse proxy, injects `use_mmap: false`, streams NDJSON
2. **Admin API** (`/broker/*`) — status, queue view, preload/unload, health
3. **A2A Agent Interface** (`/a2a/*`) — Agent Card, task lifecycle, batch inference, model leases

## Quick Start

```bash
# In user terminal (interactive shell):
conda activate bastion
python -m bastion
python -m bastion --config config/broker.yaml

# In Claude CLI Bash tool (non-interactive — conda activate FAILS):
/home/user/miniforge3/envs/bastion/bin/python -m bastion
/home/user/miniforge3/envs/bastion/bin/python -m bastion --config config/broker.yaml
```

## CLI Sandbox Constraints (MANDATORY)

Claude Code's Bash tool runs in a **non-interactive shell**. `conda activate` fails silently.

**Canonical Python invocation for ALL Bash commands:**
```bash
/home/user/miniforge3/envs/bastion/bin/python <script_or_module>
```

**PYTHONPATH** (if needed for imports):
```bash
PYTHONPATH=/home/user/BASTION/src /home/user/miniforge3/envs/bastion/bin/python -m bastion
```

## Project Structure

```
src/bastion/
├── __init__.py       # Package init
├── __main__.py       # CLI entry point (argparse + uvicorn)
├── a2a.py            # A2A protocol handler (task lifecycle, skill routing, leases)
├── audit.py          # Tiered audit logging (JSONL, content hashing, rotation)
├── auth.py           # API key + bearer token auth dependencies
├── circuitbreaker.py # Three-state circuit breaker (closed/open/half-open)
├── config.py         # YAML config loader (search paths)
├── dashboard.py      # Textual TUI dashboard (17 panels, GPU/queue/A2A views)
├── discovery.py      # Model discovery helper (nvidia/ollama)
├── gpu/              # GPU backend subpackage (nvidia/amd dispatch)
├── gpu_profiles.py   # Known-safe profiles for 13 NVIDIA GPUs
├── health.py         # nvidia-smi queries (5s timeout, graceful fallback)
├── metrics.py        # Prometheus metrics (no-op stubs when client missing)
├── middleware.py      # FastAPI middleware (request metrics recording)
├── models.py         # All Pydantic models (config, queue, GPU state, A2A)
├── paths.py          # XDG-aware data/config directory resolver
├── persistence.py    # SQLite persistence adapter (optional extra)
├── proxy.py          # Transparent proxy (streaming NDJSON, use_mmap injection)
├── queue.py          # AffinityQueue (per-model sub-queues, priority aging)
├── ratelimit.py      # Per-IP rate limiting middleware
├── scheduler.py      # Scheduling loop (model swaps, cooldown enforcement)
├── server.py         # FastAPI app factory + admin routes + proxy catch-all
├── stress.py         # --stress-test GPU calibrator
├── taskstore.py      # Dual-store A2A task store (compaction, TTL, backpressure)
├── telemetry.py      # OpenTelemetry tracing (no-op stubs when SDK missing)
├── thrashing.py      # Thrashing detector (consecutive short runs)
├── validate.py       # --validate pre-flight check runner
├── vram.py           # VRAM tracker (Ollama /api/ps + nvidia-smi fusion)
└── watchdog.py       # Ollama process monitor + systemd sd_notify integration

config/broker.yaml    # Default config (models, GPU thresholds, scheduler params)
systemd/              # Service files (bastion.service, ollama port override)
tests/                # Pytest test suite
reference/            # Crash investigation docs (read-only)
```

## Critical Rules

- **Never delete files** — archive to `_archive/` if replacing
- **Never run tests automatically** — print the command for the user:
  ```
  /home/user/miniforge3/envs/bastion/bin/python -m pytest tests/ -v
  ```
- **Type hints required** — all functions get type annotations
- **`from __future__ import annotations`** in every `.py` file
- **Import order**: stdlib → third-party → local (`bastion.*`)
- **Git commits**: `feat(S1): description` format (session-tagged)

## Key Technical Context

- **RTX 5090 crash prevention**: `use_mmap: false` injected into ALL requests (env var doesn't exist)
- **VRAM budget**: 24 GB max (32 GB total − 8 GB headroom)
- **Cooldown**: 2s mandatory between model transitions
- **Priority aging**: `effective = base + (age_seconds × 2.0)` — prevents starvation
- **Streaming critical**: NDJSON passthrough without buffering for `ollama run`
- **In-memory state**: queues, task store, leases — no external DB (no SQLite)

## Dependencies

Framework: asyncio + httpx (async) + FastAPI/uvicorn + Pydantic v2 + PyYAML.
Install: `pip install -e ".[dev]"` from project root.
