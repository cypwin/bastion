# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Scheduler re-checks the GPU-hot gate immediately before swap dispatch (releasing the VRAM reservation on abort) — a GPU transitioning hot during the swap window is no longer unprotected.
- Upstream Ollama 5xx is forwarded with its real status in both streaming and non-streaming proxy paths (a streamed 500 was previously masked as 200), connect failures map to 502 without leaking scheduler slots, and upstream ≥500 now counts toward the circuit breaker instead of recording success.
- `CircuitBreakerTransport` counts all `httpx.TransportError` subclasses toward the breaker (previously only `ConnectError`/`ConnectTimeout`/`ReadTimeout`; `RemoteProtocolError`, `PoolTimeout`, `WriteError` etc. bypassed it).
- Queue sweeps are rejections, not grants: a request swept as stale now gets 504 from the proxy instead of being forwarded to Ollama as if the scheduler had granted it.

## [0.4.1] - 2026-06-12

### Added
- `GET /broker/latency` — per-model latency percentiles (p50/p95/p99 for end-to-end duration and queue-wait) over a rolling window. Query param `window_s` (default 300, clamped `[10, 3600]`). Aggregation logic factored into `bastion.latency_aggregator.aggregate_latency` and unit-tested independently. Models with fewer than 3 samples in the window are omitted from `per_model`; the `overall` bucket aggregates all in-window samples.
- `GET /broker/catalog` — registered models from `broker.yaml` enriched with VRAMTracker residency state and a computed `is_evictable` flag (loaded AND not the scheduler's `current_model` AND not `always_allowed`). Stays queryable during `/api/ps` outages — `residency_state` flips to `"unknown"` and `loaded_count` collapses to 0 rather than 500ing.
- `GET /broker/version` — stable build identity (`version`, `git_sha`, `boot_time_unix`, `boot_time_iso`) so A2A clients can pin the SHA at batch start and detect mid-batch redeploys/restarts.
- `BastionClient.get_latency(window_s)` / `BastionClient.get_catalog()` async wrappers in the dashboard client.
- `BrokerConfig._loaded_from` (`PrivateAttr`) + public `loaded_from` property recording the resolved path of the loaded `broker.yaml`; surfaced as `registry_source` in `/broker/catalog` (home directory redacted to `~`).
- State-unknown indicators: `vram_state` on `/broker/status` and the A2A `status` skill, `residency_state` on `/broker/catalog` — `"unknown"` marks loaded-model lists as placeholders during Ollama outages, distinguishable from verified-empty.
- `streaming` flag on `/broker/recent` samples.

### Changed
- **VRAM state-unknown sentinel (fail-closed admission).** `VRAMTracker.get_loaded_models()` now returns `None` when `/api/ps` is unreachable instead of an empty list. `can_load_model()` and `POST /broker/preload` refuse loads during the outage ("Cannot determine VRAM state…"), the scheduler tick bails out instead of dispatching on missing residency data, and eviction refuses to unload on unknown state — including stopping mid-loop if state becomes unknown between unloads.
- **Proxy enqueue error contract:** unexpected enqueue exceptions now return `500 "Internal broker error"` instead of `503 "Broker queue full"`; 503 is reserved for genuine queue-full backpressure. Client retry logic should branch on the status code.
- Scheduler unload gate: failed/deferred unloads no longer count as eviction progress (`_unload_model` → bool).
- **Latency samples are recorded at true completion with real status codes.** Streaming requests record after the last byte (durations now reflect full stream time instead of ~0), and `status_code` carries the actual outcome (upstream errors, 502 backend failures) — `error_rate` in `/broker/latency` is meaningful instead of structurally zero.
- Registry name matching is tag-aware (`name` ≡ `name:latest`) in reconcile import exclusion, `can_load_model` accounting, eviction filters, and catalog residency — prevents an `always_allowed` model resident under a tagged name from being imported into the budget or evicted.
- `ResidencyCache` stale-OK preservation is bounded (default 30 s grace): after that, consecutive `/api/ps` failures surface state-unknown instead of serving an arbitrarily old residency picture.
- **`ResidencyCache` debounces residency declassification (flicker hold).** Ollama's `/api/ps` returns partial views under concurrent inference — a busy, resident model can be missing from 1–2 consecutive polls while serving warm requests. A previously-resident model now stays classified resident until missing from 2 consecutive successful refreshes, eliminating phantom scheduler swaps (`total_model_swaps` counting swaps from a model to itself), spurious 2 s cooldowns that serialized concurrent dispatch, thrashing-detector noise, and reconcile ledger churn. BASTION-initiated unloads bypass the hold (`invalidate()` makes the next read authoritative).
- nvidia-smi reserve() backstop logs a warning when it fails open (no reading) instead of being silent.
- `_detect_git_sha` only trusts `git rev-parse` when the package root itself is a checkout (prevents reporting an unrelated enclosing repo's SHA) and debug-logs failures instead of swallowing them.
- `_recent_requests` ring buffer maxlen bumped from 50 → 500. Prereq for stable per-model p95 in `/broker/latency`. Memory overhead ≈ 50 KB.
- `/broker/recent` documentation updated to reflect the 500-sample buffer and its new role feeding the latency aggregator.
- **M58 complexity routing no longer force-routes over an explicit client model.** New `complexity_routing.override_explicit` flag (default `false`): the route model only fills in for requests that omit `model`; an explicit `model` in the request body wins. Skipped routes are recorded with reason `complexity-<level>-skipped-explicit-model` in response headers and the audit log. Set `override_explicit: true` to restore the original force-route behavior. Root cause of a 2026-06-10 overnight-run incident (explicit instruct model silently replaced by a thinking-capable route target).
- `request_complete` audit events now include `routing_reason`, and `routing_applied` is `true` only when the model was actually changed.

- **Dashboard auto-fan trigger is now a four-band escalation curve with a GPU-safe floor** (was a single 80 °C → 90 % trigger with 70 °C reset). The CPU temperature engages and releases the override: 60 °C → 30 %, 70 °C → 50 %, 80 °C → 90 %, over 85 °C → 100 %, back to BIOS auto below the curve; escalation is immediate, de-escalation applies 5 °C hysteresis per band. Because a manual override suspends the GPU's own VBIOS fan curve (`GPUFanControlState=1`), the **GPU temperature acts as a floor while the override is active** — the applied duty is never below what the GPU's band demands, so a CPU-derived 30 % can no longer undercool a hot GPU. Releasing to auto hands control straight back to the firmware curve. The fan modal shows the curve and the currently applied band.
- Dashboard Alerts panel now shows the raise time (HH:MM:SS) per alert.
- **Request source attribution.** `/broker/recent` samples and the dashboard Request Trace gain a `source` field/column: the client's declared `X-Agent-ID` header when present, else the User-Agent product token (`ollama/0.5.1` → `ollama`), else `-`. Declared identity only — no process-level sniffing.
- E2e stress suite (`tests/test_e2e_stress.py`) is now opt-in via `BASTION_E2E=1`. The suite evicts every loaded model from the broker it targets; the gate makes a plain `pytest tests/` incapable of hitting a production instance by accident.

### Fixed
- Thrashing **warn** verdict on a request without complexity routing no longer breaks the request: `routing_meta` carrying only `_thrashing_warn` raised `KeyError` in response-header construction and audit emission (surfaced as a proxy error instead of the advisory `X-Swap-Penalty-Warning` header).
- A2A `status` skill no longer fails with a swallowed `TypeError` when VRAM state is unknown (Ollama outage) — it now answers with an empty list and `vram_state: "unknown"`.
- `/broker/latency` clamps the reported window at 0 when a sample carries a future timestamp (backwards wall-clock step) instead of failing response validation.

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
