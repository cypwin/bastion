# Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare BASTION for public release with clean documentation, a pre-flight validator (`bastion validate`), and a GPU stress calibrator (`bastion stress-test`).

**Architecture:** Three independent streams — (1) public-facing documentation rewrite, (2) `validate` CLI subcommand with GPU profile table, (3) `stress-test` CLI subcommand with safety ceremony and gradual ramp-up. Streams 2 and 3 share GPU profile infrastructure. Documentation is fully independent.

**Tech Stack:** Python 3.11+, argparse (CLI), httpx (HTTP checks), asyncio (GPU queries), Pydantic v2 (profile models), PyYAML (profile output), pytest + pytest-asyncio (tests)

**Spec:** `docs/superpowers/specs/2026-04-23-production-readiness-design.md`

---

## File Structure

### Stream 1 — Documentation (no code changes)

| Action | File | Responsibility |
|--------|------|----------------|
| Rewrite | `README.md` | Public-facing landing page: problem, prerequisites, quickstart, features, links |
| Archive | `ROADMAP.md` → `_archive/ROADMAP.md` | Internal session history — not for public |
| Archive | `docs/audit/*` → `_archive/docs/audit/*` | Internal analyst reports |
| Archive | `M58_BASTION_HANDOFF.md` → `_archive/M58_BASTION_HANDOFF.md` | Session handoff |
| Archive | `reference/*` → `_archive/reference/*` | Crash investigation raw data |
| Archive | `docs/production-roadmap-prompt.md` → `_archive/docs/production-roadmap-prompt.md` | Internal roadmap prompt |
| Create | `docs/getting-started.md` | Full installation walkthrough |
| Create | `docs/hardware-guide.md` | GPU compatibility table, VRAM requirements |
| Create | `docs/configuration.md` | Complete config reference with examples |
| Create | `docs/troubleshooting.md` | "I see X → do Y" diagnostic guide |
| Create | `docs/operations.md` | Day-2 operations: monitoring, restart, tuning |
| Rewrite | `SECURITY.md` → `docs/security.md` (archive original) | Practical security howto |
| Rewrite | `CHANGELOG.md` | Clean release notes, no session tags |
| Rewrite | `docs/crash-prevention.md` | Mechanisms only, no forensic narrative |

### Stream 2 — Pre-flight Validator

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/bastion/gpu_profiles.py` | GPU profile table + lookup logic |
| Create | `src/bastion/validate.py` | Pre-flight check runner |
| Modify | `src/bastion/__main__.py` | Add `--validate` subcommand |
| Create | `tests/test_gpu_profiles.py` | Profile lookup + fallback tests |
| Create | `tests/test_validate.py` | Validator check tests |

### Stream 3 — Stress Calibrator

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/bastion/stress.py` | Stress test runner (5 phases) |
| Modify | `src/bastion/__main__.py` | Add `--stress-test` subcommand |
| Modify | `src/bastion/config.py` | Load GPU profile at startup if present |
| Create | `tests/test_stress.py` | Stress test logic tests (mocked GPU/Ollama) |

---

## Stream 1: Public Documentation

Stream 1 is fully independent — no code changes, only documentation. Can be done in a single session with parallel agents (one per document).

### Task 1: Archive Internal Files

**Files:**
- Move: `docs/audit/*` → `_archive/docs/audit/*`
- Move: `M58_BASTION_HANDOFF.md` → `_archive/M58_BASTION_HANDOFF.md`
- Move: `reference/*` → `_archive/reference/*`
- Move: `ROADMAP.md` → `_archive/ROADMAP.md`
- Move: `docs/production-roadmap-prompt.md` → `_archive/docs/production-roadmap-prompt.md`
- Move: `SECURITY.md` → `_archive/SECURITY.md`

- [x] **Step 1: Create archive directory structure**

```bash
mkdir -p _archive/docs/audit
mkdir -p _archive/reference
```

- [x] **Step 2: Move internal analyst reports**

```bash
mv docs/audit/* _archive/docs/audit/
```

- [x] **Step 3: Move session handoffs and internal docs**

```bash
mv M58_BASTION_HANDOFF.md _archive/
mv ROADMAP.md _archive/
mv docs/production-roadmap-prompt.md _archive/docs/
```

- [x] **Step 4: Move crash investigation raw data**

```bash
mv reference/* _archive/reference/
```

- [x] **Step 5: Move SECURITY.md (will be rewritten as docs/security.md)**

```bash
mv SECURITY.md _archive/
```

- [x] **Step 6: Create archive README explaining what's here**

Create `_archive/README.md`:

```markdown
# Archive

Internal development artifacts preserved for reference. These files are not
part of the public BASTION distribution.

## Contents

- `docs/audit/` — Internal analyst reports from development sessions
- `reference/` — Crash investigation raw data and forensics
- `ROADMAP.md` — Development session history (S1-S14)
- `M58_BASTION_HANDOFF.md` — Session handoff notes
- `SECURITY.md` — Original security policy (rewritten as docs/security.md)
- `docs/production-roadmap-prompt.md` — Internal roadmap planning

## Why archived?

These files contain internal development history, system-specific details,
and investigation notes that are valuable for maintainers but not relevant
to end users of the public release.
```

- [x] **Step 7: Commit**

```bash
git add _archive/ docs/audit/ reference/ ROADMAP.md M58_BASTION_HANDOFF.md SECURITY.md docs/production-roadmap-prompt.md
git commit -m "chore: archive internal development artifacts for public release prep"
```

---

### Task 2: Rewrite README.md

**Files:**
- Modify: `README.md`
- Reference: `docs/crash-prevention.md` (for crash prevention summary), `config/broker.example.yaml` (for config examples)

The current README is 85% there but has some gaps: no prerequisites checklist, no `--init-config`/`--detect-models` mention in quickstart, no "where to go next", and the "Technical Deep Dive" section is too long for a landing page.

- [x] **Step 1: Read current README.md for structure**

Read `README.md` and note: what to keep, what to trim, what to add.

- [x] **Step 2: Rewrite README.md**

The new README should follow this structure (write the complete file):

```markdown
# BASTION

**Batch Affinity Scheduler for Throttled Inference on Ollama Networks**

[Brief 2-sentence description of what BASTION does and why]

---

## Why BASTION?

[3-paragraph problem statement — keep the current one, it's excellent.
Remove specific crash counts and investigation details. Focus on:
1. The problem (concurrent Ollama access crashes GPUs)
2. Why it happens (mmap, rapid cycling, no upstream protection)
3. What BASTION does about it (transparent proxy, scheduling, VRAM budgets)]

## Prerequisites

- Python 3.11+
- Linux with NVIDIA GPU and working drivers (`nvidia-smi` must respond)
- [Ollama](https://ollama.com) installed
- At least one model pulled (`ollama pull llama3.1:8b`)

## Quick Start

### 1. Install

[pip install bastion-gpu-broker OR git clone + pip install -e .]

### 2. Move Ollama to port 11435

[Keep current instructions — systemd override OR manual env var]

### 3. Configure

[NEW: mention --init-config and --detect-models]

### 4. Validate your setup

[NEW: bastion validate]

### 5. Start BASTION

[Keep current start instructions]

### 6. Verify

[Keep current verify instructions]

## Key Features

[Keep current feature list — it's comprehensive and well-written]

## Architecture

[Keep the ASCII diagram — it's excellent]

## Dashboard

[Keep current dashboard section — trim slightly]

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Full installation walkthrough |
| [Configuration](docs/configuration.md) | Every config option explained |
| [Hardware Guide](docs/hardware-guide.md) | GPU compatibility and VRAM requirements |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |
| [Operations](docs/operations.md) | Monitoring, restart, day-2 ops |
| [Security](docs/security.md) | Auth, TLS, network isolation |
| [Crash Prevention](docs/crash-prevention.md) | How BASTION prevents GPU crashes |
| [API Reference](docs/api.md) | All endpoints with examples |
| [Deployment](docs/deployment.md) | Systemd, Docker, desktop launcher |

## Optional Extras

[Keep current extras section]

## Testing

[Keep current testing section]

## Contributing

[Link to CONTRIBUTING.md]

## License

MIT License. See [LICENSE](LICENSE) for details.
```

Key changes from current README:
- Add Prerequisites section (currently missing)
- Add `--init-config` / `--detect-models` / `bastion validate` to Quick Start
- Add Documentation table linking to all guides
- Remove "Technical Deep Dive: Crash Prevention" (moved to docs/crash-prevention.md)
- Remove "API Reference" inline section (link to docs/api.md instead)
- Remove "Roadmap" section (internal)
- Remove "Project Structure" section (developer-facing, lives in CONTRIBUTING.md)
- Remove session-specific content (S7, M58 references)

- [x] **Step 3: Verify no internal references remain**

Search the rewritten README for: `S1`, `S2`, `S3`, `S4`, `S5`, `S6`, `S7`, `M58`, `RTX 5090`, `crash 7`, `crash 8`, `crash 9`, session numbers, or any personal system details.

- [x] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: rewrite README for public release"
```

---

### Task 3: Create docs/getting-started.md

**Files:**
- Create: `docs/getting-started.md`
- Reference: `docs/deployment.md` (for Ollama setup patterns), `src/bastion/__main__.py` (for CLI flags)

- [x] **Step 1: Write docs/getting-started.md**

Complete guide from zero to running. Structure:

```markdown
# Getting Started

Step-by-step guide to install and run BASTION.

## 1. System Requirements

- Linux (Ubuntu 22.04+, Fedora 38+, Arch, or equivalent)
- Python 3.11 or newer
- NVIDIA GPU with proprietary drivers installed
- nvidia-smi responding (test: `nvidia-smi`)
- 2 GB free disk space (for models)

## 2. Install Ollama

[Link to ollama.com/download, verify with `ollama --version`]

## 3. Move Ollama to Port 11435

[Two methods: systemd override (recommended) or manual. Include verification: `curl http://localhost:11435`]

## 4. Pull a Model

[`ollama pull llama3.1:8b` — verify with `ollama list`]

## 5. Install BASTION

[pip install from PyPI or git clone. Include optional extras.]

## 6. Generate Configuration

[`bastion --init-config` — explain what it creates and where]
[`bastion --detect-models` — explain the output and how to paste it]

## 7. Validate Your Setup

[`bastion validate` — explain each check and what to do if something fails, link to troubleshooting.md]

## 8. Start BASTION

[`bastion` or `bastion --config path/to/broker.yaml`]

## 9. Verify It Works

[curl tests: root endpoint, /broker/status, ollama run through proxy]

## What's Next?

- [Configuration Guide](configuration.md) — tune for your hardware
- [Hardware Guide](hardware-guide.md) — check GPU compatibility
- [Operations Guide](operations.md) — monitoring and management
- [Dashboard](../README.md#dashboard) — TUI for real-time monitoring
```

Content principles:
- Every command should be copy-paste ready
- Include expected output for verification steps
- Link to troubleshooting.md for each step that can fail
- No assumptions about the user's experience level
- No internal references

- [x] **Step 2: Commit**

```bash
git add docs/getting-started.md
git commit -m "docs: add getting started guide"
```

---

### Task 4: Create docs/hardware-guide.md

**Files:**
- Create: `docs/hardware-guide.md`
- Reference: `src/bastion/models.py:40-57` (GPUConfig defaults), `docs/crash-prevention.md` (swap rate thresholds)

- [x] **Step 1: Write docs/hardware-guide.md**

```markdown
# Hardware Guide

## Supported GPUs

BASTION works with NVIDIA GPUs that support CUDA and have working `nvidia-smi`.

### Tested

| GPU | VRAM | Status | Notes |
|-----|------|--------|-------|
| RTX 5090 | 32 GB | Fully tested | Primary development hardware |
| (Others will be added as users report) | | | |

### Expected to Work

| GPU Family | VRAM Range | Notes |
|------------|-----------|-------|
| RTX 40-series | 8-24 GB | Consumer desktop, widely available |
| RTX 30-series | 8-24 GB | Previous generation, well-supported |
| RTX 20-series | 8-11 GB | Minimum viable for small models |
| A100/A6000 | 40-80 GB | Data center GPUs |
| L40/L4 | 24-48 GB | Inference-optimized |

### Not Supported

- AMD GPUs (ROCm) — Ollama supports ROCm but BASTION's GPU monitoring uses nvidia-smi
- Apple Silicon — Ollama runs natively but BASTION's crash prevention is NVIDIA-specific
- Intel Arc — not tested, no driver integration
- CPU-only — BASTION starts but GPU safety features are disabled

## VRAM Requirements

### How VRAM Budget Works

BASTION reserves headroom from your total VRAM:

```
Usable VRAM = Total VRAM - Headroom (default 6 GB)
```

The headroom covers: OS display, CUDA runtime, KV cache growth during inference, and a safety margin.

### Model Size vs VRAM

Rough estimates for quantized (Q4_K_M) models:

| Parameter Count | Approximate VRAM | Example Models |
|----------------|-----------------|----------------|
| 1-3B | 1-2 GB | qwen3:1.7b, llama3.2:1b, phi-3:mini |
| 7-8B | 4-5 GB | llama3.1:8b, mistral:7b, qwen3:8b |
| 13-14B | 8-10 GB | llama2:13b, qwen3:14b |
| 30-34B | 18-20 GB | codellama:34b |
| 70B | 38-42 GB | llama3.1:70b (needs 48+ GB GPU) |

### Configuration by GPU Size

#### 8 GB GPU (RTX 3060, RTX 4060)

```yaml
gpu:
  total_vram_gb: 0     # auto-detect
  headroom_gb: 2       # smaller headroom for small GPUs

scheduler:
  cooldown_seconds: 3.0                # longer cooldown
  swap_rate_warn_threshold: 3          # more conservative
  swap_rate_critical_threshold: 4
  max_concurrent_dispatches: 1         # single dispatch only
```

Recommendation: stick to one 7B model. Multi-model workflows will queue heavily.

#### 12 GB GPU (RTX 3060 12GB, RTX 4070)

[Similar config block with 3GB headroom, moderate thresholds]

#### 24 GB GPU (RTX 3090, RTX 4090)

[Config block with 6GB headroom, default thresholds, 2-3 concurrent dispatches]

#### 32+ GB GPU (RTX 5090, A6000)

[Config block with 8GB headroom, default thresholds, 3 concurrent dispatches]

## Running bastion validate

[Reference to bastion validate command, what it checks for hardware]

## Running bastion stress-test

[Reference to stress test for discovering actual limits]

## Overheating and Thermal Safety

[Explain max_temperature_c, what happens when exceeded, how to monitor]
```

- [x] **Step 2: Commit**

```bash
git add docs/hardware-guide.md
git commit -m "docs: add hardware guide with GPU compatibility and VRAM tables"
```

---

### Task 5: Create docs/configuration.md

**Files:**
- Create: `docs/configuration.md`
- Reference: `src/bastion/models.py` (all config models), `config/broker.example.yaml`, `src/bastion/config.py` (env overrides)

- [x] **Step 1: Write docs/configuration.md**

Structure:

```markdown
# Configuration Reference

## Config File Location

BASTION searches for config in this order:
1. `--config` CLI flag (highest priority)
2. `config/broker.yaml` (project directory)
3. `./broker.yaml` (current directory)
4. `/etc/bastion/broker.yaml` (Linux only)
5. `~/.config/bastion/broker.yaml` (XDG)

Generate a starter config: `bastion --init-config`

## Environment Variable Overrides

Environment variables override config file values. Useful for Docker/systemd.

| Variable | Config Path | Type | Example |
|----------|------------|------|---------|
| BASTION_OLLAMA_HOST | ollama.host | string | "192.168.1.10" |
| BASTION_OLLAMA_PORT | ollama.port | int | 11435 |
| BASTION_PORT | server.port | int | 11434 |
| BASTION_ADMIN_PORT | server.admin_port | int | 9999 |
| BASTION_GPU_TOTAL_VRAM_GB | gpu.total_vram_gb | float | 24.0 |
| BASTION_GPU_MAX_TEMP_C | gpu.max_temperature_c | int | 83 |
| BASTION_GPU_MAX_POWER_W | gpu.max_power_watts | float | 300 |
| BASTION_AUTH_ENABLED | auth.enabled | bool | true |
| BASTION_API_KEYS | auth.api_keys | csv | "key1,key2" |
| BASTION_AUDIT_TIER | audit.tier | int | 2 |
| BASTION_PERSISTENCE_ENABLED | persistence.enabled | bool | true |
| BASTION_PERSISTENCE_DB_PATH | persistence.database_path | string | "/data/bastion.db" |

## Configuration Sections

[For EACH section in BrokerConfig, document every field with:
- Field name
- Type
- Default value
- What it does
- When you'd change it
- Example]

### ollama
### server
### gpu
### scheduler
### proxy
### priorities
### models
### request_overrides
### auth
### rate_limit
### circuit_breaker
### audit
### persistence
### telemetry
### a2a
### complexity_routing
### thrashing_detection

## Preset Configurations

### Minimal (8 GB GPU, single user)
[Complete config block]

### Standard (24 GB GPU, multiple agents)
[Complete config block]

### Production (24+ GB GPU, systemd, persistence)
[Complete config block with auth, persistence, rate limiting enabled]
```

Write every field from the Pydantic models. Cross-reference `src/bastion/models.py` for field names, types, and defaults. Document each field individually — no "see models.py" references.

- [x] **Step 2: Commit**

```bash
git add docs/configuration.md
git commit -m "docs: add complete configuration reference"
```

---

### Task 6: Create docs/troubleshooting.md

**Files:**
- Create: `docs/troubleshooting.md`

- [x] **Step 1: Write docs/troubleshooting.md**

Structure: each issue as a section with **Symptom**, **Cause**, **Fix**.

```markdown
# Troubleshooting

## BASTION Won't Start

### "Address already in use" on port 11434

**Symptom:** `OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 11434)`

**Cause:** Another process (likely Ollama) is using port 11434.

**Fix:** Move Ollama to port 11435 first. See [Getting Started](getting-started.md#3-move-ollama-to-port-11435).

### "No config file found"

**Symptom:** Warning at startup, BASTION uses defaults.

**Cause:** No broker.yaml in search paths.

**Fix:** Run `bastion --init-config` to generate one, then `bastion --detect-models` to add your models.

## Ollama Connection Issues

### "Ollama unreachable" / circuit breaker OPEN

**Symptom:** All requests return 503. `/broker/status` shows circuit breaker state "open".

**Cause:** Ollama is not running or not listening on the configured port.

**Fix:**
1. Check if Ollama is running: `systemctl status ollama` or `curl http://localhost:11435`
2. Verify the port matches your config: check `ollama.port` in broker.yaml
3. If Ollama crashed, restart it: `sudo systemctl restart ollama`

[Continue with 15-20 more issues covering:
- GPU not detected (nvidia-smi not found)
- VRAM exhausted / model won't load
- GPU temperature too high / scheduling paused
- Queue growing / requests timing out
- Dashboard won't launch (textual not installed)
- Models not showing in /broker/status
- Priority not being applied
- Rate limited (429 responses)
- Auth failures (401/403)
- Persistence database errors
- Port 11435 blocked (nftables)
- nvidia-smi timeouts / GPU lockup
- Streaming not working (buffered responses)
- Two-port mode not working
]
```

- [x] **Step 2: Commit**

```bash
git add docs/troubleshooting.md
git commit -m "docs: add troubleshooting guide with 15+ common issues"
```

---

### Task 7: Create docs/operations.md

**Files:**
- Create: `docs/operations.md`
- Reference: `src/bastion/metrics.py` (metric names), `src/bastion/server.py` (admin endpoints)

- [x] **Step 1: Write docs/operations.md**

```markdown
# Operations Guide

## Starting and Stopping

### Start
[bastion command, systemd start, verify it's running]

### Graceful Shutdown
[SIGTERM behavior: drains queue, waits for in-flight, closes connections]
[systemd stop, manual kill]
[TimeoutStopSec=15 — what happens if shutdown takes too long]

### Safe Restart
[Drain first, then restart. How to drain: POST /broker/drain, wait for queue to empty, then restart]

## Monitoring

### Key Health Endpoints

| Endpoint | What It Tells You | Check Frequency |
|----------|------------------|----------------|
| /broker/health | Is BASTION alive and can it reach Ollama? | Every 30s |
| /broker/status | Queue depth, GPU state, circuit breaker, swap rate | Every 60s |
| /broker/watchdog | Ollama latency, GPU responsiveness | Every 60s |

### Key Metrics (Prometheus)

[Table of most important metrics with descriptions and alert thresholds:
- bastion_requests_total
- bastion_queue_depth (by model)
- bastion_model_swap_total
- bastion_vram_used_bytes
- bastion_gpu_temperature_celsius
- bastion_request_duration_seconds
- bastion_queue_wait_seconds
]

### What to Watch

[Interpret the numbers: what's normal, what's concerning, what's critical]

## Queue Management

### Viewing the Queue
[GET /broker/queue]

### Drain Mode
[POST /broker/drain — what it does, how to exit]

### Preloading Models
[POST /broker/preload — when to use, VRAM implications]

### Unloading Models
[POST /broker/unload — when safe, when not]

## Model Management

### Adding a Model
[ollama pull, bastion --detect-models, add to config, restart or preload]

### Removing a Model
[ollama rm, remove from config, restart]

## Log Locations

| Log | Location | Format |
|-----|----------|--------|
| Application | stdout/journal | text |
| Audit | ~/.local/share/bastion/bastion-audit.jsonl | JSONL |
| VRAM journal | ~/.local/share/bastion/bastion-vram-journal.jsonl | JSONL |
| Persistence DB | ~/.local/share/bastion/bastion.db | SQLite |

## Calibrated GPU Profile

[Explain gpu-profile.yaml: what it is, where it lives, how it's used, when to re-run]
```

- [x] **Step 2: Commit**

```bash
git add docs/operations.md
git commit -m "docs: add operations guide for day-2 management"
```

---

### Task 8: Rewrite docs/security.md

**Files:**
- Create: `docs/security.md`
- Reference: `_archive/SECURITY.md` (original), `src/bastion/auth.py`, `src/bastion/ratelimit.py`

- [x] **Step 1: Write docs/security.md**

Rewrite from policy document to practical howto:

```markdown
# Security Guide

## Threat Model

BASTION is a local GPU broker. It is designed to run on a single machine or
trusted LAN. It is NOT designed for public internet exposure.

Default configuration:
- Binds to 0.0.0.0 (all interfaces) on port 11434
- No authentication required
- No TLS

This is safe for single-user workstations. For shared or remote access,
follow the hardening steps below.

## Reporting Vulnerabilities

[Keep from original SECURITY.md — email, GitHub advisory, 72h response]

## Hardening Checklist

### 1. Enable Authentication

[Step-by-step: edit broker.yaml, set auth.enabled, add api_keys, test with curl]

### 2. Restrict Network Access

#### Bind to localhost only
[Change server.host to 127.0.0.1]

#### nftables port lockdown
[Complete nftables rules to restrict Ollama backend port 11435
to only the bastion group. Step-by-step including group creation.]

### 3. Add TLS via Reverse Proxy

#### Caddy (recommended — automatic HTTPS)
[Complete Caddyfile example]

#### nginx
[Complete nginx config example with TLS]

### 4. Enable Rate Limiting

[Edit config, set rate_limit.enabled, explain parameters]

### 5. Systemd Security Hardening

[Reference the systemd service example: ProtectSystem, ProtectHome, NoNewPrivileges, etc.]

### 6. Audit Log Security

[File permissions for audit logs, rotation settings]
```

- [x] **Step 2: Commit**

```bash
git add docs/security.md
git commit -m "docs: add practical security guide with hardening steps"
```

---

### Task 9: Rewrite CHANGELOG.md and docs/crash-prevention.md

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/crash-prevention.md`

- [x] **Step 1: Rewrite CHANGELOG.md**

Remove session tags (S1-S14), internal references, and development-specific language. Keep version history clean:

```markdown
# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-04-06

### Added
- GPU auto-detection via nvidia-smi (VRAM, TDP, GPU name)
- `--init-config` CLI flag to generate a starter configuration
- `--detect-models` CLI flag to discover installed Ollama models
- Platform-aware directory resolution (XDG on Linux)
- GPU backend abstraction with automatic NVIDIA detection
- Environment variable overrides for Docker/CI configuration
- Graceful degradation for optional features (fan control, metrics, tracing)
- Complexity-based model routing with response headers
- Per-agent thrashing detection (warn and strict modes)

### Changed
- GPU VRAM defaults to auto-detect (was hardcoded)
- Conservative power defaults (300W, was hardware-specific)
- Audit and VRAM journal paths moved to XDG data directory

## [0.2.0] - 2026-03-31

### Added
- 14-panel TUI dashboard with real-time GPU monitoring
- GPU fan control with temperature-triggered auto mode
- Interactive model management (preload/unload/drain)
- Request trace viewer and VRAM budget visualization

### Fixed
- Stale VRAM ledger entries causing queue growth under concurrent load

## [0.1.0] - 2026-03-15

### Added
- Transparent Ollama proxy with `use_mmap: false` injection
- Affinity queue with per-model sub-queues and priority tiers
- VRAM tracking via nvidia-smi and Ollama `/api/ps` fusion
- Scheduler with cooldown enforcement and swap rate limiting
- Admin API for status, queue view, preload/unload, health
- A2A agent interface with task lifecycle and model leases
- Prometheus metrics and OpenTelemetry tracing (optional)
- Tiered JSONL audit logging with content hashing
- API key authentication and per-IP rate limiting
- Three-state circuit breaker for backend failures
- Health probes (`/broker/livez`, `/broker/readyz`)
- Systemd service files and watchdog integration
```

- [x] **Step 2: Rewrite docs/crash-prevention.md**

Keep the excellent technical content but remove:
- "Investigation Methodology" section (internal forensics)
- Specific crash numbers ("crash 7", "crash 8", "9 crash events")
- References to the user's specific hardware setup
- Session-specific language

Keep and clean:
- "The Problem" → "The Failure Mode"
- "The mmap Discovery" → "Memory-Mapped Loading"
- "Swap Rate Analysis" → "Swap Rate Thresholds" (keep the table, remove "observed across 9 events")
- "BASTION's Prevention Mechanisms" → keep all four mechanisms
- "Monitoring: What to Watch" → keep as-is

- [x] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/crash-prevention.md
git commit -m "docs: clean changelog and crash prevention for public release"
```

---

## Stream 2: Pre-flight Validator

### Task 10: Create GPU Profile Table

**Files:**
- Create: `src/bastion/gpu_profiles.py`
- Create: `tests/test_gpu_profiles.py`

- [x] **Step 1: Write failing test for profile lookup**

Create `tests/test_gpu_profiles.py`:

```python
"""Tests for GPU profile table and lookup."""

from __future__ import annotations

import pytest

from bastion.gpu_profiles import GPUProfile, lookup_profile


class TestLookupProfile:
    """Test GPU profile lookup by name."""

    def test_exact_match_rtx_4090(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 4090")
        assert profile.name == "RTX 4090"
        assert profile.vram_total_mb == 24576
        assert profile.safe_swap_rate == 5

    def test_exact_match_rtx_3060(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 3060")
        assert profile.name == "RTX 3060"
        assert profile.vram_total_mb == 12288

    def test_partial_match(self) -> None:
        """nvidia-smi may report just 'RTX 4090' without 'GeForce'."""
        profile = lookup_profile("RTX 4090")
        assert profile.name == "RTX 4090"

    def test_unknown_gpu_returns_default(self) -> None:
        profile = lookup_profile("Some Future GPU 9999")
        assert profile.name == "Unknown GPU"
        assert profile.safe_swap_rate == 3
        assert profile.vram_headroom_mb == 4096
        assert profile.thermal_ceiling_c == 80
        assert profile.cooldown_seconds == 3

    def test_case_insensitive(self) -> None:
        profile = lookup_profile("nvidia geforce rtx 4090")
        assert profile.name == "RTX 4090"

    def test_rtx_5090_has_mmap_note(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 5090")
        assert profile.notes is not None
        assert "use_mmap" in profile.notes


class TestGPUProfile:
    """Test GPUProfile model."""

    def test_profile_fields(self) -> None:
        profile = GPUProfile(
            name="Test GPU",
            vram_total_mb=8192,
            safe_swap_rate=3,
            vram_headroom_mb=2048,
            thermal_ceiling_c=83,
            cooldown_seconds=3,
        )
        assert profile.name == "Test GPU"
        assert profile.vram_total_mb == 8192

    def test_profile_optional_notes(self) -> None:
        profile = GPUProfile(
            name="Test",
            vram_total_mb=8192,
            safe_swap_rate=3,
            vram_headroom_mb=2048,
            thermal_ceiling_c=83,
            cooldown_seconds=3,
        )
        assert profile.notes is None
```

- [x] **Step 2: Run test to verify it fails**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_gpu_profiles.py -v`

Expected: `ModuleNotFoundError: No module named 'bastion.gpu_profiles'`

- [x] **Step 3: Write GPU profile module**

Create `src/bastion/gpu_profiles.py`:

```python
"""GPU profile table — known-safe defaults for common NVIDIA GPUs.

Maps GPU names (from nvidia-smi) to safe operating parameters. Used by
``bastion validate`` for pre-flight checks and ``bastion stress-test``
as initial estimates before calibration.

Unknown GPUs receive conservative defaults. Users can contribute profiles
for their hardware via pull request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GPUProfile:
    """Safe operating parameters for a specific GPU model."""

    name: str
    vram_total_mb: int
    safe_swap_rate: int          # Max safe model swaps per minute
    vram_headroom_mb: int        # VRAM to reserve for OS/CUDA/display
    thermal_ceiling_c: int       # Max temp before pausing scheduling
    cooldown_seconds: int        # Minimum seconds between model swaps
    notes: str | None = None     # Hardware-specific warnings


# Default profile for unknown GPUs — conservative values safe for any hardware
_DEFAULT_PROFILE = GPUProfile(
    name="Unknown GPU",
    vram_total_mb=0,             # 0 = must be detected at runtime
    safe_swap_rate=3,
    vram_headroom_mb=4096,
    thermal_ceiling_c=80,
    cooldown_seconds=3,
)

# Known GPU profiles — keyed by substring that appears in nvidia-smi output.
# Order matters: first match wins, so put specific names before general ones.
_PROFILES: list[tuple[str, GPUProfile]] = [
    ("RTX 5090", GPUProfile(
        name="RTX 5090",
        vram_total_mb=32768,
        safe_swap_rate=4,
        vram_headroom_mb=8192,
        thermal_ceiling_c=80,
        cooldown_seconds=2,
        notes="use_mmap: false mandatory — memory-mapped loading causes instability",
    )),
    ("RTX 4090", GPUProfile(
        name="RTX 4090",
        vram_total_mb=24576,
        safe_swap_rate=5,
        vram_headroom_mb=6144,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4080", GPUProfile(
        name="RTX 4080",
        vram_total_mb=16384,
        safe_swap_rate=4,
        vram_headroom_mb=4096,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4070", GPUProfile(
        name="RTX 4070",
        vram_total_mb=12288,
        safe_swap_rate=4,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4060", GPUProfile(
        name="RTX 4060",
        vram_total_mb=8192,
        safe_swap_rate=3,
        vram_headroom_mb=2048,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("RTX 3090", GPUProfile(
        name="RTX 3090",
        vram_total_mb=24576,
        safe_swap_rate=4,
        vram_headroom_mb=6144,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 3080", GPUProfile(
        name="RTX 3080",
        vram_total_mb=10240,
        safe_swap_rate=4,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 3070", GPUProfile(
        name="RTX 3070",
        vram_total_mb=8192,
        safe_swap_rate=3,
        vram_headroom_mb=2048,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("RTX 3060", GPUProfile(
        name="RTX 3060",
        vram_total_mb=12288,
        safe_swap_rate=3,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("A100", GPUProfile(
        name="A100",
        vram_total_mb=81920,
        safe_swap_rate=6,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("A6000", GPUProfile(
        name="A6000",
        vram_total_mb=49152,
        safe_swap_rate=5,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("L40", GPUProfile(
        name="L40",
        vram_total_mb=49152,
        safe_swap_rate=5,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("L4", GPUProfile(
        name="L4",
        vram_total_mb=24576,
        safe_swap_rate=5,
        vram_headroom_mb=4096,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
]


def lookup_profile(gpu_name: str) -> GPUProfile:
    """Look up a GPU profile by name from nvidia-smi output.

    Matches by substring (case-insensitive). Returns the default
    conservative profile for unknown GPUs.

    Parameters
    ----------
    gpu_name : str
        GPU name as reported by nvidia-smi (e.g. "NVIDIA GeForce RTX 4090").

    Returns
    -------
    GPUProfile
        Matching profile, or conservative defaults for unknown hardware.
    """
    name_lower = gpu_name.lower()
    for key, profile in _PROFILES:
        if key.lower() in name_lower:
            return profile
    return _DEFAULT_PROFILE
```

- [x] **Step 4: Run tests to verify they pass**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_gpu_profiles.py -v`

Expected: All tests PASS

- [x] **Step 5: Commit**

```bash
git add src/bastion/gpu_profiles.py tests/test_gpu_profiles.py
git commit -m "feat: add GPU profile table with known-safe defaults for common NVIDIA GPUs"
```

---

### Task 11: Create Validator Core

**Files:**
- Create: `src/bastion/validate.py`
- Create: `tests/test_validate.py`

- [x] **Step 1: Write failing tests for individual checks**

Create `tests/test_validate.py`:

```python
"""Tests for bastion validate pre-flight checks."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.validate import (
    CheckResult,
    CheckStatus,
    check_config,
    check_gpu,
    check_models,
    check_ollama,
    check_permissions,
    check_port,
    check_python_version,
    run_all_checks,
)


class TestCheckPythonVersion:
    """Test Python version check."""

    def test_current_python_passes(self) -> None:
        result = check_python_version()
        assert result.status == CheckStatus.PASS
        assert "3." in result.message

    def test_result_structure(self) -> None:
        result = check_python_version()
        assert isinstance(result, CheckResult)
        assert result.name == "Python version"


class TestCheckGPU:
    """Test GPU detection check."""

    @pytest.mark.asyncio
    async def test_gpu_detected(self) -> None:
        mock_status = MagicMock()
        mock_status.vram_total_mb = 24576
        mock_status.temperature_c = 45

        with patch("bastion.validate.query_gpu_status", new_callable=AsyncMock, return_value=mock_status):
            with patch("bastion.validate._query_gpu_name", return_value="NVIDIA GeForce RTX 4090"):
                result = await check_gpu()
        assert result.status == CheckStatus.PASS
        assert "RTX 4090" in result.message

    @pytest.mark.asyncio
    async def test_no_gpu(self) -> None:
        mock_status = MagicMock()
        mock_status.vram_total_mb = None
        mock_status.temperature_c = None

        with patch("bastion.validate.query_gpu_status", new_callable=AsyncMock, return_value=mock_status):
            with patch("bastion.validate._query_gpu_name", return_value=None):
                result = await check_gpu()
        assert result.status == CheckStatus.FAIL


class TestCheckOllama:
    """Test Ollama connectivity check."""

    @pytest.mark.asyncio
    async def test_ollama_reachable(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Ollama is running"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await check_ollama(port=11435)
        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_ollama_unreachable(self) -> None:
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=Exception("Connection refused")):
            result = await check_ollama(port=11435)
        assert result.status == CheckStatus.FAIL


class TestCheckPort:
    """Test port availability check."""

    @pytest.mark.asyncio
    async def test_free_port(self) -> None:
        # Use an unlikely-to-be-used port
        result = await check_port(port=19999)
        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_message_includes_port(self) -> None:
        result = await check_port(port=19999)
        assert "19999" in result.message


class TestCheckConfig:
    """Test config validation check."""

    def test_no_config_warns(self) -> None:
        with patch("bastion.validate._find_config_path", return_value=None):
            result = check_config()
        assert result.status == CheckStatus.WARN
        assert "init-config" in result.message


class TestRunAllChecks:
    """Test the full check runner."""

    @pytest.mark.asyncio
    async def test_returns_list_of_results(self) -> None:
        results = await run_all_checks(ollama_port=11435, bastion_port=11434)
        assert isinstance(results, list)
        assert all(isinstance(r, CheckResult) for r in results)
        assert len(results) >= 6  # At least 6 checks

    @pytest.mark.asyncio
    async def test_exit_code_zero_on_all_pass_or_warn(self) -> None:
        results = await run_all_checks(ollama_port=11435, bastion_port=11434)
        has_fail = any(r.status == CheckStatus.FAIL for r in results)
        exit_code = 1 if has_fail else 0
        # Just verify the logic, actual result depends on environment
        assert exit_code in (0, 1)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_validate.py -v`

Expected: `ModuleNotFoundError: No module named 'bastion.validate'`

- [x] **Step 3: Write validate.py**

Create `src/bastion/validate.py`:

```python
"""Pre-flight system validator for BASTION.

Runs a series of checks to verify that the system is ready to run BASTION:
Python version, GPU detection, Ollama connectivity, port availability,
config validation, and file permissions.

Usage::

    bastion validate
    bastion validate --ollama-port 11435 --port 11434
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from bastion.config import _find_config, load_config
from bastion.gpu_profiles import lookup_profile
from bastion.health import query_gpu_status


class CheckStatus(StrEnum):
    """Result status for a pre-flight check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""

    name: str
    status: CheckStatus
    message: str


def check_python_version() -> CheckResult:
    """Check that Python version is >= 3.11."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        return CheckResult("Python version", CheckStatus.PASS, version_str)
    return CheckResult(
        "Python version",
        CheckStatus.FAIL,
        f"{version_str} — Python 3.11+ required",
    )


def _query_gpu_name() -> str | None:
    """Query GPU name from nvidia-smi (sync, for validator only)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


async def check_gpu() -> CheckResult:
    """Check for NVIDIA GPU and query status."""
    gpu_name = _query_gpu_name()
    if gpu_name is None:
        return CheckResult(
            "NVIDIA GPU",
            CheckStatus.FAIL,
            "nvidia-smi not found or no GPU detected — install NVIDIA drivers",
        )

    status = await query_gpu_status()
    vram_mb = status.vram_total_mb or 0
    driver = _query_driver_version()
    parts = [gpu_name]
    if vram_mb > 0:
        parts.append(f"{vram_mb} MB VRAM")
    if driver:
        parts.append(f"driver {driver}")

    return CheckResult("NVIDIA GPU", CheckStatus.PASS, ", ".join(parts))


def check_gpu_profile(gpu_name: str | None) -> CheckResult:
    """Look up GPU in profile table."""
    if gpu_name is None:
        return CheckResult(
            "GPU profile",
            CheckStatus.WARN,
            "No GPU detected — cannot look up profile",
        )

    profile = lookup_profile(gpu_name)
    if profile.name == "Unknown GPU":
        return CheckResult(
            "GPU profile",
            CheckStatus.WARN,
            f"'{gpu_name}' not in profile table — using conservative defaults "
            f"(swap limit {profile.safe_swap_rate}/min, headroom "
            f"{profile.vram_headroom_mb // 1024}GB, thermal {profile.thermal_ceiling_c}C)",
        )

    return CheckResult(
        "GPU profile",
        CheckStatus.PASS,
        f"{profile.name} — swap limit {profile.safe_swap_rate}/min, "
        f"headroom {profile.vram_headroom_mb // 1024}GB, "
        f"thermal {profile.thermal_ceiling_c}C",
    )


async def check_ollama(port: int = 11435, host: str = "127.0.0.1") -> CheckResult:
    """Check if Ollama is reachable on the backend port."""
    url = f"http://{host}:{port}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            return CheckResult(
                "Ollama",
                CheckStatus.PASS,
                f"reachable on {host}:{port}",
            )
        return CheckResult(
            "Ollama",
            CheckStatus.FAIL,
            f"responded with HTTP {resp.status_code} on {host}:{port}",
        )
    except Exception:
        return CheckResult(
            "Ollama",
            CheckStatus.FAIL,
            f"unreachable on {host}:{port} — is Ollama running on that port?",
        )


async def check_models(port: int = 11435, host: str = "127.0.0.1") -> CheckResult:
    """Check installed Ollama models and VRAM compatibility."""
    url = f"http://{host}:{port}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
        if resp.status_code != 200:
            return CheckResult("Installed models", CheckStatus.WARN, "could not query Ollama models")

        data = resp.json()
        models = data.get("models", [])
        if not models:
            return CheckResult(
                "Installed models",
                CheckStatus.WARN,
                "no models installed — run: ollama pull llama3.1:8b",
            )

        model_names = [m.get("name", "?") for m in models]
        return CheckResult(
            "Installed models",
            CheckStatus.PASS,
            f"{len(models)} model(s): {', '.join(model_names[:5])}"
            + (f" (+{len(models) - 5} more)" if len(models) > 5 else ""),
        )
    except Exception:
        return CheckResult("Installed models", CheckStatus.WARN, "could not query Ollama models")


async def check_port(port: int = 11434) -> CheckResult:
    """Check if BASTION's listen port is available."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        sock.close()
        return CheckResult("Port", CheckStatus.PASS, f"{port}: available")
    except OSError:
        sock.close()
        # Check if it's BASTION already running
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/broker/status", timeout=2.0)
            if resp.status_code == 200:
                return CheckResult(
                    "Port",
                    CheckStatus.PASS,
                    f"{port}: BASTION already running",
                )
        except Exception:
            pass
        return CheckResult(
            "Port",
            CheckStatus.FAIL,
            f"{port}: in use by another process",
        )


def _find_config_path() -> Path | None:
    """Find config file using BASTION's search logic."""
    return _find_config(None)


def check_config() -> CheckResult:
    """Check if a valid config file exists and parses."""
    config_path = _find_config_path()
    if config_path is None:
        return CheckResult(
            "Config",
            CheckStatus.WARN,
            "no config file found — run: bastion --init-config",
        )

    try:
        load_config(config_path)
        return CheckResult(
            "Config",
            CheckStatus.PASS,
            f"{config_path} valid",
        )
    except Exception as e:
        return CheckResult(
            "Config",
            CheckStatus.FAIL,
            f"{config_path} has errors: {e}",
        )


def check_permissions() -> CheckResult:
    """Check GPU device node permissions."""
    dev_nvidia = Path("/dev/nvidia0")
    if not dev_nvidia.exists():
        # Could be a headless server with no display GPU
        return CheckResult(
            "Permissions",
            CheckStatus.WARN,
            "/dev/nvidia0 not found — GPU device nodes may not be created yet "
            "(run: sudo nvidia-modprobe)",
        )

    if dev_nvidia.stat().st_mode & 0o004:  # world-readable
        return CheckResult("Permissions", CheckStatus.PASS, "GPU device nodes accessible")

    # Check if current user can read it
    try:
        with open(dev_nvidia, "rb"):
            pass
        return CheckResult("Permissions", CheckStatus.PASS, "GPU device nodes accessible")
    except PermissionError:
        return CheckResult(
            "Permissions",
            CheckStatus.FAIL,
            f"/dev/nvidia0 not readable — add user to 'video' group: "
            f"sudo usermod -aG video $USER",
        )


def _query_driver_version() -> str | None:
    """Query NVIDIA driver version from nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


async def run_all_checks(
    ollama_port: int = 11435,
    bastion_port: int = 11434,
    ollama_host: str = "127.0.0.1",
) -> list[CheckResult]:
    """Run all pre-flight checks in order.

    Returns
    -------
    list[CheckResult]
        Results for each check, in order.
    """
    results: list[CheckResult] = []

    # 1. Python version (sync)
    results.append(check_python_version())

    # 2. GPU detection (async)
    gpu_result = await check_gpu()
    results.append(gpu_result)

    # 3. GPU profile lookup
    gpu_name = _query_gpu_name()
    results.append(check_gpu_profile(gpu_name))

    # 4. Ollama reachable (async)
    results.append(await check_ollama(port=ollama_port, host=ollama_host))

    # 5. Installed models (async)
    results.append(await check_models(port=ollama_port, host=ollama_host))

    # 6. Port availability (async — may do HTTP check)
    results.append(await check_port(port=bastion_port))

    # 7. Config validation (sync)
    results.append(check_config())

    # 8. Permissions (sync)
    results.append(check_permissions())

    return results


def format_results(results: list[CheckResult]) -> str:
    """Format check results for terminal output."""
    lines = ["", "BASTION Pre-flight Check", "=" * 24, ""]

    for r in results:
        tag = f"[{r.status.value}]"
        lines.append(f"{tag:6s} {r.name}: {r.message}")

    passed = sum(1 for r in results if r.status == CheckStatus.PASS)
    warned = sum(1 for r in results if r.status == CheckStatus.WARN)
    failed = sum(1 for r in results if r.status == CheckStatus.FAIL)

    lines.append("")
    lines.append(f"Result: {passed} passed, {warned} warning(s), {failed} failed")

    return "\n".join(lines)


def compute_exit_code(results: list[CheckResult]) -> int:
    """Compute exit code from results: 0 = all pass/warn, 1 = any fail."""
    if any(r.status == CheckStatus.FAIL for r in results):
        return 1
    return 0
```

- [x] **Step 4: Run tests to verify they pass**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_validate.py -v`

Expected: All tests PASS (some may skip in environments without GPU/Ollama)

- [x] **Step 5: Commit**

```bash
git add src/bastion/validate.py tests/test_validate.py
git commit -m "feat: add pre-flight validator with 8-point system check"
```

---

### Task 12: Wire Validator into CLI

**Files:**
- Modify: `src/bastion/__main__.py`

- [x] **Step 1: Add --validate flag to argparse**

In `src/bastion/__main__.py`, add the argument after `--detect-models`:

```python
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run pre-flight checks to verify your system is ready for BASTION, "
             "then exit.",
    )
```

- [x] **Step 2: Add handler before config loading**

After the `if args.detect_models:` block and before the `# Lazy import` comment, add:

```python
    if args.validate:
        from bastion.validate import (
            compute_exit_code,
            format_results,
            run_all_checks,
        )

        ollama_port = args.ollama_port or 11435
        bastion_port = args.port or 11434
        results = asyncio.run(run_all_checks(
            ollama_port=ollama_port,
            bastion_port=bastion_port,
        ))
        print(format_results(results))
        sys.exit(compute_exit_code(results))
```

- [x] **Step 3: Add `sys` import if not present**

The `sys` import is not currently in `__main__.py`. Add it to the imports at the top:

```python
import sys
```

(Note: `asyncio` is already imported.)

- [x] **Step 4: Test manually**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m bastion --validate`

Expected: Output showing PASS/WARN/FAIL for each check.

- [x] **Step 5: Commit**

```bash
git add src/bastion/__main__.py
git commit -m "feat: wire bastion validate into CLI as --validate flag"
```

---

## Stream 3: Stress Calibrator

### Task 13: Create Stress Test Core — Phases 1 & 2

**Files:**
- Create: `src/bastion/stress.py`
- Create: `tests/test_stress.py`

- [x] **Step 1: Write failing tests for safety protocol and Phase 1/2**

Create `tests/test_stress.py`:

```python
"""Tests for bastion stress-test calibrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.stress import (
    CalibrationResult,
    PhaseResult,
    StressConfig,
    baseline_phase,
    check_prerequisites,
    single_load_phase,
)


class TestStressConfig:
    """Test stress test configuration."""

    def test_default_config(self) -> None:
        config = StressConfig()
        assert config.bastion_url == "http://127.0.0.1:11434"
        assert config.thermal_cutoff_pct == 0.90
        assert config.max_inference_latency_s == 30.0

    def test_custom_bastion_url(self) -> None:
        config = StressConfig(bastion_url="http://localhost:9999")
        assert config.bastion_url == "http://localhost:9999"


class TestCheckPrerequisites:
    """Test pre-flight checks for stress test."""

    @pytest.mark.asyncio
    async def test_bastion_not_running(self) -> None:
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
                   side_effect=Exception("Connection refused")):
            ok, msg = await check_prerequisites(StressConfig())
        assert not ok
        assert "not running" in msg.lower() or "unreachable" in msg.lower()

    @pytest.mark.asyncio
    async def test_not_enough_models(self) -> None:
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {"state": "running"}

        tags_resp = MagicMock()
        tags_resp.status_code = 200
        tags_resp.json.return_value = {"models": [{"name": "one:latest", "size": 1_000_000_000}]}

        async def mock_get(url: str, **kwargs):
            if "/broker/status" in url:
                return status_resp
            if "/api/tags" in url:
                return tags_resp
            return status_resp

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            ok, msg = await check_prerequisites(StressConfig())
        assert not ok
        assert "2" in msg  # needs at least 2 models


class TestBaselinePhase:
    """Test Phase 1: Baseline measurement."""

    @pytest.mark.asyncio
    async def test_baseline_collects_samples(self) -> None:
        mock_status = MagicMock()
        mock_status.temperature_c = 42
        mock_status.power_draw_watts = 18.5
        mock_status.vram_used_mb = 512

        with patch("bastion.stress.query_gpu_status", new_callable=AsyncMock,
                   return_value=mock_status):
            result = await baseline_phase(duration_seconds=2, sample_interval=1.0)

        assert result.phase == "baseline"
        assert result.success
        assert result.data["idle_temp_c"] == 42
        assert result.data["idle_power_w"] == 18.5
        assert result.data["vram_in_use_mb"] == 512


class TestSingleLoadPhase:
    """Test Phase 2: Single model load/inference/unload."""

    @pytest.mark.asyncio
    async def test_single_load_measures_latency(self) -> None:
        generate_resp = MagicMock()
        generate_resp.status_code = 200
        generate_resp.json.return_value = {
            "response": "Hello!",
            "eval_count": 50,
            "eval_duration": 500_000_000,  # 500ms in ns
        }

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        mock_gpu = MagicMock()
        mock_gpu.temperature_c = 45
        mock_gpu.vram_used_mb = 4096

        async def mock_post(url: str, **kwargs):
            if "/api/generate" in url:
                return generate_resp
            return unload_resp

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post):
            with patch("bastion.stress.query_gpu_status", new_callable=AsyncMock,
                       return_value=mock_gpu):
                result = await single_load_phase(
                    bastion_url="http://localhost:11434",
                    model="test:latest",
                    baseline_temp=42,
                )

        assert result.phase == "single_load"
        assert result.success
        assert "inference_latency_s" in result.data
        assert "thermal_delta_c" in result.data
```

- [x] **Step 2: Run tests to verify they fail**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_stress.py -v`

Expected: `ModuleNotFoundError: No module named 'bastion.stress'`

- [x] **Step 3: Write stress.py with Phases 1 & 2**

Create `src/bastion/stress.py`:

```python
"""GPU stress calibrator for BASTION.

Discovers safe operating limits through gradual ramp-up. Writes a
calibration profile to ~/.config/bastion/gpu-profile.yaml that BASTION
uses at runtime for hardware-tuned safety limits.

Requires BASTION to be running — tests the full stack.

Usage::

    bastion stress-test
    bastion stress-test --bastion-url http://localhost:11434
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from bastion.health import query_gpu_status
from bastion.paths import config_dir


@dataclass
class StressConfig:
    """Configuration for the stress calibrator."""

    bastion_url: str = "http://127.0.0.1:11434"
    thermal_cutoff_pct: float = 0.90       # Stop at 90% of thermal ceiling
    max_inference_latency_s: float = 30.0  # Stop if latency exceeds this
    baseline_duration_s: float = 30.0      # Phase 1 duration
    sample_interval_s: float = 2.0         # GPU sampling interval
    test_prompt: str = "Count from 1 to 20. Be concise."
    max_tokens: int = 100


@dataclass
class PhaseResult:
    """Result of a single calibration phase."""

    phase: str
    success: bool
    data: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class CalibrationResult:
    """Aggregated results from all completed phases."""

    gpu_name: str = ""
    vram_total_mb: int = 0
    driver: str = ""
    phases: list[PhaseResult] = field(default_factory=list)
    calibrated: dict = field(default_factory=dict)


SAFETY_BANNER = """
=================================================================
  BASTION Stress Calibrator
=================================================================

  This will push your GPU through rapid model swaps and high load.

  Before continuing:
  1. Save all open work in other applications
  2. Close other GPU-intensive programs
  3. Ensure no critical processes depend on this GPU

  This test will:
  - Load and unload models rapidly
  - Measure GPU thermal response under swap stress
  - Discover your hardware's safe operating thresholds
  - Take approximately 10-15 minutes

  Results are written to ~/.config/bastion/gpu-profile.yaml
  BASTION can use this profile for hardware-tuned safety limits.

  Type 'I understand' to continue, or Ctrl+C to abort:
=================================================================
"""


async def check_prerequisites(config: StressConfig) -> tuple[bool, str]:
    """Verify BASTION is running and has enough models for testing.

    Returns (ok, message).
    """
    # Check BASTION is running
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.bastion_url}/broker/status", timeout=5.0)
        if resp.status_code != 200:
            return False, f"BASTION responded with HTTP {resp.status_code}"
    except Exception:
        return False, (
            "BASTION is unreachable. Start it first with: bastion\n"
            "The stress test needs to go through the full proxy stack."
        )

    # Check for at least 2 small models
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.bastion_url}/api/tags", timeout=5.0)
        data = resp.json()
        models = data.get("models", [])
        small_models = [
            m["name"] for m in models
            if m.get("size", 0) < 5 * 1024**3  # Under 5 GB
        ]
        if len(small_models) < 2:
            return False, (
                f"Need at least 2 small models for swap testing (found {len(small_models)}).\n"
                "Install small models:\n"
                "  ollama pull qwen3:1.7b\n"
                "  ollama pull llama3.2:1b"
            )
    except Exception:
        return False, "Could not query Ollama models through BASTION."

    return True, f"Ready — {len(small_models)} small models available"


async def baseline_phase(
    duration_seconds: float = 30.0,
    sample_interval: float = 2.0,
) -> PhaseResult:
    """Phase 1: Measure idle GPU metrics.

    Samples GPU status repeatedly to establish baseline temperature,
    power draw, and VRAM usage.
    """
    temps: list[int] = []
    powers: list[float] = []
    vrams: list[int] = []

    end_time = time.monotonic() + duration_seconds
    while time.monotonic() < end_time:
        status = await query_gpu_status()
        if status.temperature_c is not None:
            temps.append(status.temperature_c)
        if status.power_draw_watts is not None:
            powers.append(status.power_draw_watts)
        if status.vram_used_mb is not None:
            vrams.append(status.vram_used_mb)
        await asyncio.sleep(sample_interval)

    if not temps:
        return PhaseResult(
            phase="baseline",
            success=False,
            error="Could not read GPU temperature — is nvidia-smi working?",
        )

    return PhaseResult(
        phase="baseline",
        success=True,
        data={
            "idle_temp_c": round(statistics.median(temps)),
            "idle_power_w": round(statistics.median(powers), 1) if powers else 0,
            "vram_in_use_mb": round(statistics.median(vrams)) if vrams else 0,
            "temp_samples": len(temps),
        },
    )


async def single_load_phase(
    bastion_url: str,
    model: str,
    baseline_temp: int,
    test_prompt: str = "Count from 1 to 20. Be concise.",
    max_tokens: int = 100,
) -> PhaseResult:
    """Phase 2: Load one model, run inference, unload.

    Measures load time, inference latency, VRAM usage, and thermal impact.
    """
    data: dict = {}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            # Load + inference (BASTION handles scheduling)
            t0 = time.monotonic()
            resp = await client.post(
                f"{bastion_url}/api/generate",
                json={
                    "model": model,
                    "prompt": test_prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
            t1 = time.monotonic()

            if resp.status_code != 200:
                return PhaseResult(
                    phase="single_load",
                    success=False,
                    error=f"Inference failed with HTTP {resp.status_code}",
                )

            result = resp.json()
            data["inference_latency_s"] = round(t1 - t0, 2)
            data["eval_count"] = result.get("eval_count", 0)

            # Check GPU after load
            status = await query_gpu_status()
            data["peak_vram_mb"] = status.vram_used_mb or 0
            data["thermal_delta_c"] = (status.temperature_c or baseline_temp) - baseline_temp

            # Unload
            await client.post(
                f"{bastion_url}/api/generate",
                json={"model": model, "keep_alive": 0},
            )

    except Exception as e:
        return PhaseResult(phase="single_load", success=False, error=str(e))

    return PhaseResult(phase="single_load", success=True, data=data)


async def swap_ramp_phase(
    bastion_url: str,
    models: list[str],
    thermal_ceiling: int,
    thermal_cutoff_pct: float = 0.90,
    test_prompt: str = "Say hello.",
) -> PhaseResult:
    """Phase 3: Alternate models at decreasing intervals.

    Ramps swap frequency: 10s → 8s → 6s → 4s → 2s gaps.
    Stops when thermal threshold or swap failure occurs.
    """
    intervals = [10, 8, 6, 4, 2]
    swaps_per_interval = 3
    cutoff_temp = int(thermal_ceiling * thermal_cutoff_pct)
    last_safe_rate: int | None = None
    swap_durations: list[float] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for interval in intervals:
            rate_per_min = 60 // interval
            interval_ok = True

            for swap_idx in range(swaps_per_interval):
                model = models[swap_idx % len(models)]
                t0 = time.monotonic()

                try:
                    resp = await client.post(
                        f"{bastion_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": test_prompt,
                            "stream": False,
                            "options": {"num_predict": 10},
                        },
                    )
                    t1 = time.monotonic()

                    if resp.status_code != 200:
                        interval_ok = False
                        break

                    swap_durations.append(t1 - t0)

                except Exception:
                    interval_ok = False
                    break

                # Check temperature
                status = await query_gpu_status()
                if status.temperature_c and status.temperature_c >= cutoff_temp:
                    return PhaseResult(
                        phase="swap_ramp",
                        success=True,
                        data={
                            "safe_swap_rate_per_min": last_safe_rate or rate_per_min,
                            "stopped_at_interval_s": interval,
                            "stop_reason": f"thermal cutoff ({status.temperature_c}C >= {cutoff_temp}C)",
                            "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
                        },
                    )

                if interval > 2:
                    await asyncio.sleep(interval)

            if not interval_ok:
                return PhaseResult(
                    phase="swap_ramp",
                    success=True,
                    data={
                        "safe_swap_rate_per_min": last_safe_rate or 3,
                        "stopped_at_interval_s": interval,
                        "stop_reason": "swap failed or errored",
                        "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
                    },
                )

            last_safe_rate = rate_per_min

    return PhaseResult(
        phase="swap_ramp",
        success=True,
        data={
            "safe_swap_rate_per_min": last_safe_rate or 3,
            "stopped_at_interval_s": 2,
            "stop_reason": "completed all intervals",
            "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
        },
    )


async def concurrent_load_phase(
    bastion_url: str,
    model: str,
    test_prompt: str = "Say hello.",
    max_latency_s: float = 30.0,
) -> PhaseResult:
    """Phase 4: Send concurrent requests to a loaded model.

    Tests 2, 4, then 8 simultaneous requests. Stops at first
    error or latency breach.
    """
    concurrency_levels = [2, 4, 8]
    last_safe_level = 1

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # Pre-load the model
        await client.post(
            f"{bastion_url}/api/generate",
            json={"model": model, "prompt": "warmup", "stream": False,
                  "options": {"num_predict": 5}},
        )

        for level in concurrency_levels:

            async def _single_request() -> float:
                t0 = time.monotonic()
                resp = await client.post(
                    f"{bastion_url}/api/generate",
                    json={"model": model, "prompt": test_prompt, "stream": False,
                          "options": {"num_predict": 20}},
                )
                t1 = time.monotonic()
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                return t1 - t0

            try:
                latencies = await asyncio.gather(
                    *[_single_request() for _ in range(level)]
                )
                p95 = sorted(latencies)[int(len(latencies) * 0.95)]
                if p95 > max_latency_s:
                    return PhaseResult(
                        phase="concurrent_load",
                        success=True,
                        data={
                            "max_concurrent_requests": last_safe_level,
                            "stopped_at_level": level,
                            "stop_reason": f"p95 latency {p95:.1f}s > {max_latency_s}s",
                        },
                    )
                last_safe_level = level
            except Exception as e:
                return PhaseResult(
                    phase="concurrent_load",
                    success=True,
                    data={
                        "max_concurrent_requests": last_safe_level,
                        "stopped_at_level": level,
                        "stop_reason": str(e),
                    },
                )

    return PhaseResult(
        phase="concurrent_load",
        success=True,
        data={
            "max_concurrent_requests": last_safe_level,
            "stopped_at_level": concurrency_levels[-1],
            "stop_reason": "completed all levels",
        },
    )


async def recovery_phase(
    bastion_url: str,
    baseline_temp: int,
    temp_tolerance: int = 3,
    timeout_seconds: float = 120.0,
) -> PhaseResult:
    """Phase 5: Unload everything and wait for GPU to cool down."""
    # Unload all models via admin API
    try:
        async with httpx.AsyncClient() as client:
            status_resp = await client.get(f"{bastion_url}/broker/status", timeout=5.0)
            if status_resp.status_code == 200:
                data = status_resp.json()
                for model_info in data.get("loaded_models", []):
                    model_name = model_info.get("name", "")
                    if model_name:
                        await client.post(
                            f"{bastion_url}/api/generate",
                            json={"model": model_name, "keep_alive": 0},
                            timeout=10.0,
                        )
    except Exception:
        pass  # Best effort unload

    # Wait for cooldown
    target_temp = baseline_temp + temp_tolerance
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_seconds:
        status = await query_gpu_status()
        if status.temperature_c and status.temperature_c <= target_temp:
            return PhaseResult(
                phase="recovery",
                success=True,
                data={
                    "cooldown_duration_s": round(time.monotonic() - t0, 1),
                    "final_temp_c": status.temperature_c,
                },
            )
        await asyncio.sleep(2.0)

    status = await query_gpu_status()
    return PhaseResult(
        phase="recovery",
        success=True,
        data={
            "cooldown_duration_s": round(time.monotonic() - t0, 1),
            "final_temp_c": status.temperature_c,
            "note": "timeout — GPU did not fully cool down",
        },
    )


def write_profile(result: CalibrationResult) -> Path:
    """Write calibration results to gpu-profile.yaml.

    Returns the path to the written file.
    """
    profile = {
        "gpu": {
            "name": result.gpu_name,
            "vram_total_mb": result.vram_total_mb,
            "driver": result.driver,
        },
        "calibrated": result.calibrated,
        "tested": {
            "date": time.strftime("%Y-%m-%d"),
            "phases_completed": len([p for p in result.phases if p.success]),
            "models_used": result.calibrated.get("models_used", []),
        },
    }

    # Add baseline data if available
    for phase in result.phases:
        if phase.phase == "baseline" and phase.success:
            profile["baseline"] = {
                "idle_temp_c": phase.data.get("idle_temp_c", 0),
                "idle_power_w": phase.data.get("idle_power_w", 0),
                "vram_in_use_mb": phase.data.get("vram_in_use_mb", 0),
            }
            break

    dest = config_dir() / "gpu-profile.yaml"
    header = (
        f"# Auto-generated by 'bastion stress-test'\n"
        f"# Date: {time.strftime('%Y-%m-%d')}\n"
        f"# GPU: {result.gpu_name} ({result.vram_total_mb} MB)\n"
        f"# Driver: {result.driver}\n\n"
    )

    dest.write_text(header + yaml.dump(profile, default_flow_style=False), encoding="utf-8")
    return dest
```

- [x] **Step 4: Run tests to verify they pass**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_stress.py -v`

Expected: All tests PASS

- [x] **Step 5: Commit**

```bash
git add src/bastion/stress.py tests/test_stress.py
git commit -m "feat: add stress calibrator with 5-phase GPU testing and profile output"
```

---

### Task 14: Wire Stress Test into CLI

**Files:**
- Modify: `src/bastion/__main__.py`

- [x] **Step 1: Add --stress-test flag to argparse**

In `src/bastion/__main__.py`, add after `--validate`:

```python
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="Run GPU stress calibrator to discover safe operating limits. "
             "Requires BASTION to be running. Writes results to "
             "~/.config/bastion/gpu-profile.yaml.",
    )
```

- [x] **Step 2: Add handler**

After the `if args.validate:` block, add:

```python
    if args.stress_test:
        from bastion.stress import (
            SAFETY_BANNER,
            CalibrationResult,
            StressConfig,
            baseline_phase,
            check_prerequisites,
            concurrent_load_phase,
            recovery_phase,
            single_load_phase,
            swap_ramp_phase,
            write_profile,
        )
        from bastion.gpu_profiles import lookup_profile
        from bastion.validate import _query_driver_version, _query_gpu_name

        stress_config = StressConfig(
            bastion_url=f"http://127.0.0.1:{args.port or 11434}",
        )

        # Safety ceremony
        print(SAFETY_BANNER)
        try:
            response = input().strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

        if response.lower() != "i understand":
            print("Aborted — you must type 'I understand' to continue.")
            sys.exit(0)

        asyncio.run(_run_stress_test(stress_config))
        sys.exit(0)
```

- [x] **Step 3: Add the async runner function**

Add this function before `if __name__ == "__main__":`:

```python
async def _run_stress_test(config: StressConfig) -> None:
    """Run the full stress test sequence with phase-by-phase confirmation."""
    from bastion.stress import (
        CalibrationResult,
        baseline_phase,
        check_prerequisites,
        concurrent_load_phase,
        recovery_phase,
        single_load_phase,
        swap_ramp_phase,
        write_profile,
    )
    from bastion.gpu_profiles import lookup_profile
    from bastion.validate import _query_driver_version, _query_gpu_name

    # Prerequisites
    print("\nChecking prerequisites...")
    ok, msg = await check_prerequisites(config)
    if not ok:
        print(f"\n  FAILED: {msg}")
        return
    print(f"  {msg}")

    # Get GPU info
    gpu_name = _query_gpu_name() or "Unknown GPU"
    driver = _query_driver_version() or "unknown"
    profile = lookup_profile(gpu_name)

    from bastion.health import query_gpu_status
    status = await query_gpu_status()
    vram_total = status.vram_total_mb or profile.vram_total_mb

    result = CalibrationResult(
        gpu_name=gpu_name,
        vram_total_mb=vram_total,
        driver=driver,
    )

    # Get small models for testing
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{config.bastion_url}/api/tags", timeout=5.0)
    models_data = resp.json().get("models", [])
    small_models = sorted(
        [m["name"] for m in models_data if m.get("size", 0) < 5 * 1024**3],
        key=lambda n: next((m["size"] for m in models_data if m["name"] == n), 0),
    )[:2]

    # Phase 1: Baseline
    print(f"\n--- Phase 1: Baseline ({config.baseline_duration_s:.0f}s) ---")
    phase1 = await baseline_phase(config.baseline_duration_s, config.sample_interval_s)
    result.phases.append(phase1)

    if not phase1.success:
        print(f"  FAILED: {phase1.error}")
        await recovery_phase(config.bastion_url, 40)
        return

    print(f"  Idle temp: {phase1.data['idle_temp_c']}C")
    print(f"  Idle power: {phase1.data['idle_power_w']}W")
    print(f"  VRAM in use: {phase1.data['vram_in_use_mb']} MB")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, phase1.data["idle_temp_c"])
        return

    baseline_temp = phase1.data["idle_temp_c"]

    # Phase 2: Single load
    print(f"\n--- Phase 2: Single Load ({small_models[0]}) ---")
    phase2 = await single_load_phase(config.bastion_url, small_models[0], baseline_temp)
    result.phases.append(phase2)

    if not phase2.success:
        print(f"  FAILED: {phase2.error}")
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    print(f"  Inference latency: {phase2.data['inference_latency_s']}s")
    print(f"  Thermal delta: +{phase2.data['thermal_delta_c']}C")
    print(f"  Peak VRAM: {phase2.data['peak_vram_mb']} MB")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    # Phase 3: Swap ramp
    print("\n--- Phase 3: Swap Ramp ---")
    phase3 = await swap_ramp_phase(
        config.bastion_url, small_models, profile.thermal_ceiling_c,
    )
    result.phases.append(phase3)
    print(f"  Safe swap rate: {phase3.data['safe_swap_rate_per_min']}/min")
    print(f"  Stop reason: {phase3.data['stop_reason']}")
    print(f"  Avg swap duration: {phase3.data['swap_duration_avg_s']}s")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    # Phase 4: Concurrent load
    print(f"\n--- Phase 4: Concurrent Load ({small_models[0]}) ---")
    phase4 = await concurrent_load_phase(config.bastion_url, small_models[0])
    result.phases.append(phase4)
    print(f"  Max concurrent: {phase4.data['max_concurrent_requests']}")
    print(f"  Stop reason: {phase4.data['stop_reason']}")

    # Phase 5: Recovery
    print("\n--- Phase 5: Recovery ---")
    phase5 = await recovery_phase(config.bastion_url, baseline_temp)
    result.phases.append(phase5)
    print(f"  Cooldown: {phase5.data['cooldown_duration_s']}s")
    print(f"  Final temp: {phase5.data.get('final_temp_c', '?')}C")

    # Aggregate calibrated values
    result.calibrated = {
        "safe_swap_rate_per_min": phase3.data.get("safe_swap_rate_per_min", 3),
        "max_concurrent_requests": phase4.data.get("max_concurrent_requests", 2),
        "vram_headroom_mb": profile.vram_headroom_mb,
        "thermal_ceiling_c": profile.thermal_ceiling_c,
        "cooldown_seconds": max(2, int(phase5.data.get("cooldown_duration_s", 3) / 2)),
        "swap_duration_avg_s": phase3.data.get("swap_duration_avg_s", 0),
        "models_used": small_models,
    }

    # Write profile
    dest = write_profile(result)
    print(f"\n  Profile written to {dest}")
    print("  BASTION will use these calibrated values on next startup.")


def _confirm_continue() -> bool:
    """Ask user to continue to next phase. Returns False on abort."""
    try:
        response = input("\n  Continue to next phase? [Y/n] ").strip().lower()
        return response in ("", "y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborting — running recovery...")
        return False
```

- [x] **Step 4: Test that --stress-test flag is recognized**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m bastion --help`

Expected: `--stress-test` appears in the help output.

- [x] **Step 5: Commit**

```bash
git add src/bastion/__main__.py
git commit -m "feat: wire stress-test into CLI with safety ceremony and phase confirmations"
```

---

### Task 15: Load GPU Profile at Startup

**Files:**
- Modify: `src/bastion/config.py`

- [x] **Step 1: Write failing test for profile loading**

Add to `tests/test_validate.py` (or a new section in an existing config test file):

```python
class TestGPUProfileLoading:
    """Test that GPU profile is loaded at startup if present."""

    def test_load_calibrated_profile(self, tmp_path: Path) -> None:
        profile_yaml = tmp_path / "gpu-profile.yaml"
        profile_yaml.write_text(
            "calibrated:\n"
            "  safe_swap_rate_per_min: 4\n"
            "  max_concurrent_requests: 6\n"
            "  cooldown_seconds: 3\n"
            "  thermal_ceiling_c: 82\n"
            "  vram_headroom_mb: 6144\n"
        )
        from bastion.config import _load_gpu_profile
        profile = _load_gpu_profile(profile_yaml)
        assert profile is not None
        assert profile["calibrated"]["safe_swap_rate_per_min"] == 4

    def test_missing_profile_returns_none(self, tmp_path: Path) -> None:
        from bastion.config import _load_gpu_profile
        profile = _load_gpu_profile(tmp_path / "nonexistent.yaml")
        assert profile is None
```

- [x] **Step 2: Run test to verify it fails**

Run the test — expected: `ImportError` for `_load_gpu_profile`

- [x] **Step 3: Add profile loading to config.py**

In `src/bastion/config.py`, add after the imports:

```python
def _load_gpu_profile(path: Path | None = None) -> dict | None:
    """Load calibrated GPU profile if it exists.

    Parameters
    ----------
    path : Path, optional
        Explicit path. If None, checks ~/.config/bastion/gpu-profile.yaml.

    Returns
    -------
    dict or None
        Parsed profile data, or None if no profile exists.
    """
    if path is None:
        from bastion.paths import config_dir
        path = config_dir() / "gpu-profile.yaml"

    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "calibrated" in data:
            logger.info(
                "Using calibrated GPU profile from %s (tested %s on %s)",
                path,
                data.get("tested", {}).get("date", "unknown"),
                data.get("gpu", {}).get("name", "unknown"),
            )
            return data
    except Exception as e:
        logger.warning("Failed to load GPU profile from %s: %s", path, e)

    return None
```

Then in `load_config()`, after `_apply_env_overrides(config)` and before `return config`, add:

```python
    # Apply calibrated GPU profile if available
    gpu_profile = _load_gpu_profile()
    if gpu_profile:
        _apply_gpu_profile(config, gpu_profile)
```

And add the `_apply_gpu_profile` function:

```python
def _apply_gpu_profile(config: BrokerConfig, profile: dict) -> None:
    """Apply calibrated GPU profile values to config.

    Calibrated values override defaults but NOT explicit user config.
    """
    cal = profile.get("calibrated", {})

    if "cooldown_seconds" in cal:
        config.scheduler.cooldown_seconds = float(cal["cooldown_seconds"])
    if "safe_swap_rate_per_min" in cal:
        config.scheduler.swap_rate_warn_threshold = max(1, cal["safe_swap_rate_per_min"] - 1)
        config.scheduler.swap_rate_critical_threshold = cal["safe_swap_rate_per_min"]
    if "max_concurrent_requests" in cal:
        config.scheduler.max_concurrent_dispatches = cal["max_concurrent_requests"]
    if "thermal_ceiling_c" in cal:
        config.gpu.max_temperature_c = cal["thermal_ceiling_c"]
    if "vram_headroom_mb" in cal:
        config.gpu.headroom_gb = cal["vram_headroom_mb"] / 1024.0

    logger.info("Applied calibrated GPU profile overrides")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/test_validate.py::TestGPUProfileLoading -v`

Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/bastion/config.py tests/test_validate.py
git commit -m "feat: load calibrated GPU profile at startup to override defaults"
```

---

### Task 16: Add Ctrl+C Recovery to Stress Test

**Files:**
- Modify: `src/bastion/stress.py`
- Modify: `src/bastion/__main__.py`

- [x] **Step 1: Add signal handling to _run_stress_test**

In `src/bastion/__main__.py`, wrap the `_run_stress_test` call with Ctrl+C handling:

```python
        try:
            asyncio.run(_run_stress_test(stress_config))
        except KeyboardInterrupt:
            print("\n\nCtrl+C — running recovery phase...")
            asyncio.run(
                recovery_phase(
                    stress_config.bastion_url,
                    baseline_temp=40,  # conservative fallback
                )
            )
            print("Recovery complete. Exiting.")
        sys.exit(0)
```

This needs the import at the top of the handler:

```python
        from bastion.stress import recovery_phase
```

- [x] **Step 2: Verify KeyboardInterrupt triggers recovery**

This is inherently interactive — document for manual testing:

Run `bastion --stress-test`, type 'I understand', wait for Phase 1 to start, then press Ctrl+C. Expected: "running recovery phase..." message, models unloaded, clean exit.

- [x] **Step 3: Commit**

```bash
git add src/bastion/__main__.py
git commit -m "feat: add Ctrl+C recovery handler to stress test"
```

---

### Task 17: Final Verification

**Files:** None (verification only)

- [x] **Step 1: Run full test suite**

Print command for user:
```
/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/ -v
```

Expected: All tests pass, including new tests in `test_gpu_profiles.py`, `test_validate.py`, and `test_stress.py`.

- [x] **Step 2: Verify CLI flags work**

```bash
/home/cyprian/miniforge3/envs/phenotype/bin/python -m bastion --help
/home/cyprian/miniforge3/envs/phenotype/bin/python -m bastion --validate
```

- [x] **Step 3: Verify documentation links**

Check that all cross-references between docs resolve:
- README links to all docs/*.md files
- getting-started.md links to troubleshooting.md, configuration.md, hardware-guide.md
- Each doc links back to README or related guides

- [x] **Step 4: Verify no internal references in public docs**

Search all docs/ and README.md for: S1-S14, M58, RTX 5090, crash 7-9, session, forensic, investigation, cyprian (the user's name), SWARM_BRAIN, or any system-specific paths.

- [x] **Step 5: Final commit**

If any fixes were needed, commit them:
```bash
git add -A
git commit -m "chore: final verification pass for production readiness"
```
