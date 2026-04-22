# ROADMAP -- BASTION

**Batch Affinity Scheduler for Throttled Inference on Ollama Networks**

BASTION evolved from a crash investigation into a production GPU inference broker. It sits as a transparent HTTP proxy in front of Ollama, preventing GPU crashes from concurrent model loading while providing scheduling, observability, and agent-to-agent communication.

This roadmap tracks completed work and defines future directions.

---

## Completed Work

### S1 -- Core Proxy and Scheduler

Built the foundational proxy layer and scheduler. BASTION intercepts all Ollama traffic on port 11434, injects `use_mmap: false` to prevent GPU crashes from memory-mapped model loading, and schedules model loads within a configurable VRAM budget. Includes the affinity queue with priority tiers, VRAM tracking via nvidia-smi and Ollama `/api/ps` fusion, and a full test suite.

### S2 -- CLI Hooks and TUI Dashboard

Added a Textual-based terminal dashboard for real-time monitoring of GPU state, queue depth, loaded models, and scheduler activity. Integrated CLI entry point with argparse and uvicorn launch.

### S3 -- Scheduler Intelligence

Made the scheduler residency-aware. Instead of tracking a single `_current_model`, the scheduler now queries which models are co-resident in VRAM and skips cooldown for transitions between already-loaded models. Added a residency cache with configurable TTL to avoid excessive polling.

### S4 -- Observability and Telemetry

Wired up Prometheus metrics (optional dependency), structured JSONL audit logging with rotation, and a `/broker/metrics` endpoint. Metrics cover request latency, queue wait time, model swap counts, VRAM usage, and GPU temperature. Added request-tracking middleware.

### S5 -- Dashboard Evolution

Extended the TUI dashboard with sparkline trend panels for VRAM and temperature history, alert panels with severity tiers, interactive model management (preload/unload/drain via keyboard shortcuts), a request trace viewer, and a VRAM budget visualization bar.

### S6 -- External System Integration

Defined client integration patterns for external systems to use BASTION as their GPU broker. Introduced session profiles for pre-declared model sequences, intent declaration API for scheduler optimization, and priority header mapping so different pipeline stages get appropriate scheduling priority.

### S7 -- A2A Agent Interface

Implemented the Agent-to-Agent protocol layer. BASTION publishes a discoverable agent card, accepts A2A tasks (infer, batch_infer, preload, status), manages task lifecycle with a dual-store architecture (active + compacted), supports SSE streaming, and provides model leases with hybrid eviction triggers (request count, TTL, idle timeout, fencing tokens).

### S8 -- Production Hardening

Added API key authentication for admin endpoints, per-client rate limiting, a three-state circuit breaker for Ollama backend failures, graceful degradation when Ollama is unreachable, request validation, and Kubernetes-compatible health probes (`/broker/livez`, `/broker/readyz`). Extracted all hardcoded constants into `broker.yaml`.

### S9 -- GPU Panel and VRAM Fixes

Fixed GPU dashboard panel rendering for empty state display and resolved stale VRAM ledger entries that caused queue growth. Improved error handling for GPU health queries.

### S10 -- Configuration and Systemd

Cleaned up systemd service files, added example configurations, and improved the config loader with multiple search paths.

### S11 -- Dashboard Auth Support

Added authentication support to the TUI dashboard so it can connect to auth-protected broker endpoints.

### S12 -- GPU Management in Dashboard

Added GPU fan control, auto-fan trigger on temperature thresholds, and GPU process kill functionality to the dashboard.

### S13 -- Auto-Start Integration

Implemented auto-start for Ollama and BASTION from the dashboard launcher, streamlining the startup workflow.

### S14 -- GPU Panel Stability

Fixed GPU panel empty display issues and resolved stale VRAM ledger entries causing queue growth under concurrent load.

---

## Future Work

### Multi-GPU Support (Aspirational)

Extend BASTION to manage multiple GPUs on the same machine or across a cluster:

- Per-GPU VRAM tracking and budget enforcement
- GPU-aware scheduler with independent cooldown per device
- Model placement optimizer based on VRAM capacity and access patterns
- Distributed broker protocol for multi-machine coordination
- Load balancer with affinity-aware routing
- Model migration between GPUs without dropping in-flight requests

The single-GPU path remains the well-tested default. Multi-GPU support would use a single-process model for local setups (2-4 GPUs) and a leader-follower protocol for multi-machine clusters.

### MCP (Model Context Protocol) Integration

Expose BASTION capabilities as MCP tools, allowing LLM agents that support MCP to directly interact with the broker:

- `bastion_infer` -- Submit inference requests with model and priority selection
- `bastion_status` -- Query broker state, queue depth, loaded models
- `bastion_preload` / `bastion_unload` -- Model management
- `bastion_reserve` -- Reserve a model for a sequence of requests

MCP integration would complement the existing A2A interface, providing a second standard protocol for agent-broker communication. Unlike A2A (which is designed for agent-to-agent task delegation), MCP positions BASTION as a tool provider that any MCP-capable agent can discover and invoke.

### Enhanced Observability

- Grafana dashboard template for BASTION metrics
- OpenTelemetry trace correlation across A2A task chains
- Alertmanager integration for VRAM and temperature thresholds
- Historical analytics for scheduling decisions and swap patterns

### Scheduler Improvements

- Deadline-aware scheduling for time-sensitive requests
- Fair-share policies to prevent single-client queue monopolization
- Predictive model preloading based on historical access patterns
- Dynamic priority adjustment based on system load

---

## Dependency Graph

```
S1 (Core) -> S2 (Dashboard) -> S5 (Dashboard Evolution)
S1 (Core) -> S3 (Scheduler) -> S6 (Integration) -> S7 (A2A)
S1 (Core) -> S4 (Observability) -> S5, S8
S8 (Hardening) -> Multi-GPU (future)
```

Sessions S1-S8 form the production-ready foundation. S9-S14 are incremental improvements. Multi-GPU and MCP integration are independent future tracks.
