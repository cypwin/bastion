# BASTION

![CI](https://github.com/cypwin/bastion/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)

**Batch Affinity Scheduler for Throttled Inference on Ollama Networks**

A system-level GPU inference broker that prevents crashes from concurrent
[Ollama](https://ollama.com) access. BASTION sits as a transparent HTTP proxy
between your applications and Ollama, serializing model loads, enforcing VRAM
budgets, and eliminating the memory-mapped I/O conditions that cause GPU driver
crashes under heavy inference workloads.

---

## Why BASTION?

Running multiple LLM clients against a single Ollama instance is a recipe for
hard crashes. Here is why:

1. **Memory-mapped model loading.** Ollama memory-maps model files by default.
   When multiple models are loaded concurrently, the virtual address space
   balloons far beyond physical VRAM, and the OS page cache competes with GPU
   memory for the same pages.

2. **Rapid model cycling.** With `OLLAMA_MAX_LOADED_MODELS=1`, every request
   for a different model triggers a full unload/load cycle. Under concurrent
   access from multiple clients, this creates dozens of VRAM
   allocation/deallocation cycles per minute.

3. **No upstream protection.** Ollama has no built-in queue, no VRAM budget
   enforcement, and no cooldown between model transitions.

The result: after roughly 60 rapid model swaps, the GPU driver hits an
unrecoverable state -- kernel OOM, display freeze, or full system crash
requiring a hard reboot.

**BASTION solves this at the system level**, sitting transparently on the
standard Ollama port so every client benefits with zero configuration changes.

## Prerequisites

- Python 3.11+
- Linux with NVIDIA GPU and working drivers (`nvidia-smi` must respond)
- [Ollama](https://ollama.com) installed
- At least one model pulled (`ollama pull llama3.1:8b`)

## Quick Start

### 1. Install

```bash
git clone https://github.com/cypwin/bastion.git
cd bastion
pip install -e ".[dev]"
```

### 2. Move Ollama to port 11435

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d/
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null << 'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11435"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Or manually: `OLLAMA_HOST=127.0.0.1:11435 ollama serve`

### 3. Configure

```bash
bastion --init-config       # Generate ~/.config/bastion/broker.yaml
bastion --detect-models     # Discover installed models
```

### 4. Validate your setup

```bash
bastion --validate
```

### 5. Start BASTION

```bash
bastion                              # Default config
bastion --config config/broker.yaml  # Custom config
bastion --admin-port 9999            # Two-port mode
```

### 6. Verify

```bash
curl http://localhost:11434                # "Ollama is running"
curl http://localhost:11434/broker/status  # Broker status
ollama run llama3.1:8b "Hello, world!"    # Transparent proxy
```

## Key Features

- **Transparent HTTP proxy** -- drop-in replacement on port 11434; existing
  clients work without any changes. Streams NDJSON faithfully so `ollama run`
  remains responsive.

- **Affinity-based scheduling** -- per-model sub-queues drain all pending
  requests for the currently loaded model before swapping, dramatically
  reducing GPU model transitions.

- **Priority aging** -- four priority tiers (interactive, agent, pipeline,
  background) with time-based aging to prevent starvation of lower-priority
  requests.

- **VRAM budget enforcement** -- tracks GPU memory via Ollama `/api/ps` fused
  with `nvidia-smi` data. Enforces a configurable budget and blocks model
  loads that would exceed it.

- **Crash prevention** -- BASTION injects `use_mmap: false` into scheduled
  inference requests (`/api/generate`, `/api/chat`, `/api/embed`) when the
  client hasn't specified `use_mmap` explicitly. This mitigates PCIe power
  transients that crashed RTX 5090 during mmap-backed model cycling.
  Passthrough endpoints (`/api/pull`, `/api/show`, `/api/tags`, etc.) forward
  unchanged. Cooldown periods between model transitions and swap-rate limiting
  provide additional protection.

- **19-panel TUI dashboard** -- real-time Textual dashboard showing GPU
  thermals, VRAM usage, queue depth, scheduler state, A2A tasks, leases, audit
  events, and more.

- **A2A protocol support** -- Agent-to-Agent protocol with agent card
  discovery, task lifecycle, batch inference, and model reservation leases.

- **Prometheus metrics + OpenTelemetry tracing** -- optional observability
  integrations with graceful no-op fallbacks.

- **Circuit breaker** -- three-state (closed/open/half-open) protection
  against cascading failures when the Ollama backend is unresponsive.

- **Per-IP rate limiting** -- configurable rate limiting middleware.

- **Tiered audit logging** -- JSONL audit logs with content hashing and
  automatic rotation.

## Architecture

```
                          Clients
    (ollama run, Claude Code, Python scripts, A2A agents, curl)
                             |
                             | :11434 (standard Ollama port)
                             v
    +------------------------------------------------------------+
    |                        BASTION                              |
    |                                                             |
    |  +----------------+  +-----------------+  +--------------+  |
    |  | Ollama Proxy   |  | Admin API       |  | A2A Agent    |  |
    |  | /api/*         |  | /broker/*       |  | /a2a/*       |  |
    |  |                |  |                 |  |              |  |
    |  | - use_mmap     |  | - status/queue  |  | - tasks      |  |
    |  |   injection    |  | - health/vram   |  | - streaming  |  |
    |  | - NDJSON       |  | - preload       |  | - leases     |  |
    |  |   streaming    |  | - unload/drain  |  | - agent card |  |
    |  | - priority     |  | - metrics       |  | - batch      |  |
    |  |   detection    |  |                 |  |   inference  |  |
    |  +-------+--------+  +-----------------+  +--------------+  |
    |          |                                                   |
    |  +-------v-------------------------------------------------+ |
    |  |          Affinity Queue + Scheduler                      | |
    |  |                                                          | |
    |  |  - Per-model sub-queues (minimize GPU model swaps)       | |
    |  |  - Priority tiers: INTERACTIVE > AGENT > PIPELINE > BG  | |
    |  |  - Aging: effective = base + (age_seconds * 2.0)        | |
    |  |  - Cooldown: 2s between model transitions               | |
    |  |  - VRAM ledger (assume/confirm/forget pattern)           | |
    |  |  - GPU health gating (temp, power, utilization)          | |
    |  |  - Concurrent co-resident dispatch (up to 3 models)     | |
    |  +-------+-------------------------------------------------+ |
    +-----------|---------------------------------------------------+
                | :11435
    +-----------v---------------------------------------------------+
    |                     Ollama (backend)                           |
    |              OLLAMA_HOST=127.0.0.1:11435                      |
    +---------------------------------------------------------------+
```

## Dashboard

```bash
bastion-dashboard
bastion-dashboard --url http://localhost:11434 --interval 2.0
```

(Or: `python -m bastion.dashboard` / `python -m bastion.dashboard --url http://localhost:11434 --interval 2.0`)

**Keyboard shortcuts:** `p` preload model, `u` unload model, `d` toggle drain
mode, `r` manual refresh, `h` help overlay, `q` quit.

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
| [Releasing](docs/releasing.md) | One-time PyPI/OIDC setup and release cut procedure |

## Optional Extras

```bash
pip install -e ".[metrics]"     # Prometheus metrics export
pip install -e ".[dashboard]"   # TUI dashboard (Textual)
pip install -e ".[a2a]"         # A2A SDK types
pip install -e ".[dev]"         # Testing and linting tools
```

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## License

MIT License. See [LICENSE](LICENSE) for details.
