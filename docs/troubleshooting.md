# Troubleshooting

## BASTION Won't Start

### "Address already in use" on port 11434

**Symptom:** `OSError: [Errno 98] error while attempting to bind on address ('0.0.0.0', 11434)`

**Cause:** Another process (likely Ollama) is using port 11434.

**Fix:** Move Ollama to port 11435 first. See [Getting Started: Move Ollama to Port 11435](getting-started.md#3-move-ollama-to-port-11435).

To check what is using the port:

```bash
ss -tlnp | grep 11434
```

### "No config file found"

**Symptom:** Warning at startup, BASTION uses defaults.

**Cause:** No `broker.yaml` in any of the search paths.

**Fix:** Generate a config:

```bash
bastion --init-config
bastion --detect-models
```

Then restart BASTION.

### Python version too old

**Symptom:** `SyntaxError` or `ImportError` on startup.

**Cause:** BASTION requires Python 3.11+.

**Fix:** Check your version: `python --version`. Install Python 3.11+ or use a virtual environment.

## Ollama Connection Issues

### "Ollama unreachable" / circuit breaker OPEN

**Symptom:** All requests return 503. `/broker/status` shows circuit breaker state `open`.

**Cause:** Ollama is not running or not listening on the configured port.

**Fix:**

1. Check if Ollama is running: `systemctl status ollama`
2. Verify the port: `curl http://localhost:11435`
3. Check your config matches: look at `ollama.port` in `broker.yaml`
4. If Ollama crashed, restart it: `sudo systemctl restart ollama`

The circuit breaker will automatically recover after `recovery_timeout` seconds (default: 30s).

### Port 11435 blocked (nftables)

**Symptom:** BASTION starts but can't reach Ollama. `curl http://localhost:11435` returns connection refused even though Ollama is running.

**Cause:** nftables rules may restrict port 11435 to specific groups.

**Fix:** If you have nftables port lockdown configured, ensure the BASTION process runs under the `bastion` group:

```bash
sudo usermod -aG bastion $USER
```

Then log out and back in.

## GPU Issues

### GPU not detected

**Symptom:** `bastion --validate` shows `[FAIL] NVIDIA GPU: nvidia-smi not found`.

**Cause:** NVIDIA drivers not installed or nvidia-smi not in PATH.

**Fix:**

```bash
# Check if nvidia-smi is available
nvidia-smi

# Install drivers (Ubuntu)
sudo apt install nvidia-driver-560

# After installation, reboot
sudo reboot
```

### VRAM exhausted / model won't load

**Symptom:** Requests queue indefinitely. `/broker/status` shows VRAM near budget limit.

**Cause:** Too many models loaded, or a model exceeds available VRAM.

**Fix:**

1. Check loaded models: `curl http://localhost:11434/broker/status | python -m json.tool`
2. Unload unused models:
   ```bash
   curl -X POST http://localhost:11434/broker/unload \
     -H "Content-Type: application/json" \
     -d '{"model": "unused-model:latest"}'
   ```
3. Increase headroom in config if models are being evicted too aggressively:
   ```yaml
   gpu:
     headroom_gb: 4   # reduce from default 6 for more usable VRAM
   ```

### GPU temperature too high / scheduling paused

**Symptom:** Requests queue but no inference happens. Logs show temperature warnings.

**Cause:** GPU temperature exceeds `gpu.max_temperature_c` (default: 83C).

**Fix:**

1. Check current temperature: `nvidia-smi`
2. Improve cooling (fans, airflow)
3. Or raise the threshold if your GPU is rated for higher temps:
   ```yaml
   gpu:
     max_temperature_c: 87
   ```

### nvidia-smi timeouts / GPU lockup

**Symptom:** Logs show `nvidia-smi timeout`. Three consecutive timeouts trigger automatic drain.

**Cause:** GPU driver is wedged, often a precursor to crashes.

**Fix:**

1. Check GPU status: `nvidia-smi`
2. If nvidia-smi hangs, the driver is wedged. Restart Ollama: `sudo systemctl restart ollama`
3. If repeated, check swap rate in `/broker/status` -- sustained rates above 4/min indicate stress. See [Crash Prevention](crash-prevention.md).

## Queue and Performance

### Queue growing / requests timing out

**Symptom:** Queue depth keeps increasing. Requests return 504 after timeout.

**Cause:** Requests arriving faster than they can be served, or model swaps taking too long.

**Fix:**

1. Check queue depth: `curl http://localhost:11434/broker/status`
2. If queue is backed up with different models, consider preloading your most-used model:
   ```bash
   curl -X POST http://localhost:11434/broker/preload \
     -H "Content-Type: application/json" \
     -d '{"model": "llama3.1:8b"}'
   ```
3. Reduce `proxy.queue_timeout_seconds` if you prefer fast failures over queueing
4. Scale to a larger GPU or reduce concurrent clients

### Priority not being applied

**Symptom:** Background requests are served before interactive ones.

**Cause:** Priority header not set by the client.

**Fix:** Set the priority header on requests:

```bash
curl http://localhost:11434/api/generate \
  -H "X-Broker-Priority: interactive" \
  -d '{"model": "llama3.1:8b", "prompt": "hello"}'
```

Valid tiers: `interactive` (100), `agent` (50), `pipeline` (25), `background` (10).

### Streaming not working (buffered responses)

**Symptom:** Response arrives all at once instead of streaming token by token.

**Cause:** A reverse proxy or middleware is buffering the response.

**Fix:**

1. If using nginx, add: `proxy_buffering off;`
2. If using Caddy, streaming works by default
3. Ensure `"stream": true` is set in the request body (Ollama defaults to streaming)
4. Check that BASTION is not behind a proxy that buffers Server-Sent Events

## Authentication and Rate Limiting

### Auth failures (401/403)

**Symptom:** Requests return `401 Unauthorized` or `403 Forbidden`.

**Cause:** Authentication is enabled but the request lacks a valid API key.

**Fix:**

1. Include the API key in requests:
   ```bash
   curl http://localhost:11434/broker/status \
     -H "Authorization: Bearer your-api-key"
   ```
2. Or via query parameter: `?api_key=your-api-key`
3. Check `auth.api_keys` in your `broker.yaml` matches the key you are using
4. To disable auth: set `auth.enabled: false` in config

### Rate limited (429 responses)

**Symptom:** Requests return `429 Too Many Requests`.

**Cause:** Per-IP rate limit exceeded.

**Fix:**

1. Wait and retry (the `Retry-After` header indicates how long)
2. Increase the limit in config:
   ```yaml
   rate_limit:
     enabled: true
     requests_per_minute: 120   # default is 60
     burst: 20                  # default is 10
   ```
3. To disable rate limiting: set `rate_limit.enabled: false`

## Dashboard

### Dashboard won't launch

**Symptom:** `ModuleNotFoundError: No module named 'textual'` when running `python -m bastion.dashboard`.

**Cause:** Dashboard dependencies not installed.

**Fix:**

```bash
pip install -e ".[dashboard]"
```

## Persistence

### Persistence database errors

**Symptom:** Errors about SQLite database on startup.

**Cause:** Database file is corrupted or permissions are wrong.

**Fix:**

1. Check the database path: `~/.local/share/bastion/bastion.db`
2. Verify permissions: `ls -la ~/.local/share/bastion/`
3. If corrupted, remove and let BASTION recreate:
   ```bash
   mv ~/.local/share/bastion/bastion.db ~/.local/share/bastion/bastion.db.bak
   ```
4. To disable persistence: set `persistence.enabled: false` in config

## Two-Port Mode

### Two-port mode not working

**Symptom:** Admin endpoints (`/broker/*`) not accessible on the admin port.

**Cause:** Admin port not configured or firewall blocking.

**Fix:**

1. Start with `--admin-port`:
   ```bash
   bastion --admin-port 9999
   ```
2. Or set in config:
   ```yaml
   server:
     admin_port: 9999
   ```
3. Verify: `curl http://localhost:9999/broker/status`
4. Check firewall allows the admin port
