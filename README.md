# BASTION

![CI](https://github.com/cyprian-w/bastion/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)

**Batch Affinity Scheduler for Throttled Inference on Ollama Networks**

A system-level GPU inference broker that prevents crashes from concurrent
[Ollama](https://ollama.com) access. BASTION sits as a transparent HTTP proxy
between your applications and Ollama, serializing model loads, enforcing VRAM
budgets, and eliminating the memory-mapped I/O conditions that cause GPU driver
crashes under heavy inference workloads.

---

## The Problem

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
   enforcement, and no cooldown between model transitions. The `OLLAMA_MMAP`
   environment variable was proposed but never merged upstream (PR #6854).

The result: after roughly 60 rapid model swaps, the GPU driver hits an
unrecoverable state -- kernel OOM, display freeze, or full system crash
requiring a hard reboot.

**BASTION solves this at the system level**, sitting transparently on the
standard Ollama port so every client benefits with zero configuration changes.

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

## Key Features

- **Transparent HTTP proxy** -- drop-in replacement on port 11434; existing
  clients work without any changes. Streams NDJSON faithfully so `ollama run`
  remains responsive.

- **Affinity-based scheduling** -- per-model sub-queues drain all pending
  requests for the currently loaded model before swapping, dramatically
  reducing GPU model transitions.

- **Priority aging** -- four priority tiers (interactive, agent, pipeline,
  background) with time-based aging (`base + age_seconds * 2.0`) to prevent
  starvation of lower-priority requests.

- **VRAM budget enforcement** -- tracks GPU memory via Ollama `/api/ps` fused
  with `nvidia-smi` data. Enforces a configurable budget (e.g., 24 GB usable
  from 32 GB total) and blocks model loads that would exceed it.

- **Crash prevention** -- injects `use_mmap: false` into every Ollama API
  request, enforces cooldown periods between model transitions, and rate-limits
  model swap frequency.

- **14-panel TUI dashboard** -- real-time Textual dashboard showing GPU
  thermals, VRAM usage, queue depth, scheduler state, circuit breaker status,
  A2A tasks, audit events, and more.

- **A2A protocol support** -- implements the Agent-to-Agent protocol with
  agent card discovery, task lifecycle management, batch inference, and model
  reservation leases.

- **Prometheus metrics + OpenTelemetry tracing** -- optional observability
  integrations with graceful no-op fallbacks when dependencies are not
  installed.

- **Circuit breaker** -- three-state (closed/open/half-open) circuit breaker
  protects against cascading failures when the Ollama backend is unresponsive.

- **Per-IP rate limiting** -- configurable rate limiting middleware to prevent
  abuse.

- **Tiered audit logging** -- JSONL audit logs with content hashing and
  automatic rotation.

## Quick Start

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) installed and running
- NVIDIA GPU with `nvidia-smi` (optional -- graceful fallback without it)

### 1. Install

```bash
git clone https://github.com/cyprian-sw/bastion.git
cd bastion
pip install -e ".[dev]"
```

### 2. Move Ollama to port 11435

BASTION needs to claim port 11434 (the standard Ollama port) so existing
clients connect through it transparently.

```bash
# Using the included systemd override
sudo mkdir -p /etc/systemd/system/ollama.service.d/
sudo cp systemd/ollama-port-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Or set the environment variable manually:

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

### 3. Start BASTION

```bash
# Default configuration (single-port mode)
python -m bastion

# With custom config
python -m bastion --config config/broker.yaml

# Two-port mode (proxy on :11434, admin API on :9999)
python -m bastion --admin-port 9999

# As a systemd service
sudo cp systemd/bastion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bastion
```

### 4. Verify

```bash
# Should return "Ollama is running" (proxied through BASTION)
curl http://localhost:11434

# Check broker status
curl http://localhost:11434/broker/status

# Use Ollama normally -- everything is transparent
ollama run llama3.1:8b "Hello, world!"
```

For production deployment (systemd, Docker, desktop launcher), see the
**[Deployment Guide](docs/deployment.md)**.

## Configuration

Copy the example configuration and adjust for your hardware:

```bash
cp config/broker.example.yaml config/broker.yaml
```

See [`config/broker.example.yaml`](config/broker.example.yaml) for a
fully-commented minimal configuration. Key parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ollama.port` | `11435` | Port where Ollama is actually listening |
| `server.port` | `11434` | Port where clients connect (standard Ollama port) |
| `server.admin_port` | `0` | Separate admin port (`0` = disabled, single-port mode) |
| `gpu.total_vram_gb` | `32` | Total physical VRAM on your GPU |
| `gpu.headroom_gb` | `8` | Reserved for OS, display, CUDA overhead |
| `gpu.max_temperature_c` | `82` | Block model loads above this temperature |
| `scheduler.cooldown_seconds` | `2.0` | Mandatory pause between model transitions |
| `scheduler.aging_rate` | `2.0` | Priority points gained per second waiting |
| `request_overrides.use_mmap` | `false` | Disable memory-mapped model loading |

### Priority Tiers

Set priority via HTTP header: `X-Broker-Priority: pipeline`

| Tier | Base Priority | Typical Use Case |
|------|---------------|------------------|
| `interactive` | 100 | User-facing: `ollama run`, IDE integrations |
| `agent` | 50 | AI agent frameworks, A2A clients |
| `pipeline` | 25 | Batch extraction and ingestion pipelines |
| `background` | 10 | Overnight jobs, consolidation tasks |

### Deployment Modes

**Single-port mode** (default): All routes (`/api/*`, `/broker/*`, `/a2a/*`)
served on port 11434. Simple setup suitable for most deployments.

**Two-port mode**: Ollama proxy on port 11434, admin and A2A endpoints on a
separate port. Useful for firewall rules that expose only the proxy to LLM
clients while keeping admin endpoints on a private network. Enable with
`--admin-port <port>` or `server.admin_port` in config.

## Dashboard

BASTION includes a 14-panel TUI dashboard built with
[Textual](https://textual.textualize.io/) for real-time monitoring:

```bash
python -m bastion.dashboard

# Custom endpoint and poll interval
python -m bastion.dashboard --url http://localhost:11434 --interval 2.0

# For two-port mode, point to the admin port
python -m bastion.dashboard --admin-url http://localhost:9999
```

The dashboard displays:

- GPU temperature, VRAM usage, and power draw with sparkline graphs
- Currently loaded models and active model name
- Queue depth per model with trend visualization
- Scheduler state, uptime, total requests served, and model swap count
- Circuit breaker status (closed/open/half-open)
- A2A task lifecycle and lease status
- VRAM budget bar with per-model breakdown
- Severity-tiered alerts (VRAM pressure, temperature, queue depth)
- Recent request trace viewer (last 20 requests)

**Keyboard shortcuts:** `p` preload model, `u` unload model, `d` toggle drain
mode, `s` restart service, `r` manual refresh, `h` help overlay, `q` quit.

## API Reference

Full API documentation is available in [`docs/api.md`](docs/api.md). Key
endpoints:

### Broker Admin API (`/broker/*`)

```bash
# Broker status (queue, scheduler, GPU state)
curl http://localhost:11434/broker/status

# Preload a model into VRAM
curl -X POST http://localhost:11434/broker/preload \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b"}'

# Unload a model from VRAM
curl -X POST http://localhost:11434/broker/unload \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b"}'

# Drain mode (stop accepting new requests)
curl -X POST http://localhost:11434/broker/drain
```

### A2A Agent Interface (`/a2a/*`)

BASTION implements the [A2A (Agent-to-Agent)](https://google.github.io/A2A/)
protocol, allowing agents to discover and use it for GPU inference.

```bash
# Discover BASTION's agent card
curl http://localhost:11434/.well-known/agent-card.json

# Submit an inference task
curl -X POST http://localhost:11434/a2a/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "skill_id": "infer",
    "params": {
      "model": "llama3.1:8b",
      "prompt": "Explain quantum entanglement briefly."
    }
  }'

# Batch inference (multiple prompts, single model load)
curl -X POST http://localhost:11434/a2a/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "skill_id": "batch_infer",
    "params": {
      "model": "llama3.1:8b",
      "prompts": ["What is gravity?", "What is light?"]
    }
  }'

# Stream task updates (Server-Sent Events)
curl -N http://localhost:11434/a2a/tasks/<task_id>/stream
```

| Skill | Description |
|-------|-------------|
| `infer` | Single-prompt inference through the scheduler pipeline |
| `batch_infer` | N prompts with single-model-load guarantee |
| `preload` | Reserve a model in VRAM to prevent eviction |
| `status` | Current broker and queue state |

## Technical Deep Dive: Crash Prevention

This section describes the core engineering problem BASTION was built to solve
and the layered approach used to prevent GPU crashes under concurrent LLM
inference workloads.

### The Failure Mode

When Ollama loads a model, it memory-maps the model file from disk into virtual
memory. This is efficient for single-model use but creates a dangerous
condition under concurrent access:

1. **Virtual memory inflation.** A 14 GB model file mapped with `mmap()`
   consumes 14 GB of virtual address space. With multiple models loaded, the
   total virtual footprint can exceed physical VRAM by 3-4x.

2. **Page cache contention.** The OS page cache and GPU driver compete for the
   same physical pages. Under pressure, the kernel evicts GPU-resident pages to
   make room for file-backed pages, triggering GPU page faults.

3. **Swap storms.** When the kernel runs out of physical memory to satisfy both
   the page cache and GPU allocations, it begins swapping aggressively. GPU
   operations stall waiting for pages to be faulted back in, and the system
   enters a death spiral.

4. **Driver crash.** After approximately 60 rapid model load/unload cycles in
   under 10 minutes, the GPU driver hits an unrecoverable state. The display
   freezes, and the system requires a hard reboot.

### BASTION's Layered Solution

BASTION applies four complementary strategies:

**1. Memory-map injection (`use_mmap: false`)**

Every Ollama API request passing through BASTION has `options.use_mmap` set to
`false`, forcing Ollama to use regular memory allocation instead of `mmap()`.
This eliminates virtual memory inflation and page cache contention entirely.
The upstream `OLLAMA_MMAP` environment variable was proposed (Ollama PR #6854)
but never merged, so per-request injection is the only reliable control.

**2. Serialized scheduling with affinity**

The affinity queue groups pending requests by model name. When the scheduler
picks work, it drains all requests for the currently loaded model before
considering a swap. This reduces total model transitions from potentially
dozens per minute to only what is strictly necessary.

**3. VRAM budget enforcement**

A VRAM tracker fuses data from Ollama's `/api/ps` endpoint with `nvidia-smi`
to maintain an accurate picture of GPU memory usage. Before loading a model,
the scheduler checks whether the load would exceed the configured budget
(total VRAM minus headroom). If it would, the request waits until VRAM is
freed.

**4. Cooldown and rate limiting**

A mandatory 2-second cooldown between model transitions prevents the rapid
allocation/deallocation cycling that triggers driver instability. A swap rate
limiter provides an additional safety net by capping the maximum number of
model swaps per time window.

### Why Not Just Use `OLLAMA_MAX_LOADED_MODELS=1`?

Setting `OLLAMA_MAX_LOADED_MODELS=1` forces Ollama to unload the current model
before loading a new one. Under concurrent access from multiple clients
requesting different models, this creates the worst possible pattern: every
request triggers a full unload/load cycle. In testing, this produced 175 model
swaps in 7 minutes, crashing at swap ~60.

BASTION takes the opposite approach: it allows multiple co-resident models (up
to 3 by default) within the VRAM budget, and uses affinity scheduling to
minimize transitions. The result is dramatically fewer swaps with no risk of
exceeding GPU memory.

## Project Structure

```
src/bastion/
  __init__.py          Package init and version
  __main__.py          CLI entry point (argparse + uvicorn launcher)
  server.py            FastAPI app factory, admin routes, proxy catch-all
  proxy.py             Transparent HTTP proxy (NDJSON streaming, mmap injection)
  queue.py             AffinityQueue (per-model sub-queues, priority aging)
  scheduler.py         Scheduling loop (model swaps, cooldown, GPU health gating)
  vram.py              VRAM tracker (Ollama /api/ps + nvidia-smi fusion)
  health.py            GPU status queries (nvidia-smi, 5s timeout, fallback)
  a2a.py               A2A protocol handler (tasks, skills, leases, SSE streaming)
  taskstore.py         In-memory A2A task store (compaction, TTL, backpressure)
  models.py            Pydantic v2 models (config, queue, GPU state, A2A types)
  config.py            YAML config loader with search path resolution
  auth.py              API key + bearer token auth dependencies
  audit.py             Tiered audit logging (JSONL, content hashing, rotation)
  circuitbreaker.py    Three-state circuit breaker (closed/open/half-open)
  dashboard.py         Textual TUI dashboard (14 panels, real-time monitoring)
  metrics.py           Prometheus metrics (no-op stubs when client not installed)
  middleware.py        FastAPI middleware (request metrics recording)
  ratelimit.py         Per-IP rate limiting middleware
  telemetry.py         OpenTelemetry tracing (no-op stubs when SDK not installed)
  watchdog.py          Ollama process monitor + systemd sd_notify integration

config/
  broker.yaml          Default configuration (all sections documented)
  broker.example.yaml  Minimal safe defaults for new deployments

systemd/               Service files (bastion.service, ollama port override)
tests/                 Pytest test suite
docs/                  API and architecture documentation
```

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run the full test suite
python -m pytest tests/ -v

# Run only fast unit tests (skip integration and e2e)
python -m pytest tests/ -v -m "not slow and not e2e"

# Lint
ruff check src/ tests/
```

## Optional Extras

BASTION's core has minimal dependencies. Optional features are installed via
extras:

```bash
pip install -e ".[metrics]"     # Prometheus metrics export
pip install -e ".[dashboard]"   # TUI dashboard (Textual)
pip install -e ".[a2a]"         # A2A SDK types
pip install -e ".[dev]"         # Testing and linting tools
```

## Roadmap

- **MCP integration** -- Model Context Protocol tool server for direct IDE
  integration with queue-aware inference.
- **Multi-GPU scheduling** -- extend the scheduler to distribute models across
  multiple GPUs with per-device VRAM tracking.
- **Persistent task store** -- optional durable backend for A2A task state
  across restarts (currently in-memory only).
- **Web dashboard** -- browser-based alternative to the TUI dashboard with
  historical metrics and charting.

## License

MIT License. See [LICENSE](LICENSE) for details.

**Author:** Cyprian Winogradow
