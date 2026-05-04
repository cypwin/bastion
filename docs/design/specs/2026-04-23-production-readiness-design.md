# Production Readiness: Documentation, Validation & Calibration

> Design spec for BASTION's public release preparation.
> Approved 2026-04-23. Three parallel streams.

## Goal

Take BASTION from "works on my machine" to "a stranger can install it from PyPI,
validate their hardware, and run it safely" — without rushing, across multiple sessions.

## Audience

Someone who discovers BASTION on PyPI or GitHub. They have:
- A Linux machine with an NVIDIA GPU
- Ollama installed
- A reason to broker GPU access (multiple agents, crash prevention, queue management)
- No knowledge of our development history, crash forensics, or internal tooling

## Non-Goals

- macOS/AMD/Windows support (deferred)
- Multi-GPU (documented as unsupported, single-instance-per-GPU workaround noted)
- Blog posts or development journey content (later, from this working repo)
- Changing BASTION's runtime behavior (this is docs + tooling only)

---

## Stream 1 — Public Documentation

### Objective

A clean, self-contained documentation set that ships with the package. No internal
history, no session references, no system-specific details.

### Documents

| Document | Purpose | Scope |
|----------|---------|-------|
| `README.md` (rewrite) | First-touch landing page | Problem statement, prerequisites checklist, 5-minute quickstart, feature overview, "where to go next" links |
| `docs/getting-started.md` (new) | Full installation walkthrough | Ollama setup, port move, `--init-config`, `--detect-models`, first request, verification |
| `docs/hardware-guide.md` (new) | GPU compatibility & requirements | Compatibility table (tested/expected-to-work/unknown), VRAM per model size, minimum vs recommended specs |
| `docs/configuration.md` (new) | Complete config reference | Every config option with explanation and examples. Preset snippets for common scenarios: 8GB GPU, 24GB GPU, multi-agent pipeline |
| `docs/troubleshooting.md` (new) | "I see X, do Y" guide | 15-20 common issues: won't start, Ollama unreachable, VRAM exhausted, port conflict, dashboard won't launch, GPU overheating, etc. |
| `docs/operations.md` (new) | Day-2 operations | Which metrics matter, safe restart procedure, queue management, model preloading, when to worry, log locations |
| `docs/security.md` (rewrite from SECURITY.md) | Practical security howto | Auth setup walkthrough, nftables port lockdown, reverse proxy TLS with Caddy/nginx examples, threat model (localhost-only by design) |
| `CHANGELOG.md` (rewrite) | User-facing release notes | Clean version history, no session tags, no internal references |
| `docs/crash-prevention.md` (rewrite) | Safety mechanisms explained | What BASTION does to prevent GPU crashes and why — mechanisms only, not the forensic investigation narrative |

### Files to Archive

These files move to `_archive/` with a note explaining why. They remain in the
working repo for internal reference but are excluded from the public release.

| File/Directory | Reason |
|----------------|--------|
| `docs/audit/*` (25+ files) | Internal analyst reports — development artifacts, not user-facing |
| `M58_BASTION_HANDOFF.md` | Session handoff — internal development history |
| `reference/` | Crash investigation raw data — internal forensics |
| `ROADMAP.md` | References internal sessions S1-S14; replaced by clean public roadmap in README |

These directories should be `.gitignore`'d in the public repo (not archived):

| Directory | Reason |
|-----------|--------|
| `.claude/` | Claude Code memory/config — developer-specific |
| `.idea/`, `.vscode/` | IDE config — developer-specific |

### Content Principles

- **No session tags** (S1, M58, etc.) in any public-facing text
- **No references to specific hardware** ("my RTX 5090") — use generic GPU language
- **No internal jargon without explanation** — define "affinity queue," "VRAM ledger," etc. on first use
- **Every doc is self-contained** — a user can land on any page and orient themselves
- **Actionable over explanatory** — "run this command" over "the architecture suggests"
- **Copy-paste friendly** — all commands should work when pasted into a terminal

---

## Stream 2 — Pre-flight Validator (`bastion validate`)

### Objective

A new CLI subcommand that checks whether the user's system can run BASTION safely.
Read-only, no side effects, safe to run anytime.

### Implementation

New file: `src/bastion/validate.py`
CLI integration: new subcommand in `__main__.py`

### Checks (in order)

| # | Check | Pass | Warn | Fail |
|---|-------|------|------|------|
| 1 | **Python version** | >= 3.11 | — | < 3.11 |
| 2 | **NVIDIA driver & GPU** | nvidia-smi responds, GPU detected | — | nvidia-smi not found or no GPU |
| 3 | **GPU profile lookup** | Known GPU matched in profile table | Unknown GPU (conservative defaults applied) | — |
| 4 | **Ollama reachable** | HTTP 200 from backend port | — | Connection refused or timeout |
| 5 | **Installed models** | At least 1 model, all within VRAM budget | Model exceeds available VRAM budget | No models installed |
| 6 | **Port availability** | BASTION port free (or BASTION already running) | — | Port in use by non-BASTION process |
| 7 | **Config validation** | Config file parses and validates | No config file (suggest `--init-config`) | Config file has errors |
| 8 | **Permissions** | GPU device nodes readable, user in correct groups | — | Cannot access /dev/nvidia* |

### Output Format

```
BASTION Pre-flight Check
========================

[PASS] Python 3.12.3
[PASS] NVIDIA RTX 4090 (24576 MB VRAM, driver 570.86)
[PASS] GPU profile: RTX 4090 — swap limit 5/min, headroom 6GB, thermal 83C
[PASS] Ollama: reachable on localhost:11435 (v0.6.3)
[WARN] Model llama3.1:70b (~40GB) exceeds VRAM budget (18GB available)
[PASS] Port 11434: available
[PASS] Config: ~/.config/bastion/broker.yaml valid
[PASS] Permissions: GPU device nodes accessible

Result: 7 passed, 1 warning, 0 failed
```

### GPU Profile Table

A data structure (Python dict or YAML file) mapping GPU names to known-safe defaults:

```yaml
profiles:
  "RTX 3060":
    vram_total_mb: 12288
    safe_swap_rate: 3
    vram_headroom_mb: 3072
    thermal_ceiling_c: 83
    cooldown_seconds: 3

  "RTX 4090":
    vram_total_mb: 24576
    safe_swap_rate: 5
    vram_headroom_mb: 6144
    thermal_ceiling_c: 83
    cooldown_seconds: 2

  "RTX 5090":
    vram_total_mb: 32768
    safe_swap_rate: 4
    vram_headroom_mb: 8192
    thermal_ceiling_c: 80
    cooldown_seconds: 2
    notes: "use_mmap: false mandatory — memory-mapped loading causes kernel panic"

  "_default":
    safe_swap_rate: 3
    vram_headroom_mb: 4096
    thermal_ceiling_c: 80
    cooldown_seconds: 3
```

Shipped as `src/bastion/gpu_profiles.py` — importable by both the validator and
the stress calibrator. Users can contribute profiles via PR.

### Exit Codes

- `0` — all checks pass (warnings are ok)
- `1` — one or more checks failed
- `2` — validator itself crashed (bug)

---

## Stream 3 — Stress Calibrator (`bastion stress-test`)

### Objective

A guided stress test that discovers the user's GPU's safe operating limits through
gradual ramp-up. Writes a calibration profile that BASTION uses at runtime.

### Prerequisites

- BASTION must be running (tests the full stack: proxy -> scheduler -> Ollama)
- At least 2 small models installed (suggests `qwen3:1.7b` + `llama3.2:1b` if missing)
- `bastion validate` should pass first (suggested, not enforced)

### Implementation

New file: `src/bastion/stress.py`
CLI integration: new subcommand in `__main__.py`

### Safety Protocol

Before any GPU work, the script displays:

```
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
```

This prompt is mandatory and cannot be skipped with a flag.

### Calibration Phases

#### Phase 1: Baseline (30 seconds)

- Sample idle GPU metrics every 2 seconds
- Record: resting temperature, idle power draw, VRAM in use by other processes
- Establish thermal headroom = thermal_ceiling - resting_temp
- **Report results, ask to continue**

#### Phase 2: Single Load (model-dependent, ~1-2 minutes)

- Load one small model via BASTION proxy
- Run a short inference (fixed prompt, ~100 tokens)
- Record: load time, inference latency, peak VRAM, thermal delta from baseline
- Unload model
- Wait for temp to return within 3C of baseline
- **Report results, ask to continue**

#### Phase 3: Swap Ramp (3-5 minutes)

- Alternate between 2 models at decreasing intervals:
  - 10s gap -> 8s -> 6s -> 4s -> 2s
- At each step (run 3 swaps per interval):
  - Record: swap duration, temp after swap, VRAM stability
  - If temp exceeds 90% of thermal ceiling: stop, report last safe interval
  - If swap fails (timeout, OOM, Ollama error): stop, report last safe interval
- Derive `safe_swap_rate_per_min` from the last interval that completed cleanly
- **Report results, ask to continue**

#### Phase 4: Concurrent Load (2-3 minutes)

- Load one model, keep it resident
- Send 2, then 4, then 8 simultaneous inference requests (fixed short prompt)
- At each concurrency level:
  - Record: p50/p95 latency, throughput (tokens/sec), error count
  - If any request errors or latency > 30s: stop, report last safe level
- Derive `max_concurrent_requests` from last clean concurrency level
- **Report results, ask to continue**

#### Phase 5: Recovery (1-2 minutes, also triggered by Ctrl+C)

- Unload all models via BASTION admin API
- Sample GPU metrics every 2s until temp returns within 3C of Phase 1 baseline
- Record: cooldown duration
- Derive `cooldown_seconds` recommendation

### Ctrl+C Handling

At any point, Ctrl+C:
1. Immediately stops the current phase
2. Jumps to Phase 5 (recovery)
3. Writes partial results (whatever was measured so far)
4. Exits cleanly — no orphaned models, no GPU left under load

### Output: GPU Profile File

Written to `~/.config/bastion/gpu-profile.yaml`:

```yaml
# Auto-generated by 'bastion stress-test'
# Date: 2026-04-23
# GPU: NVIDIA RTX 4090 (24576 MB)
# Driver: 570.86
# Status: complete (all 5 phases)

gpu:
  name: "RTX 4090"
  vram_total_mb: 24576
  driver: "570.86"

baseline:
  idle_temp_c: 41
  idle_power_w: 18
  vram_in_use_mb: 512

calibrated:
  safe_swap_rate_per_min: 4
  max_concurrent_requests: 6
  vram_headroom_mb: 6144
  thermal_ceiling_c: 82
  cooldown_seconds: 3
  swap_duration_avg_s: 2.4

tested:
  date: "2026-04-23"
  phases_completed: 5
  models_used: ["qwen3:1.7b", "llama3.2:1b"]
  duration_seconds: 487
```

### Runtime Integration

At BASTION startup, if `~/.config/bastion/gpu-profile.yaml` exists:
- Load calibrated values
- Override generic GPU profile table defaults with measured data
- Log: "Using calibrated GPU profile (tested 2026-04-23 on RTX 4090)"

If the file doesn't exist, BASTION uses the profile table from Stream 2
(auto-detected GPU -> known defaults -> conservative fallback). No degradation.

---

## Session Strategy

The three streams are independent and can be tackled across sessions:

| Session | Stream | Deliverable |
|---------|--------|-------------|
| Session 1 | Stream 1 (docs) | All public-facing documentation written and committed |
| Session 2 | Stream 2 (validator) | `bastion validate` implemented with GPU profile table |
| Session 3 | Stream 3 (calibrator) | `bastion stress-test` implemented with safety protocol |

Stream 2 and 3 could also be combined into one session since they share the GPU
profile infrastructure. Stream 1 is fully independent.

Within each session, agent teams can parallelize:
- Stream 1: one agent per document, review agent for consistency
- Stream 2: implementation agent + test agent
- Stream 3: implementation agent + test agent (reuses Stream 2 GPU profiles)

## What This Does NOT Cover

- Publishing to PyPI (separate task — name verification, CI/CD, release workflow)
- Creating the clean public repo copy (separate task — .gitignore, archive strategy)
- Blog post or development journey content
- Runtime behavior changes (this spec is docs + tooling only)
- Docker image building or Helm charts
