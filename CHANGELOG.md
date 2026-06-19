# Changelog

All notable changes to BASTION are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **`GPUBackend.query_processes()` is now `async`** (breaking protocol change). It was synchronous (`subprocess.run`); it now uses `asyncio.create_subprocess_exec` like `query_status` so it never blocks the asyncio event loop for up to 5s when polled from the machine-snapshot loop. Custom `GPUBackend` implementations must update the signature to `async def query_processes(self) -> list[dict[str, str]]`. A new async `query_process_utilization()` (per-PID `nvidia-smi pmon` sm/mem/enc/dec util) is added to the protocol; `StubBackend` returns `[]`. The Textual `SystemDataCollector.query_gpu_processes()` wrapper keeps its synchronous call contract by driving the coroutine to completion.
- `DELETE /a2a/tasks/{id}` on an already-terminal task now returns **409 Conflict** instead of 404 — 404 is reserved for tasks that never existed (or whose state was evicted). Client retry logic can now distinguish the two.
- A2A invalid live-task state transitions are logged at WARNING (was DEBUG); the already-compacted case stays at DEBUG.
- `audit.emit()` before `init_audit_logger()` buffers events in a bounded ring (256) with a WARNING and flushes them on init, instead of silently dropping them.

### Added

#### Inference-Correlated Observatory (observability expansion, Phases 1–4)

Turns BASTION's single-chokepoint vantage point (proxy + scheduler + the only process that sees GPU, host, and token stream at once) into a correlated observatory. Design: `docs/design/specs/2026-06-19-observability-expansion.md` (rev. 3, hardware/model-portable). All signals degrade gracefully (`None`/`[]`) on non-NVIDIA / no-GPU / no-PSI / no-RAPL hosts; per-process data stays on TUI+JSON surfaces only (never a Prometheus label).

- **Unified `MachineSnapshot` data model** (`models.py`) — one canonical Pydantic v2 snapshot per tick joining broker, GPU (fast + slow path), host contention, per-process attribution, stream-tapped inference throughput, and correlation-engine outputs. All new fields are `Optional`/`None`-default (backward-compatible).
- **`GET /broker/snapshot`** (`?history=N` capped at 60, `?include_ring=true`) plus a monotonic-anchored `_machine_snapshot_loop` (fast 2 s / slow 10–30 s two-cadence collection). Registered in **both** the public and admin app factories.
- **GPU device signals via the `GPUBackend` protocol seam** — `NvidiaBackend.query_status()` extended to ~16 nvidia-smi fields populating 11 new `GPUStatus` signals (compute/memory utilization, SM/gr/mem clocks, fan-speed read, GDDR memory-junction temp, PCIe gen/width + computed `pcie_downgraded`), plus new async slow-path methods (`query_throttle_reasons`, `query_xid_errors` with bounded rising-edge dedup, `query_pcie_throughput`, `query_process_utilization` pmon). `StubBackend` returns the empty contract for every one.
- **Host contention collectors** — PSI (`/proc/pressure/*`), swap in/out rate, block-device util/await via a portable base-device regex (`nvme*`/`sd*`/`vd*`/`mmcblk*`), CPU package power via RAPL (Intel **and** AMD probe order), and OOM-kill counter/rate. Exposed via **`GET /broker/contention`** and `ContentionPanel`.
- **`GET /broker/gpu/extended`** — slow-path GPU signals (throttle reasons, PCIe tx/rx, recent Xid events) on a separate endpoint to keep the 2 s fast path free of 30 s-stale data.
- **Always-on process attribution** — own-PID registry (bastion/ollama roles), top-N by CPU/IO, watchlist, churn events, and per-PID GPU utilization (pmon). Exposed via **`GET /broker/processes`** and `ProcessAttributionPanel` (GPU section shows `(no GPU)` on StubBackend). No Prometheus labels by rule.
- **Inference stream tap** (`inference_tap.py`, `InferenceTapCollector`) — non-buffering O(1) tap on the streaming proxy path capturing TTFT, prefill/decode tokens-per-sec (ns→s with cache-hit divide-by-zero guard), and context-window utilization, for any model Ollama runs. `record_recent_request()` gained six `None`-default keyword params (existing callers unaffected).
- **Correlation engine** (`correlation.py`) — an in-memory, bounded, purely passive engine embedded in the snapshot loop (zero new background tasks/IO): a 512-entry monotonic `CorrelationRing`, additive stall-reason enrichment, a `ContentionEventDetector` (coincidence join — a host spike counts only when it coincides with an inference stall, with 2-tick hysteresis), a composite forward-looking `RiskIndex` (5 weighted, independently-degrading components), and CPU↔GPU `ThermalCoupling` derived from the shared fan curve. Exposed via **`GET /broker/correlation/risk`** and **`/broker/correlation/contentions`** + `CorrelationPanel`.
- **New Prometheus metrics** (`metrics.py`, all bounded-label or label-less) — `bastion_vram_reconcile_stale_total`, `bastion_vram_reconcile_import_total`, `bastion_vram_ledger_drift_mb{gpu_index}`, `bastion_risk_index`, `bastion_risk_dominant_factor_total{factor}`, `bastion_contention_events_total{kind}`, `bastion_thermal_coupling_active`, `bastion_thermal_headroom_celsius`. Three previously-dead metrics are now emitted: `bastion_gpu_temperature_celsius` (fast tick, skipped not zeroed on `None`), `bastion_cooldown_waits_total`, `bastion_model_swap_duration_seconds{model}` (both swap branches).
- **Audit cursor API** — `audit.get_events_since(cursor)` public accessor + monotonic `_event_seq` (replaces a private-deque reach-in; stable across ring wraps), consumed by the correlation engine.
- **SSE `/broker/snapshot/stream`** — external `StreamingResponse` push surface, shipped 501-disabled behind a config flag with an 8-client cap; dedupes the pre-existing `_sse_wrapper` debt. Supersedes the never-built `/broker/status/stream`. The TUI keeps polling.
- **CI cardinality lint** (`scripts/check_metric_cardinality.py`) — AST-parses `metrics.py` and fails on any `labelnames` outside the permitted-set `{model, resource, device, op, reason, kind, factor, xid_code, gpu_index}` (+ legacy bounded labels). Validates label **names**, never **values** (a `device="sda"` series is as valid as `device="nvme0n1"`).
- **Governance docs** — ADR-005-B (records MCP `broker_snapshot_v1` as ADR-005 gating event #1 and the deferral of the subscriber/pub-sub bus; MCP blocked on `mcp_adapter` v0.5 per ADR-007), an ADR-009 addendum (observability expansion as the TUI-instrumentation baseline reference), a metric-freeze proposal (freeze the new names + label sets at v0.6), and a Grafana panel catalogue (intent only, gated on the Vision C base dashboard / `dashboards/grafana/` dir — no JSON authored).
- `ThrashingDetectionConfig` docstring portability fix — "RTX 5090 crash data" replaced with consumer-GPU-forensics framing and server-GPU (A100/H100) tuning guidance.

#### Other

- `A2AHandler.try_create_lease(model, ...)` — atomic check-and-create for single-grant-per-model lease semantics (closes the `has_active_lease` → `create_lease` TOCTOU window). `create_lease` remains unconditional for multi-lease use.
- Dashboard `BastionClient` logs failed admin-API GETs at DEBUG with endpoint and exception type (previously swallowed silently; empty panels were indistinguishable from outages).

### Fixed
- `TaskStore.create` enforces its asyncio-single-loop contract: calls from a foreign thread raise `RuntimeError` instead of silently racing on the unlocked active store.
- `VRAMManager.reconcile()` and `status()` reclaim expired reservations inside the ledger lock (same discipline as `reserve()`); `status()` is now async.
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
