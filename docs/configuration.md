# Configuration Reference

## Config File Location

BASTION searches for config in this order:

1. `--config` CLI flag (highest priority)
2. `config/broker.yaml` (project directory)
3. `./broker.yaml` (current directory)
4. `/etc/bastion/broker.yaml` (Linux only)
5. `~/.config/bastion/broker.yaml` (XDG)

Generate a starter config: `bastion --init-config`

## Environment Variable Overrides

Environment variables override config file values. Useful for Docker/systemd.

| Variable | Config Path | Type | Example |
|----------|------------|------|---------|
| `BASTION_OLLAMA_HOST` | `ollama.host` | string | `"192.168.1.10"` |
| `BASTION_OLLAMA_PORT` | `ollama.port` | int | `11435` |
| `BASTION_PORT` | `server.port` | int | `11434` |
| `BASTION_ADMIN_PORT` | `server.admin_port` | int | `9999` |
| `BASTION_GPU_TOTAL_VRAM_GB` | `gpu.total_vram_gb` | float | `24.0` |
| `BASTION_GPU_MAX_TEMP_C` | `gpu.max_temperature_c` | int | `83` |
| `BASTION_GPU_MAX_POWER_W` | `gpu.max_power_watts` | float | `300` |
| `BASTION_AUTH_ENABLED` | `auth.enabled` | bool | `true` |
| `BASTION_API_KEYS` | `auth.api_keys` | csv | `"key1,key2"` |
| `BASTION_AUDIT_TIER` | `audit.tier` | int | `2` |
| `BASTION_PERSISTENCE_ENABLED` | `persistence.enabled` | bool | `true` |
| `BASTION_PERSISTENCE_DB_PATH` | `persistence.database_path` | string | `"/data/bastion.db"` |

## Configuration Sections

### ollama

Ollama backend connection settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `"127.0.0.1"` | Ollama backend host |
| `port` | int | `11435` | Ollama backend port (moved from default 11434) |
| `api_timeout_seconds` | float | `5.0` | Timeout for `/api/ps` queries |
| `unload_timeout_seconds` | float | `10.0` | Timeout for model unload requests |

### server

BASTION server settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `"0.0.0.0"` | Bind address |
| `port` | int | `11434` | Listen port (standard Ollama port) |
| `admin_port` | int | `0` | Admin+A2A port. `0` = disabled (single-port mode). Set to a different port to enable two-port mode. |
| `public_url` | string | `null` | Full external URL for A2A agent card advertisement. Use when BASTION runs behind a reverse proxy or on a specific IP and you want third-party agents to discover the correct endpoint. Defaults to `http://localhost:<port>` when unset. |

### gpu

GPU safety thresholds. Set `total_vram_gb` to `0` to auto-detect from nvidia-smi.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `total_vram_gb` | float | `0.0` | Total GPU VRAM. `0` = auto-detect. |
| `headroom_gb` | float | `6.0` | VRAM reserved for OS, display, CUDA overhead |
| `max_temperature_c` | int | `83` | Block model loads above this temperature |
| `max_power_watts` | float | `300.0` | Max power threshold. Auto-detect overrides. |
| `default_vram_estimate_gb` | float | `10.0` | VRAM estimate for models not in config |
| `nvidia_smi_timeout_seconds` | int | `5` | nvidia-smi subprocess timeout |

Computed: `max_vram_gb = total_vram_gb - headroom_gb`

### scheduler

Scheduling algorithm parameters.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cooldown_seconds` | float | `2.0` | Mandatory pause between model transitions |
| `model_affinity_bonus` | float | `10.0` | Priority bonus for requests matching current model |
| `aging_rate` | float | `2.0` | Priority points gained per second waiting |
| `max_queue_size` | int | `512` | Maximum queue depth before rejecting |
| `residency_cache_ttl_seconds` | float | `1.0` | TTL for model residency cache |
| `ollama_max_loaded_models` | int | `4` | Max models Ollama keeps loaded |
| `loop_interval_seconds` | float | `0.1` | Scheduler wake-up interval |
| `error_backoff_seconds` | float | `1.0` | Backoff after scheduler loop error |
| `gpu_unsafe_backoff_seconds` | float | `5.0` | Backoff when GPU health check fails |
| `shutdown_timeout_seconds` | float | `10.0` | Max time to wait for scheduler stop |
| `swap_rate_window_seconds` | float | `60.0` | Rolling window for swap counting |
| `swap_rate_warn_threshold` | int | `4` | Swaps/min to start throttling |
| `swap_rate_critical_threshold` | int | `6` | Swaps/min for hard brake |
| `swap_rate_warn_cooldown_seconds` | float | `5.0` | Cooldown at warn level |
| `swap_rate_critical_cooldown_seconds` | float | `10.0` | Cooldown at critical level |
| `max_concurrent_dispatches` | int | `3` | Max concurrent inferences (different models) |
| `concurrent_dispatch_delay_seconds` | float | `0.1` | Stagger delay for concurrent dispatches |
| `queue_ttl_seconds` | float | `600.0` | Max age for queued requests (10 min) |

### proxy

Proxy routing and timeout settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `inference_timeout_seconds` | float | `300.0` | HTTP timeout for Ollama inference |
| `connect_timeout_seconds` | float | `10.0` | HTTP connect timeout |
| `queue_timeout_seconds` | float | `300.0` | Max wait in queue before 504 |
| `max_request_body_bytes` | int | `10485760` | Max request body size (10 MB) |
| `scheduled_endpoints` | set | `{"/api/generate", "/api/chat", "/api/embed"}` | Endpoints that go through the scheduler |
| `passthrough_endpoints` | set | `{"/api/pull", "/api/show", "/api/tags", ...}` | Endpoints forwarded directly to Ollama |

### priorities

Base priority values for each tier. Higher values = higher priority.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `interactive` | float | `100.0` | User-facing: `ollama run`, IDE integrations |
| `agent` | float | `50.0` | AI agent frameworks, A2A clients |
| `pipeline` | float | `25.0` | Batch extraction and ingestion |
| `background` | float | `10.0` | Overnight jobs, consolidation tasks |

Set priority via HTTP header: `X-Broker-Priority: pipeline`

### models

Known model metadata. Map of model name to configuration:

```yaml
models:
  llama3.1:8b:
    vram_gb: 4.7
    default_num_ctx: 4096
    tags: ["general"]
  mistral:7b:
    vram_gb: 4.1
    default_num_ctx: 4096
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vram_gb` | float | required | Expected VRAM usage |
| `default_num_ctx` | int | `4096` | Default context window |
| `tags` | list[str] | `[]` | Arbitrary tags |
| `always_allowed` | bool | `false` | Skip VRAM budget check |

Use `bastion --detect-models` to auto-generate this section.

### request_overrides

Safety overrides injected into ALL Ollama requests.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `use_mmap` | bool | `false` | Disable memory-mapped model loading (GPU crash prevention) |
| `default_num_ctx` | int \| None | `4096` | Global fallback context window |

### auth

Authentication configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable API key authentication |
| `api_keys` | list[str] | `[]` | Valid API keys |

### rate_limit

Per-IP rate limiting.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable rate limiting |
| `requests_per_minute` | int | `60` | Sustained request rate per IP |
| `burst` | int | `10` | Burst allowance above sustained rate |

### circuit_breaker

Three-state circuit breaker for backend failures.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable circuit breaker |
| `failure_threshold` | int | `5` | Consecutive failures to trip open |
| `recovery_timeout` | float | `30.0` | Seconds before half-open probe |

### audit

Tiered audit logging.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tier` | int | `2` | Logging detail: 1=minimal, 2=hashes, 3=full content |
| `content_hashing` | bool | `true` | SHA-256 hash prompt/response content |

### persistence

Optional SQLite persistence.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable SQLite persistence |
| `database_path` | string | `""` | Path to database. Empty = auto (XDG data dir) |
| `persist_audit` | bool | `true` | Persist audit events |
| `persist_tasks` | bool | `true` | Persist A2A tasks |
| `persist_queue` | bool | `false` | Persist queue state (opt-in) |
| `queue_recovery_ttl` | int | `300` | Discard queue entries older than this on startup |

### telemetry

OpenTelemetry tracing.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable OpenTelemetry tracing |
| `exporter` | string | `"none"` | Exporter: `"none"`, `"console"`, `"otlp"` |
| `endpoint` | string | `""` | OTLP endpoint (e.g. `"http://localhost:4317"`) |
| `service_name` | string | `"bastion"` | Service name in traces |

### a2a

A2A (Agent-to-Agent) interface.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable A2A protocol endpoints |
| `tokens` | list[str] | `[]` | Bearer tokens for A2A auth |
| `reservation_max_requests` | int | `100` | Max requests per model lease |
| `reservation_timeout_seconds` | float | `600.0` | Lease TTL (10 minutes) |
| `task_ttl_seconds` | float | `3600.0` | Completed task retention (1 hour) |
| `max_batch_size` | int | `50` | Max prompts per batch_infer |

### complexity_routing

Complexity-based model routing.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable complexity routing |
| `routes` | dict | `{}` | Map complexity level to model name (e.g. `"simple": "qwen3:1.7b"`) |
| `complex_action` | string | `"reject"` | Action for complex requests: `"reject"` returns HTTP 422 |

Clients set complexity via header: `X-Task-Complexity: simple`

### thrashing_detection

Per-agent swap thrashing detection.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable thrashing detection |
| `mode` | string | `"warn"` | `"warn"` (log only) or `"strict"` (reject requests) |
| `window_size` | int | `12` | Request window for swap ratio calculation |
| `warn_swap_ratio` | float | `0.5` | Ratio threshold for warning |
| `halt_swap_ratio` | float | `0.75` | Ratio threshold for halting (strict mode) |
| `cooloff_seconds` | int | `30` | Cooloff period after halt |
| `min_requests_before_eval` | int | `6` | Minimum requests before evaluating |

## Preset Configurations

### Minimal (8 GB GPU, single user)

```yaml
ollama:
  port: 11435

server:
  port: 11434

gpu:
  total_vram_gb: 0       # auto-detect
  headroom_gb: 2

scheduler:
  cooldown_seconds: 3.0
  max_concurrent_dispatches: 1

models: {}               # use --detect-models to populate
```

### Standard (24 GB GPU, multiple agents)

```yaml
ollama:
  port: 11435

server:
  port: 11434

gpu:
  total_vram_gb: 0
  headroom_gb: 6

scheduler:
  cooldown_seconds: 2.0
  max_concurrent_dispatches: 3

priorities:
  interactive: 100
  agent: 50
  pipeline: 25
  background: 10

models: {}
```

### Production (24+ GB GPU, systemd, persistence)

```yaml
ollama:
  port: 11435

server:
  host: "127.0.0.1"     # localhost only
  port: 11434
  admin_port: 9999       # separate admin port

gpu:
  total_vram_gb: 0
  headroom_gb: 6

scheduler:
  cooldown_seconds: 2.0
  max_concurrent_dispatches: 3

auth:
  enabled: true
  api_keys:
    - "change-me-to-a-real-key"

rate_limit:
  enabled: true
  requests_per_minute: 120
  burst: 20

persistence:
  enabled: true

audit:
  tier: 2

models: {}
```
