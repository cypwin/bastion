# Operations Guide

## Starting and Stopping

### Start

```bash
# Direct
bastion

# With config
bastion --config config/broker.yaml

# Two-port mode
bastion --admin-port 9999

# Via systemd
sudo systemctl start bastion
```

Verify BASTION is running:

```bash
curl http://localhost:11434/broker/status
```

### Graceful Shutdown

BASTION handles SIGTERM gracefully:

1. Stops accepting new requests
2. Drains the queue (completes in-flight requests)
3. Closes httpx clients and connections
4. Exits cleanly

```bash
# systemd
sudo systemctl stop bastion

# Manual (sends SIGTERM)
kill $(pgrep -f "python -m bastion")
```

The systemd service has `TimeoutStopSec=15`. If shutdown takes longer than 15 seconds, systemd sends SIGKILL.

### Safe Restart

For zero-downtime restarts, drain first:

```bash
# 1. Enter drain mode (stops accepting new requests)
curl -X POST http://localhost:11434/broker/drain

# 2. Wait for queue to empty
watch -n2 'curl -s http://localhost:11434/broker/status | python -m json.tool | grep queue_depth'

# 3. Restart when queue_depth is 0
sudo systemctl restart bastion
```

## Monitoring

### Key Health Endpoints

| Endpoint | What It Tells You | Check Frequency |
|----------|------------------|----------------|
| `GET /broker/health` | Is BASTION alive? | Every 30s |
| `GET /broker/livez` | Liveness probe (Kubernetes) | Every 10s |
| `GET /broker/readyz` | Readiness probe (Kubernetes) | Every 10s |
| `GET /broker/status` | Queue depth, GPU state, circuit breaker, swap rate | Every 60s |

### Broker Status

```bash
curl -s http://localhost:11434/broker/status | python -m json.tool
```

Key fields in the response:

| Field | Description | Alert When |
|-------|-------------|------------|
| `queue_depth` | Total requests waiting | > 50 sustained |
| `total_model_swaps` | Lifetime swap count | Rate > 4/min |
| `gpu.temperature_c` | Current GPU temp | > 80C |
| `gpu.vram_used_mb` | VRAM in use | > 90% of budget |
| `state` | `running` / `draining` / `stopped` | Not `running` |
| `swap_rate_level` | `normal` / `warn` / `critical` | `warn` or `critical` |
| `circuit_breaker.state` | `closed` / `open` / `half_open` | `open` |

### Prometheus Metrics

Available at `GET /broker/metrics` (requires `pip install -e ".[metrics]"`).

| Metric | Type | Description |
|--------|------|-------------|
| `bastion_requests_total` | Counter | Total requests served |
| `bastion_queue_depth` | Gauge | Current queue depth (by model) |
| `bastion_model_swap_total` | Counter | Total model swap operations |
| `bastion_vram_used_bytes` | Gauge | Current VRAM usage |
| `bastion_gpu_temperature_celsius` | Gauge | GPU temperature |
| `bastion_request_duration_seconds` | Histogram | Request latency |
| `bastion_request_queue_wait_seconds` | Histogram | Time spent waiting in queue (labels: priority, model) |

### What to Watch

| Signal | Normal | Concerning | Critical |
|--------|--------|------------|----------|
| Queue depth | 0-10 | 10-50 | > 50 |
| Swap rate | < 2/min | 2-4/min | > 4/min |
| GPU temp | < 70C | 70-80C | > 80C |
| VRAM usage | < 80% | 80-90% | > 90% |
| Circuit breaker | closed | half_open | open |

## Queue Management

### Viewing the Queue

```bash
curl -s http://localhost:11434/broker/queue | python -m json.tool
```

### Drain Mode

Enter drain mode to stop accepting new requests while completing in-flight work:

```bash
# Enter drain mode
curl -X POST http://localhost:11434/broker/drain

# Check status (state will show "draining")
curl -s http://localhost:11434/broker/status | python -m json.tool | grep state

# Exit drain mode (resume accepting requests)
curl -X POST http://localhost:11434/broker/resume
```

To exit drain mode, POST to `/broker/resume` (drain is not a toggle).

### Preloading Models

Pre-load a model into VRAM to reduce first-request latency:

```bash
curl -X POST http://localhost:11434/broker/preload \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b"}'
```

Preloading counts against the VRAM budget. Only preload models you expect to use soon.

### Unloading Models

Free VRAM by unloading an idle model:

```bash
curl -X POST http://localhost:11434/broker/unload \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b"}'
```

Do not unload a model that has active requests -- check `/broker/status` first.

## Model Management

### Adding a Model

1. Pull the model with Ollama: `ollama pull <model>`
2. Run `bastion --detect-models` to get the YAML config
3. Add the model entry to your `broker.yaml`
4. Either restart BASTION or preload the model

### Removing a Model

1. Unload the model: `POST /broker/unload`
2. Remove it from `broker.yaml`
3. Optionally remove from Ollama: `ollama rm <model>`
4. Restart BASTION

## Log Locations

| Log | Location | Format |
|-----|----------|--------|
| Application | stdout / systemd journal | Text |
| Audit | `~/.local/share/bastion/bastion-audit.jsonl` | JSONL |
| VRAM journal | `~/.local/share/bastion/bastion-vram-journal.jsonl` | JSONL |
| Persistence DB | `~/.local/share/bastion/bastion.db` | SQLite |

Override data directory with `BASTION_DATA_DIR` environment variable.

## Calibrated GPU Profile

After running `bastion --stress-test`, a calibration profile is saved to
`~/.config/bastion/gpu-profile.yaml`. BASTION loads this at startup and
uses the calibrated values for:

- Safe swap rate per minute
- Maximum concurrent dispatches
- VRAM headroom
- Thermal ceiling
- Cooldown duration

To re-calibrate after hardware changes, run `bastion --stress-test` again.
The old profile is overwritten.
