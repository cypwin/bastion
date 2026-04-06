# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-04-06

### Added
- `bastion.paths` module for platform-aware directory resolution (XDG on Linux)
- `BASTION_DATA_DIR` / `BASTION_CONFIG_DIR` environment variable overrides
- GPU auto-detection via nvidia-smi (VRAM, TDP) when `total_vram_gb: 0` in config
- `--init-config` CLI flag to generate a starter config file
- `--detect-models` CLI flag to discover installed Ollama models and generate YAML config
- `bastion.discovery` module for model discovery with user guidance
- `bastion.gpu` package — GPU backend abstraction with pluggable providers
  - `GPUBackend` protocol, `NvidiaBackend` (nvidia-smi), `StubBackend` (no-op)
  - Auto-detection via `detect_backend()` factory
- 11 `BASTION_*` environment variable overrides for Docker/CI configuration
- Graceful degradation for fan control (hidden when prerequisites absent)
- `fan_control_available()` helper for checking fan control prerequisites
- Python 3.13 to CI test matrix
- CHANGELOG.md
- Changelog and Bug Tracker URLs in package metadata

### Changed
- Default `total_vram_gb` from 32 (RTX 5090-specific) to 0 (auto-detect)
- Default `max_power_watts` from 450 to 300 (conservative; auto-detect overrides)
- Default `max_temperature_c` from 82 to 83
- Config search path: `/etc/bastion/` only included on Linux (`sys.platform` guard)
- Audit log path: from hardcoded `/tmp/bastion-audit.jsonl` to `~/.local/share/bastion/`
- VRAM journal path: from hardcoded `/tmp/bastion-vram-journal.jsonl` to `~/.local/share/bastion/`
- `health.py` delegates to `gpu.get_backend()` instead of calling nvidia-smi directly
- `watchdog.py` GPU check uses GPU backend abstraction
- `dashboard/collectors.py` GPU process query uses GPU backend
- Example config ships with empty `models: {}` and guidance comments
- Startup messages improved: Ollama not running, no models, nvidia-smi missing
- Version bumped to 0.3.0

### Fixed
- Fan wrapper path resolution (`FAN_WRAPPER_PATH`) after dashboard package refactor

## [0.2.0] - 2026-03-31

### Added
- Textual TUI dashboard with 14 panels, GPU/queue/A2A views, sparklines
- GPU fan control with auto-trigger on temperature thresholds
- GPU process kill functionality in dashboard
- Auto-start integration for Ollama and BASTION from dashboard launcher
- Interactive model management (preload/unload/drain via keyboard shortcuts)
- Request trace viewer and VRAM budget visualization bar
- Dashboard auth support for protected broker endpoints

### Fixed
- GPU panel empty display issues
- Stale VRAM ledger entries causing queue growth under concurrent load

## [0.1.0] - 2026-03-15

### Added
- Transparent Ollama proxy with `use_mmap: false` injection
- Affinity queue with per-model sub-queues and priority tiers
- VRAM tracking via nvidia-smi and Ollama `/api/ps` fusion
- Scheduler with cooldown enforcement and residency-aware transitions
- Admin API (`/broker/*`) for status, queue view, preload/unload, health
- A2A agent interface (`/a2a/*`) with task lifecycle and model leases
- Prometheus metrics and OpenTelemetry tracing (optional dependencies)
- Structured JSONL audit logging with tiered verbosity
- API key + bearer token authentication
- Per-IP token-bucket rate limiting
- Three-state circuit breaker for Ollama backend failures
- Kubernetes-compatible health probes (`/broker/livez`, `/broker/readyz`)
- systemd service files and watchdog integration
- Full test suite (870+ tests)
