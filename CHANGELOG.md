# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `GET /broker/latency` — per-model latency percentiles (p50/p95/p99 for end-to-end duration and queue-wait) over a rolling window. Query param `window_s` (default 300, clamped `[10, 3600]`). Aggregation logic factored into `bastion.latency_aggregator.aggregate_latency` and unit-tested independently. Models with fewer than 3 samples in the window are omitted from `per_model`; the `overall` bucket aggregates all in-window samples.
- `GET /broker/catalog` — registered models from `broker.yaml` enriched with VRAMTracker residency state and a computed `is_evictable` flag (loaded AND not the scheduler's `current_model` AND not `always_allowed`). Stays queryable during `/api/ps` outages — `loaded_count` collapses to 0 rather than 500ing.
- `BastionClient.get_latency(window_s)` / `BastionClient.get_catalog()` async wrappers in the dashboard client.
- `BrokerConfig._loaded_from` (`PrivateAttr`) + public `loaded_from` property recording the resolved path of the loaded `broker.yaml`; surfaced as `registry_source` in `/broker/catalog`.

### Changed
- `_recent_requests` ring buffer maxlen bumped from 50 → 500. Prereq for stable per-model p95 in `/broker/latency`. Memory overhead ≈ 50 KB.
- `/broker/recent` documentation updated to reflect the 500-sample buffer and its new role feeding the latency aggregator.
- **M58 complexity routing no longer force-routes over an explicit client model.** New `complexity_routing.override_explicit` flag (default `false`): the route model only fills in for requests that omit `model`; an explicit `model` in the request body wins. Skipped routes are recorded with reason `complexity-<level>-skipped-explicit-model` in response headers and the audit log. Set `override_explicit: true` to restore the original force-route behavior. Root cause of the 2026-06-10 SWARM_BRAIN overnight-run incident (explicit instruct model silently replaced by a thinking-capable route target).
- `request_complete` audit events now include `routing_reason`, and `routing_applied` is `true` only when the model was actually changed.

### Fixed
- Thrashing **warn** verdict on a request without complexity routing no longer breaks the request: `routing_meta` carrying only `_thrashing_warn` raised `KeyError` in response-header construction and audit emission (surfaced as a proxy error instead of the advisory `X-Swap-Penalty-Warning` header).

## [0.4.0] - 2026-04-23

### Added
- `--validate` CLI flag for pre-flight system checks (Python, GPU, Ollama, config, permissions)
- `--stress-test` CLI flag for GPU stress calibration with 5-phase ramp-up
- GPU profile table (`gpu_profiles.py`) with known-safe defaults for 13 named NVIDIA GPUs + a conservative fallback
- Calibrated GPU profile loading at startup (`gpu-profile.yaml`)
- Documentation suite: getting-started, hardware guide, configuration reference, troubleshooting, operations, security

### Changed
- README rewritten for public release (prerequisites, quickstart, documentation table)
- CHANGELOG cleaned of internal session tags
- Crash prevention guide rewritten as technical reference (removed investigation narrative)
- Internal development artifacts archived to `_archive/`
- VRAM budget in e2e stress tests raised from 26 GB to 28 GB (4 GB headroom on 32 GB GPU)

### Fixed
- E2e stress tests failing due to VRAM state leaking between tests (added cleanup)

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
