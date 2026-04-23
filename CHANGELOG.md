# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-04-23

### Added
- `--validate` CLI flag for pre-flight system checks (Python, GPU, Ollama, config, permissions)
- `--stress-test` CLI flag for GPU stress calibration with 5-phase ramp-up
- GPU profile table (`gpu_profiles.py`) with known-safe defaults for 13 NVIDIA GPUs
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
