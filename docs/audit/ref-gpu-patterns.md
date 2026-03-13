# the-batch-client GPU Patterns Adapted for BASTION

> Documents which the-batch-client patterns were extracted into BASTION and how they
> were adapted. Use this as a reference when the two codebases need to stay in sync.

## Source Files and What Was Extracted

### src/gpu_guard.py -> bastion/health.py

**What**: nvidia-smi subprocess queries for temperature, VRAM, power draw.

**Pattern**: All queries use `subprocess.run()` with 5-second timeouts, `capture_output=True`,
CSV output format (`--format=csv,noheader,nounits`), and graceful fallback to `None`.

**Adaptation**:
- the-batch-client makes separate subprocess calls per metric (temperature, vram_used, etc.)
- BASTION queries ALL metrics in a single nvidia-smi call for efficiency:
  `--query-gpu=temperature.gpu,memory.used,memory.free,memory.total,power.draw`
- Both return None on failure (nvidia-smi unavailable, timeout, parse error)

**Also from gpu_guard.py**: `get_loaded_models()` uses httpx to query Ollama `/api/ps`.
This pattern is in `bastion/vram.py` (async httpx instead of sync).

### src/ollama_client.py -> bastion/proxy.py

**What**: `use_mmap: false` injection and streaming patterns.

**Key pattern** -- `_BASE_RUNNER_OPTIONS`:
```python
# the-batch-client (ollama_client.py:75)
_BASE_RUNNER_OPTIONS: Dict[str, Any] = {"use_mmap": False}
# Merged into every generate/chat call's "options" dict
```

BASTION equivalent in `proxy.py`:
```python
# Injects into request body before forwarding to Ollama
if self.config.request_overrides.use_mmap is False:
    options = payload.get("options", {})
    if "use_mmap" not in options:
        options["use_mmap"] = False
        payload["options"] = options
```

**Key difference**: the-batch-client only protects Python callers using `OllamaClient`.
BASTION protects ALL traffic (including `ollama run`, Claude Code MCP, any HTTP client).

**Streaming**: the-batch-client's `ollama_client.py` uses `stream=False` (waits for full response).
BASTION's `proxy.py` must support `stream=True` (NDJSON passthrough) because `ollama run`
and other clients expect real-time token streaming.

### src/ollama_client.py -> bastion/vram.py (unload pattern)

**What**: Model unloading via `keep_alive=0`.

```python
# the-batch-client pattern (ollama_client.py:418)
resp = self._http.post(
    f"{self.base_url}/api/generate",
    json={"model": target_model, "keep_alive": 0},
    timeout=10.0,
)
```

BASTION uses the same pattern in `vram.py:unload_model()` (async version).

### config/models.yaml -> config/broker.yaml

**What**: Model VRAM sizes, GPU profiles, VRAM budgets, model loading gate config.

**Extracted**:
- All model VRAM sizes (primary, fast, council trio, embedding, etc.)
- GPU safety thresholds (26 GB budget = 32 GB - 6 GB headroom)
- Temperature limit (82C) and power limit (450W)
- `always_allowed` concept for embedding models

**Not extracted** (the-batch-client-specific):
- `stage_assignments` (pipeline-stage-to-model mapping)
- `gpu_profiles` with `conflicts` lists (BASTION uses dynamic VRAM budgeting instead)
- `allowed_combinations` (BASTION's scheduler handles this dynamically)
- `jury` and `jury_cpu` sections (the-batch-client-specific roles)

### src/vram_queue.py -> bastion/vram.py (TOCTOU prevention)

**What**: Cross-process VRAM reservation to prevent race conditions.

**the-batch-client approach**: File-based JSON ledger at `/tmp/swarm_vram_queue.json` with
`filelock` for cross-process coordination. Each caller acquires a "slot" reservation
before loading a model.

**BASTION approach**: Centralized — since ALL traffic goes through the broker, there's
no TOCTOU race. The scheduler is the single serialization point. The broker's
`can_load_model()` checks both Ollama `/api/ps` and nvidia-smi, then makes a
single-threaded decision.

**Trade-off**: the-batch-client's file lock works without a central process. BASTION requires
the broker to be running. If the broker is down, clients hit Ollama directly (no safety).

### src/ollama_client.py -> bastion/proxy.py (sandbox proxy)

**What**: SOCKS5 proxy detection for Claude Code sandbox compatibility.

```python
# the-batch-client (ollama_client.py:46)
def _get_sandbox_proxy() -> Optional[str]:
    if os.environ.get("SANDBOX_RUNTIME") != "1":
        return None
    socks = os.environ.get("ALL_PROXY", "")
    if socks.startswith("socks5"):
        return socks.replace("socks5h://", "socks5://")
    return None
```

**Not needed in BASTION**: The broker runs as a systemd service (not inside Claude Code's
sandbox). Clients inside the sandbox connect to `localhost:11434` which is the broker
(allowed by sandbox network rules). The broker then connects to Ollama at `localhost:11435`
directly (no sandbox restrictions on the broker process).

## Patterns NOT Adapted (the-batch-client-specific)

| Pattern | Why Not Needed |
|---|---|
| `GPUGuard` filelock mutex | BASTION is the single serialization point |
| `gpu_session.py` model loading gate | Replaced by broker's VRAM budget enforcement |
| `ModelRegistry` role-based lookup | BASTION uses model names directly (no role abstraction) |
| `VRAMQueue` JSON ledger | Centralized broker eliminates cross-process coordination |
| `_get_sandbox_proxy()` | Broker runs outside sandbox as systemd service |
| `generate_batch()` slot hold | Broker's scheduler handles batch model affinity natively |

## Keeping in Sync

When the-batch-client's GPU safety code changes, check:
1. Model VRAM sizes in `config/models.yaml` -- update `config/broker.yaml`
2. Safety thresholds (temperature, power, headroom) -- update broker.yaml `gpu:` section
3. New nvidia-smi query patterns -- update `bastion/health.py`
4. New Ollama API patterns -- update `bastion/proxy.py`

When BASTION is running, the-batch-client's internal GPU safety stack becomes redundant
for Ollama traffic. the-batch-client can simplify to a thin broker status check:
```python
# Future: replace GPUSession/VRAMQueue with broker health check
resp = httpx.get("http://localhost:11434/broker/status")
if resp.json()["state"] != "running":
    # fallback to local safety checks
```
