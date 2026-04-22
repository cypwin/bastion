# Config & Integration Analysis -- BASTION

**Analyst**: Config & Integration Analyst
**Generated**: 2026-03-13
**Scope**: Configuration completeness, client-server parity, systemd integration, deployment patterns, environment variables, config validation, hot-reload feasibility, and integration with external tooling
**Builds on**: scout-code-cartography.md, scout-data-models.md

---

## Executive Summary

BASTION's configuration layer is functional and well-structured but has significant gaps between what the code supports and what is documented or exposed to operators. The analysis identified **27 discrete findings** across 10 areas:

- **13 config options** exist in code but are absent from the example config (8 confirmed by scouts, 5 newly identified)
- **7 hardcoded values** that should be configurable
- **9 missing client features** vs server capabilities
- **Zero environment variable overrides** for any config option (only `BASTION_API_KEY` for the dashboard)
- **Zero config validation** beyond Pydantic type coercion (no range checks, no cross-field invariants)
- **No hot-reload capability** and no path toward one without architectural changes
- **No container deployment artifacts** (Dockerfile, Compose, Kubernetes manifests)
- **Solid systemd integration** with room for enhancement (journal structured fields, socket activation)

---

## 1. Config Options in Code but Missing from Example Config

The example config (`config/broker.example.yaml`) is a "quick start" file. It omits many options that exist in the full config (`config/broker.yaml`) and in the Pydantic models. New users who start from the example config will miss these tuning knobs entirely.

### 1.1 Confirmed Missing (from scout-data-models.md)

| # | Config Path | Default | Impact |
|---|------------|---------|--------|
| 1 | `ollama.unload_timeout_seconds` | `10.0` | Unload timeout invisible to example users |
| 2 | `proxy.max_request_body_bytes` | `10485760` (10 MB) | Size limit undocumented |
| 3 | `scheduler.residency_cache_ttl_seconds` | `1.0` | Cache freshness not tunable |
| 4 | `scheduler.loop_interval_seconds` | `0.1` | Scheduler tick rate hidden |
| 5 | `scheduler.max_concurrent_dispatches` | `3` | Concurrency limit undocumented |
| 6 | `scheduler.concurrent_dispatch_delay_seconds` | `0.1` | Power transient stagger hidden |
| 7 | `scheduler.queue_ttl_seconds` | `600.0` | Queue sweep policy missing |
| 8 | `gpu.nvidia_smi_timeout_seconds` | `5` | Health check timeout missing |

### 1.2 Newly Identified Missing Options

| # | Config Path | Default | Where It Appears |
|---|------------|---------|-----------------|
| 9 | `ollama.api_timeout_seconds` | `5.0` | In full config, missing from example |
| 10 | `gpu.max_power_watts` | `450.0` | In full config, missing from example |
| 11 | `gpu.default_vram_estimate_gb` | `10.0` | In full config, missing from example |
| 12 | `proxy.connect_timeout_seconds` | `10.0` | In full config, missing from example |
| 13 | `request_overrides.default_num_ctx` | `4096` | In code only, missing from ALL configs |

**Finding 13 is notable**: `RequestOverrides.default_num_ctx` exists in `models.py` (line 155) with a default of 4096, but neither the full config nor the example config includes it. The `proxy.py` module can inject this value into requests, but operators have no documented way to change or disable it.

### 1.3 Options in Full Config but Not in Example (by design)

These are intentionally omitted from the example to keep it minimal, but they exist in `config/broker.yaml`:

- All swap rate limiter fields (`swap_rate_*`) -- 5 fields
- Scheduler timing fields (`error_backoff_seconds`, `gpu_unsafe_backoff_seconds`, `shutdown_timeout_seconds`) -- 3 fields
- `scheduler.ollama_max_loaded_models` -- 1 field
- Session profiles section -- entire section
- Rate limiting section -- entire section (commented out in example)

**Assessment**: This design is acceptable. The example config is a "safe start" file. However, the gap between the example and the full config should be documented more explicitly. A commented-out section in the example showing ALL available options would help.

---

## 2. Hardcoded Values That Should Be Configurable

These values are embedded in source code with no config override path:

| # | Value | Location | Current | Should Be |
|---|-------|----------|---------|-----------|
| 1 | Audit log path | `server.py:404`, `audit.py:159` | `/tmp/bastion-audit.jsonl` | `audit.log_path` in config |
| 2 | Audit max bytes | `server.py:405` | `10 * 1024 * 1024` (10 MB) | `audit.max_bytes` in config |
| 3 | Audit backup count | `server.py:406` | `5` | `audit.backup_count` in config |
| 4 | VRAM journal path | `vram.py:303` | `/tmp/bastion-vram-journal.jsonl` | `audit.vram_journal_path` or similar |
| 5 | Recent requests buffer size | `server.py:108` | `50` | `server.recent_buffer_size` |
| 6 | Watchdog check interval | `ProcessMonitor.__init__` | `10.0` | `watchdog.check_interval_seconds` in config |
| 7 | Watchdog failure threshold | `ProcessMonitor.__init__` | `3` | `watchdog.failure_threshold` in config |

**Impact**: The audit log path is the most critical. In production, `/tmp` is often a tmpfs with limited space or is cleaned on reboot. Operators need to point audit logs to a persistent location without editing source code. The systemd unit uses `PrivateTmp=true`, which isolates `/tmp` per-service -- the journal path may not be where operators expect it.

**Recommendation**: Add `AuditConfig.log_path`, `AuditConfig.max_bytes`, `AuditConfig.backup_count` fields. Add a `WatchdogConfig` section to `BrokerConfig` for check interval and failure threshold.

---

## 3. Client Library vs Server Capabilities

### 3.1 Feature Gap Analysis

The `bastion-client` library (`clients/bastion-client/`) provides 3 methods. The server exposes approximately 20 endpoints. The gap is substantial:

| Server Capability | Server Endpoint | Client Support | Priority |
|------------------|----------------|----------------|----------|
| Inference | `POST /api/generate` | `infer()` | Covered |
| Intent declaration | `POST /broker/intent` | `declare_intent()` | Covered |
| VRAM status | `GET /broker/status` | `check_vram()` | Covered (with drift) |
| Chat completion | `POST /api/chat` | **Missing** | High |
| Embedding | `POST /api/embed` | **Missing** | High |
| Streaming inference | `POST /api/generate` (stream=true) | **Missing** (returns dict, not async iter) | High |
| Queue inspection | `GET /broker/queue` | **Missing** | Medium |
| Health check | `GET /broker/health` | **Missing** | Medium |
| Recent requests | `GET /broker/recent` | **Missing** | Medium |
| A2A task create | `POST /a2a/tasks` | **Missing** | Medium |
| A2A task query | `GET /a2a/tasks/{id}` | **Missing** | Medium |
| A2A task cancel | `DELETE /a2a/tasks/{id}` | **Missing** | Medium |
| A2A task stream (SSE) | `GET /a2a/tasks/{id}/stream` | **Missing** | Medium |
| Model preload | `POST /broker/preload` | **Missing** | Low |
| Model unload | `POST /broker/unload` | **Missing** | Low |
| Lease create/heartbeat/release | `POST/DELETE /a2a/leases/*` | **Missing** | Low |
| Watchdog status | `GET /broker/watchdog` | **Missing** | Low |
| Metrics | `GET /broker/metrics` | **Missing** | Low |
| Agent card | `GET /.well-known/agent-card.json` | **Missing** | Low |

### 3.2 Client-Server Model Drift (from scouts + new findings)

**Drift 1: VRAMInfo structure mismatch**

The client's `VRAMInfo` model expects GB units and a `utilization_pct` field. The server returns `GPUStatus` with MB units and no utilization field. The client manually converts in `check_vram()` (lines 122-132). This works but is fragile -- any change to the server's `BrokerStatus.gpu` schema will break the client silently.

**Drift 2: `total_requests_served` always zero**

`BrokerStatus.total_requests_served` (models.py:425) is never populated. The client and dashboard see `0` regardless of actual throughput. The data exists in `Scheduler._total_dispatched` but is not wired to the status response.

**Drift 3: `vram_ledger` always None**

`BrokerStatus.vram_ledger` (models.py:428-431) is declared but never populated from `VRAMManager.status()`.

**Drift 4 (NEW): `InferenceResult` model unused**

The client defines `InferenceResult` in `models.py` (line 38-46) and exports it in `__init__.py`, but `client.infer()` returns `dict[str, Any]`, not `InferenceResult`. The model is defined but never instantiated by the client code.

**Drift 5 (NEW): Version mismatch**

- `pyproject.toml` declares `version = "0.2.0"`
- `src/bastion/__init__.py` declares `__version__ = "0.1.0"`
- `clients/bastion-client/pyproject.toml` declares `version = "0.1.0"`

The server package metadata says 0.2.0 but the runtime version string says 0.1.0. `BrokerStatus.version` reads from `__init__.__version__`, so API consumers see "0.1.0" while pip would install "0.2.0".

### 3.3 Client Architecture Gaps

1. **No auth support**: Client has no `api_key` or `bearer_token` parameter. If the server enables `auth.enabled: true`, the client cannot authenticate. The dashboard supports `BASTION_API_KEY` but the programmatic client does not.

2. **No error model**: Client calls `resp.raise_for_status()` which raises `httpx.HTTPStatusError`. There is no BASTION-specific error class, no retry logic, and no handling for the circuit breaker's 503 responses.

3. **No connection pooling configuration**: The client creates a single `httpx.AsyncClient` with no pool limits. Under batch workloads, this may hit httpx's default connection limit.

4. **No streaming support**: `infer()` has a `stream: bool` parameter but always calls `resp.json()`, which will fail or produce incomplete results when `stream=True` (Ollama returns NDJSON, not a single JSON object).

---

## 4. Systemd Integration Analysis

### 4.1 What Exists (Solid Foundation)

The `systemd/` directory provides four `.example` files:

| File | Type | Integration Level |
|------|------|------------------|
| `bastion.service.example` | `Type=notify` with `WatchdogSec=30` | Production-grade |
| `ollama-port-override.conf.example` | Drop-in override for Ollama | Correct approach |
| `nvidia-powercap.service.example` | Oneshot power cap | Hardware-specific |
| `bastion-sudoers.example` | Dashboard systemctl access | Convenience |

**Strengths**:
- `Type=notify` with `WatchdogSec=30` is the right pattern. The `watchdog.py` module sends `READY=1`, `WATCHDOG=1`, `STOPPING=1` and `STATUS=` messages via `NOTIFY_SOCKET`.
- `Requires=ollama.service` ensures correct startup ordering.
- Security hardening: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp=true`.
- `Restart=always` with `RestartSec=3` and burst limits (`StartLimitBurst=5`, `StartLimitIntervalSec=60`).

### 4.2 Enhancement Opportunities

**4.2.1 Structured Journal Fields**

The service uses `StandardOutput=journal` and `SyslogIdentifier=bastion`, but the Python logging format is plain text. Adding structured journal fields would enable queries like `journalctl BASTION_MODEL=qwen3:14b`:

Currently not implemented. Would require `systemd.journal` Python bindings or `python-systemd` package.

**4.2.2 Socket Activation**

Socket activation (`Type=socket`, `ListenStream=11434`) would allow systemd to hold port 11434 open even when BASTION restarts. This is NOT implemented and would require changes to how uvicorn binds its socket. Feasibility: medium effort (uvicorn supports passing file descriptors via `--fd`).

**4.2.3 Missing `WatchdogConfig` in YAML**

The `ProcessMonitor` has hardcoded defaults for `check_interval` (10s), `ollama_timeout` (5s), `gpu_timeout` (5s), and `failure_threshold` (3). These should be configurable (see Finding 2 above). The `WatchdogSec=30` in the service file means systemd expects a heartbeat every 30s, but the monitor runs every 10s -- these are independent and could drift.

**4.2.4 `EnvironmentFile` Support**

The service unit hardcodes `PYTHONPATH` in an `Environment=` directive. It does not use `EnvironmentFile=` to load secrets (API keys, A2A tokens). For production deployments, secrets should not be in YAML config files. An `EnvironmentFile=/etc/bastion/env` approach would be more secure.

**4.2.5 Missing `After=nvidia-persistenced.service`**

The `bastion.service` uses `After=nvidia-powercap.service` but does not depend on `nvidia-persistenced.service`. If the system runs nvidia-persistenced (which keeps the GPU driver loaded), BASTION should start after it. The `nvidia-powercap.service` already depends on `nvidia-persistenced`, so indirect ordering works, but the dependency is not explicit for systems without the power cap service.

---

## 5. Deployment Patterns

### 5.1 Currently Supported

| Pattern | Status | Files |
|---------|--------|-------|
| Bare metal + systemd | Fully supported | `systemd/*.example` |
| Manual launch (development) | Fully supported | `python -m bastion` |
| Two-port mode | Fully implemented | `__main__.py`, `server.py` |
| pip-installable package | Fully supported | `pyproject.toml` with `[project.scripts]` |

### 5.2 Not Supported (No Artifacts)

| Pattern | Effort | Blockers |
|---------|--------|----------|
| Docker / Podman | Medium | No Dockerfile. Would need multi-stage build (Python + nvidia-smi). Requires NVIDIA Container Toolkit for GPU access. |
| Docker Compose | Medium | No `docker-compose.yml`. Would compose BASTION + Ollama + Prometheus + Grafana. |
| Kubernetes | High | No Helm chart or manifests. GPU scheduling requires `nvidia.com/gpu` resource requests. Node affinity for GPU nodes. |
| Ansible / Terraform | Low | No playbooks or modules. Systemd files are a reasonable starting point. |

**Docker Considerations**:
- BASTION requires access to `nvidia-smi` for GPU monitoring. This means the container needs `--gpus all` or equivalent.
- Ollama typically runs as a separate container or host process. The proxy pattern (BASTION on 11434, Ollama on 11435) maps naturally to Docker networking.
- The `/tmp/bastion-audit.jsonl` path needs a volume mount for persistence (especially with `PrivateTmp` behavior).
- The config search path includes `/etc/bastion/broker.yaml` which maps well to Docker config mounts.

**Kubernetes Considerations**:
- BASTION is a singleton (one instance per GPU). It fits the DaemonSet pattern (one per GPU node) or a single-replica Deployment.
- The watchdog's `sd_notify` is irrelevant in Kubernetes (no systemd). A Kubernetes liveness/readiness probe on `/broker/health` would replace it.
- The two-port mode maps to two Kubernetes Services pointing at the same Pod.

---

## 6. Environment Variable Overrides

### 6.1 Current State

BASTION has almost zero environment variable integration:

| Variable | Used By | Purpose |
|----------|---------|---------|
| `NOTIFY_SOCKET` | `watchdog.py:50` | Systemd watchdog (read by systemd, not BASTION config) |
| `BASTION_API_KEY` | `dashboard.py:2151` | Dashboard auth (only the TUI, not the programmatic client) |
| `PYTHONPATH` | `systemd/bastion.service.example:28` | Python import path (standard, not BASTION-specific) |

**There are no `BASTION_*` environment variable overrides for any config option.** This is a significant gap for container deployments where environment variables are the standard configuration mechanism.

### 6.2 Recommended Environment Variable Overrides

Following the convention of `BASTION_<SECTION>_<KEY>` (matching the YAML hierarchy):

| Variable | Maps to | Priority Use Case |
|----------|---------|------------------|
| `BASTION_OLLAMA_HOST` | `ollama.host` | Container deployments (Ollama as separate service) |
| `BASTION_OLLAMA_PORT` | `ollama.port` | Container deployments |
| `BASTION_SERVER_PORT` | `server.port` | Container deployments |
| `BASTION_SERVER_ADMIN_PORT` | `server.admin_port` | Two-port in containers |
| `BASTION_AUTH_ENABLED` | `auth.enabled` | Enable auth without config file change |
| `BASTION_AUTH_API_KEYS` | `auth.api_keys` | Secrets via env (comma-separated) |
| `BASTION_A2A_ENABLED` | `a2a.enabled` | Feature toggle |
| `BASTION_A2A_TOKENS` | `a2a.tokens` | Secrets via env (comma-separated) |
| `BASTION_LOG_LEVEL` | CLI `--log-level` | Container log level |
| `BASTION_CONFIG` | CLI `--config` | Config file path override |
| `BASTION_GPU_TOTAL_VRAM_GB` | `gpu.total_vram_gb` | Hardware-specific override |
| `BASTION_GPU_HEADROOM_GB` | `gpu.headroom_gb` | Hardware-specific override |

**Implementation approach**: Add a `_apply_env_overrides(config: BrokerConfig) -> BrokerConfig` function in `config.py` that reads `BASTION_*` variables after YAML loading. This is the pattern used by FastAPI, Pydantic Settings, and most 12-factor apps.

---

## 7. Config Validation Analysis

### 7.1 Current Validation

BASTION relies entirely on Pydantic's built-in type coercion for validation:

- **Type checking**: Pydantic enforces types (`int`, `float`, `str`, `bool`, `list[str]`). Invalid types raise `ValidationError`.
- **Default values**: All config fields have defaults, so a completely empty YAML file produces a valid `BrokerConfig`.
- **No `Field(ge=, le=, gt=, lt=)` constraints**: Confirmed by grep -- no range validators exist in `models.py`.
- **No `@field_validator` or `@model_validator` decorators**: Confirmed by grep -- none exist.

### 7.2 Unhandled Edge Cases

| # | Scenario | Current Behavior | Expected Behavior |
|---|----------|-----------------|-------------------|
| 1 | `cooldown_seconds: -1.0` | Accepted, negative cooldown | Should reject (`ge=0`) |
| 2 | `max_queue_size: 0` | Accepted, queue immediately full | Should reject (`ge=1`) |
| 3 | `headroom_gb > total_vram_gb` | Accepted, negative `max_vram_gb` | Should reject (cross-field) |
| 4 | `swap_rate_warn_threshold > swap_rate_critical_threshold` | Accepted, warn fires after critical | Should reject (invariant) |
| 5 | `port: 0` | Accepted, OS picks random port | Should warn or validate range |
| 6 | `inference_timeout_seconds: 0` | Accepted, immediate timeout | Should reject (`gt=0`) |
| 7 | `max_concurrent_dispatches: 0` | Accepted, no dispatches ever | Should reject (`ge=1`) |
| 8 | `aging_rate: -5.0` | Accepted, requests lose priority over time | Should reject (`ge=0`) |
| 9 | `audit.tier: 99` | Accepted, behaves as tier 3+ | Should constrain to 1-3 |
| 10 | `models: {"name": {"vram_gb": -5}}` | Accepted, negative VRAM | Should reject (`gt=0`) |
| 11 | Unknown YAML keys (typos) | Silently ignored | Should warn about unrecognized keys |
| 12 | `ollama.host: ""` | Accepted, empty host | Should reject (non-empty string) |

**Finding 11 is particularly dangerous**: If a user writes `scheduller:` (typo) instead of `scheduler:`, Pydantic ignores the entire section and uses defaults. The user gets no feedback that their configuration was not applied.

### 7.3 Recommendation

Add a `@model_validator(mode='after')` to `BrokerConfig` that:
1. Checks `headroom_gb < total_vram_gb`
2. Checks `swap_rate_warn_threshold <= swap_rate_critical_threshold`
3. Logs warnings for zero or negative values on timing fields
4. Warns about unknown top-level keys in the raw YAML (requires passing raw dict to validator)

Add `Field(ge=0)` or `Field(gt=0)` constraints on all timing and size fields.

---

## 8. Hot-Reload Feasibility

### 8.1 Current Architecture

Config is loaded once at startup in `__main__.py:75` (`config = load_config(args.config)`) and passed to `create_app(config)`. The config is stored as `app.state.config` and propagated to all components during `lifespan()`. After startup, config is immutable.

### 8.2 What Prevents Hot-Reload

1. **Config propagated to constructors**: `Scheduler`, `VRAMTracker`, `VRAMManager`, `OllamaProxy`, `A2AHandler`, `AffinityQueue` all receive `config` (or sub-configs) in their constructors and store private copies. Changing config requires re-creating these objects.

2. **Module-level state in `server.py`**: The 12 module globals (`_proxy`, `_scheduler`, etc.) are set once during lifespan. There is no mechanism to swap them atomically.

3. **httpx clients with configured timeouts**: `OllamaProxy` and `A2AHandler` create `httpx.AsyncClient` instances with timeout settings from config. Changing timeouts requires creating new clients.

4. **Middleware configured at startup**: `RateLimitMiddleware` reads `RateLimitConfig` at construction time. FastAPI middleware cannot be hot-swapped.

### 8.3 What Could Be Hot-Reloaded (Partial)

Some config values are read at decision time, not construction time:

| Config Value | Read At | Hot-Reloadable? |
|-------------|---------|-----------------|
| `scheduler.cooldown_seconds` | Each `_process_tick()` | Yes (if scheduler reads from mutable reference) |
| `scheduler.aging_rate` | Each `pick_next()` call | Yes |
| `priorities.*` | Each request priority calculation | Yes |
| `models.*` (known models) | Each VRAM check | Yes |
| `audit.tier` | Each `emit_tiered()` call | Yes |
| `auth.api_keys` | Each request authentication | Yes |
| `rate_limit.*` | Construction time | No |
| `proxy.*_timeout_*` | httpx client construction | No |

### 8.4 Recommendation

Full hot-reload is not feasible without significant refactoring. A pragmatic approach:

1. **SIGHUP handler** that re-reads `broker.yaml` and updates mutable config values (priorities, models, auth keys, scheduler timing).
2. **Immutable restart** for structural changes (ports, timeouts, A2A enable/disable) -- requires service restart.
3. **`POST /broker/reload` admin endpoint** as an alternative to SIGHUP for HTTP-based management.

This would cover the most common operational changes (adding models, adjusting priorities, rotating API keys) without a restart.

---

## 9. Phase 4 Polish Plans vs Implementation

The file `docs/audit/ref-phase4-polish.md` is titled "Phase 5: BASTION Test Hardening" (despite the filename). It focused on test coverage, not config/integration polish. It describes two workstreams:

### 9.1 What Was Planned

- **Workstream A**: Scheduler swap rate limiter tests, concurrent dispatch edge cases, circuit breaker transport tests, model eviction tests
- **Workstream B**: VRAM edge case tests, TaskStore stress tests, safe transition race tests, VRAM tracker edge cases

### 9.2 What Was NOT Planned (Config/Integration Gaps)

The phase 4/5 plan did not address any of the following:

1. Config validation (range checks, cross-field invariants)
2. Environment variable overrides
3. Audit log path configurability
4. Client library feature parity
5. Docker/container deployment artifacts
6. Hot-reload mechanism
7. Version string consistency (`pyproject.toml` vs `__init__.py`)
8. Watchdog config exposure in YAML

These items represent a natural "Phase 6: Config & Integration Polish" scope.

---

## 10. External Tool Integration

### 10.1 Reverse Proxy (nginx, Caddy, Traefik)

BASTION itself is a reverse proxy (for Ollama), but it may sit behind another reverse proxy for TLS termination, load balancing, or network segmentation.

**Current support**: None documented. The `X-Broker-Priority` header must be passed through. The streaming responses (NDJSON, SSE) require the reverse proxy to NOT buffer responses.

**nginx considerations**:
- `proxy_buffering off;` is required for streaming endpoints (`/api/generate`, `/a2a/tasks/{id}/stream`)
- `proxy_read_timeout 300s;` must match `proxy.inference_timeout_seconds`
- `proxy_pass http://localhost:11434;` for standard proxy

**Caddy considerations**:
- `reverse_proxy localhost:11434` with `flush_interval -1` for streaming
- Automatic TLS would add HTTPS with zero config

### 10.2 Monitoring Stack (Prometheus + Grafana)

**Current support**: `GET /broker/metrics` endpoint returns Prometheus-format metrics when `prometheus-client` is installed. 35+ metrics are defined in `metrics.py`.

**What is missing**:
- No example Prometheus scrape config (`prometheus.yml` snippet)
- No example Grafana dashboard JSON
- No alerting rules (e.g., alert when `swap_rate_level == "critical"` or circuit breaker opens)
- No ServiceMonitor CRD for Kubernetes Prometheus Operator

### 10.3 OpenTelemetry

**Current support**: `telemetry.py` implements full OTel instrumentation with no-op stubs when the SDK is absent. Supports console and OTLP exporters via config.

**What is missing**:
- No example Jaeger/Tempo docker-compose for local tracing
- No documented trace propagation headers (the A2A handler emits spans but does not propagate W3C TraceContext headers to Ollama)

### 10.4 Log Aggregation (Loki, ELK, Fluentd)

**Current support**: Audit logs are written to `/tmp/bastion-audit.jsonl` in JSONL format. This is directly parseable by any log aggregator.

**What is missing**:
- No example Fluentd/Fluent Bit config for tailing the JSONL file
- No example Loki/Promtail config
- The audit log path is hardcoded (see Finding 2), making log aggregation integration require knowing the internal path

### 10.5 GPU Monitoring (DCGM, nvidia-smi exporter)

**Current support**: BASTION queries `nvidia-smi` directly via subprocess for temperature, VRAM, and power. It also queries Ollama `/api/ps` for loaded model state.

**What is missing**:
- No integration with NVIDIA DCGM (Data Center GPU Manager) for more detailed GPU metrics
- No `nvidia_gpu_exporter` integration (Prometheus exporter for GPU metrics)
- Potential overlap: if both BASTION and an external GPU exporter run nvidia-smi, they create redundant subprocess calls

---

## Findings Summary

### Critical (Should Fix)

| # | Finding | Files Affected |
|---|---------|---------------|
| C1 | Audit log path hardcoded to `/tmp/` (unsafe with PrivateTmp, lost on reboot) | `server.py:404`, `audit.py:159` |
| C2 | Version mismatch: pyproject.toml=0.2.0, __init__.py=0.1.0 | `pyproject.toml:4`, `src/bastion/__init__.py:13` |
| C3 | Zero config validation beyond types (negative cooldowns, headroom > total VRAM accepted) | `src/bastion/models.py` |
| C4 | Unknown YAML keys silently ignored (typos produce default config with no warning) | `src/bastion/config.py` |
| C5 | Client `infer(stream=True)` calls `resp.json()` which will fail on NDJSON | `clients/bastion-client/bastion_client/client.py:111` |

### High (Should Plan)

| # | Finding | Files Affected |
|---|---------|---------------|
| H1 | Zero environment variable overrides for any config option | `src/bastion/config.py` |
| H2 | Client has no auth support (no api_key parameter) | `clients/bastion-client/bastion_client/client.py` |
| H3 | Client missing `/api/chat`, `/api/embed`, streaming, queue, health, A2A endpoints | `clients/bastion-client/bastion_client/client.py` |
| H4 | `request_overrides.default_num_ctx` not in any config file | All config files |
| H5 | Watchdog config (check_interval, failure_threshold) hardcoded, not in YAML | `src/bastion/watchdog.py` |
| H6 | `total_requests_served` always 0 (not wired to `_total_dispatched`) | `src/bastion/server.py` |
| H7 | `vram_ledger` always None (not wired to `VRAMManager.status()`) | `src/bastion/server.py` |

### Medium (Should Document)

| # | Finding | Context |
|---|---------|---------|
| M1 | 13 config options missing from example config | See Section 1 |
| M2 | No Docker/container deployment artifacts | See Section 5.2 |
| M3 | No reverse proxy configuration examples (nginx, Caddy) | See Section 10.1 |
| M4 | No Prometheus scrape config or Grafana dashboard examples | See Section 10.2 |
| M5 | No hot-reload capability (requires restart for all config changes) | See Section 8 |
| M6 | `InferenceResult` model defined but never used by client | `clients/bastion-client/bastion_client/models.py:38` |
| M7 | Config search paths not documented for operators | `src/bastion/config.py:19-24` |

### Low (Nice to Have)

| # | Finding | Context |
|---|---------|---------|
| L1 | No socket activation support in systemd unit | See Section 4.2.2 |
| L2 | No structured journal fields | See Section 4.2.1 |
| L3 | No SIGHUP handler for partial hot-reload | See Section 8.4 |
| L4 | `bastion.service.example` missing `After=nvidia-persistenced.service` | `systemd/bastion.service.example` |
| L5 | No EnvironmentFile for secrets in systemd unit | See Section 4.2.4 |

---

## Key Files Referenced

| File | Path | Role |
|------|------|------|
| Config loader | `/home/user/BASTION/src/bastion/config.py` | YAML search paths, ModelInfo transform |
| All models | `/home/user/BASTION/src/bastion/models.py` | 32 Pydantic models, zero validators |
| Full config | `/home/user/BASTION/config/broker.yaml` | All options, production values |
| Example config | `/home/user/BASTION/config/broker.example.yaml` | Minimal starter, missing 13+ options |
| CLI entry | `/home/user/BASTION/src/bastion/__main__.py` | CLI arg parsing, two-port launch |
| Server factory | `/home/user/BASTION/src/bastion/server.py` | App creation, lifespan, all routes |
| Audit logger | `/home/user/BASTION/src/bastion/audit.py` | Hardcoded /tmp path |
| Watchdog | `/home/user/BASTION/src/bastion/watchdog.py` | sd_notify, ProcessMonitor |
| Client library | `/home/user/BASTION/clients/bastion-client/bastion_client/client.py` | 3 methods, no auth |
| Client models | `/home/user/BASTION/clients/bastion-client/bastion_client/models.py` | 4 models, 1 unused |
| Client tests | `/home/user/BASTION/clients/bastion-client/tests/test_client.py` | 15 tests |
| Server pyproject | `/home/user/BASTION/pyproject.toml` | Version 0.2.0, deps |
| Client pyproject | `/home/user/BASTION/clients/bastion-client/pyproject.toml` | Version 0.1.0, deps |
| systemd service | `/home/user/BASTION/systemd/bastion.service.example` | Type=notify, security hardening |
| Ollama override | `/home/user/BASTION/systemd/ollama-port-override.conf.example` | Port 11435, env vars |
| Power cap | `/home/user/BASTION/systemd/nvidia-powercap.service.example` | GPU power limit |
| Sudoers | `/home/user/BASTION/systemd/bastion-sudoers.example` | Dashboard systemctl |
| Phase 4 plan | `/home/user/BASTION/docs/audit/ref-phase4-polish.md` | Test hardening (not config) |

---

**End of Report**

Generated by Config & Integration Analyst
Session: S0 (Audit Phase)
