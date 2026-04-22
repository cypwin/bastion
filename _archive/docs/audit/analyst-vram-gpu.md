# VRAM & GPU Analyst Report -- BASTION Audit

**Generated**: 2026-03-13
**Analyst**: VRAM & GPU Analyst
**Focus**: GPU telemetry, VRAM prediction, nvidia-smi/Ollama fusion, model size estimation, multi-GPU readiness, eviction optimization, crash prevention

---

## Executive Summary

BASTION's GPU/VRAM subsystem is architecturally sound and reflects hard-won lessons from nine GPU crash events. The four-layer defense (use_mmap injection, VRAM budget enforcement, serialized scheduling, dynamic cooldown) is well-implemented and rooted in forensic evidence. However, the system collects significantly more GPU telemetry than it surfaces, discards potentially valuable data from both nvidia-smi and Ollama, and has several areas where predictive capabilities could be built from existing infrastructure. The multi-GPU plan exists but the current single-GPU architecture is deeply embedded, requiring substantial refactoring for multi-GPU support.

**Key findings**:
- **5 categories of GPU telemetry collected but not surfaced** (utilization trends, power draw history, swap rate state, VRAM convergence timing, cache staleness)
- **3 nvidia-smi fields available but not queried** (GPU utilization %, fan speed, clock speeds)
- **Ollama `/api/ps` details dict captured but discarded** (contains quantization, parameter count, family)
- **Model size estimation is crude** -- fuzzy name matching or flat 10GB default; could use Ollama `/api/show` for precise sizing
- **VRAM journal writes to `/tmp` with no read API** -- crash forensics data trapped in ephemeral storage
- **Multi-GPU requires extending 7+ modules** -- nvidia-smi query, GPUStatus model, VRAMTracker, VRAMManager, scheduler, config, dashboard
- **3 additional crash prevention measures** could complement use_mmap: PCIe power state management, CUDA memory pool pre-allocation, thermal ramp detection

---

## 1. GPU Telemetry: Collected but Not Surfaced

### 1.1 What Is Queried from nvidia-smi

`health.py:query_gpu_status()` queries five fields in a single call:

```
--query-gpu=temperature.gpu,memory.used,memory.free,memory.total,power.draw
```

These populate `GPUStatus`:
| Field | Collected | Surfaced in `/broker/status` | Surfaced in `/broker/health` | In Prometheus |
|-------|-----------|------------------------------|------------------------------|---------------|
| `temperature_c` | Yes | Yes (raw) | Yes (raw + safe check) | `bastion_gpu_temperature_celsius` (defined, **never updated**) |
| `vram_used_mb` | Yes | Yes (raw) | Yes (raw) | `bastion_vram_used_bytes` (defined, **never updated**) |
| `vram_free_mb` | Yes | Yes (raw) | Yes (raw) | Not tracked |
| `vram_total_mb` | Yes | Yes (raw) | Yes (raw) | Not tracked |
| `power_draw_watts` | Yes | Yes (raw) | Yes (raw) | Not tracked |

**Critical gap**: The Prometheus gauge metrics `VRAM_USED_BYTES` and `GPU_TEMPERATURE` are *defined* in `metrics.py` (lines 162-170) with helper functions `update_vram_usage()` and `update_gpu_temperature()`, but **no code anywhere calls these helpers**. The gauges are always zero. This means Prometheus-based alerting on GPU temperature or VRAM usage is non-functional.

**File references**:
- `/home/user/BASTION/src/bastion/health.py` lines 40-71 (nvidia-smi query)
- `/home/user/BASTION/src/bastion/metrics.py` lines 162-170 (defined gauges)
- `/home/user/BASTION/src/bastion/metrics.py` lines 328-348 (unused helper functions)

### 1.2 Computed Properties Never Exposed

`GPUStatus` has two computed properties that are lost during serialization:

1. **`vram_utilization_pct`** (line 396-399 of `models.py`): Computes `(vram_used_mb / vram_total_mb) * 100`. Never appears in any API response because Pydantic `model_dump()` excludes `@property` fields by default.

2. **`is_safe(gpu_config)`** (lines 401-413 of `models.py`): Checks temperature and VRAM against thresholds. Used internally by the scheduler but never surfaced. The `/broker/health` endpoint computes safety independently via `check_gpu_safe()` instead of using this method, creating a subtle code duplication.

**File reference**: `/home/user/BASTION/src/bastion/models.py` lines 387-414

### 1.3 nvidia-smi Fields Available but Not Queried

nvidia-smi supports many additional fields that could enhance monitoring:

| Available Field | Query Flag | Use Case |
|-----------------|-----------|----------|
| `utilization.gpu` | `--query-gpu=utilization.gpu` | Compute utilization trend (0-100%) |
| `fan.speed` | `--query-gpu=fan.speed` | Early thermal warning (fan ramp before temp spike) |
| `clocks.current.graphics` | `--query-gpu=clocks.current.graphics` | Detect GPU throttling |
| `clocks.max.graphics` | `--query-gpu=clocks.max.graphics` | Compute throttle ratio |
| `pstate` | `--query-gpu=pstate` | Performance state (P0=max, P8=idle) |
| `power.limit` | `--query-gpu=power.limit` | Compare draw vs limit for headroom |
| `pcie.link.gen.current` | `--query-gpu=pcie.link.gen.current` | Detect PCIe degradation |
| `ecc.errors.corrected.volatile.total` | `--query-gpu=ecc.errors.corrected.volatile.total` | Memory error accumulation |

Adding these to the existing single nvidia-smi call has near-zero cost (same subprocess, slightly more CSV parsing). The most valuable additions would be `utilization.gpu` (for load-based scheduling), `clocks.current.graphics` (for throttle detection), and `pcie.link.gen.current` (for PCIe health after the crash investigation identified PCIe power transients as the crash vector).

### 1.4 Watchdog's Independent GPU Query

`watchdog.py:ProcessMonitor._check_gpu()` runs a *separate* nvidia-smi call (line 285-288) that only queries `temperature.gpu`. This creates two issues:

1. **Redundant subprocess execution**: The watchdog polls every 10 seconds, and the scheduler/health module also queries nvidia-smi for every scheduling tick and every `/broker/health` request. These are independent calls.

2. **Data discrepancy window**: The watchdog and health module can see different GPU states because they query at different times.

A shared GPU status cache with a short TTL (similar to `ResidencyCache`) could eliminate redundant calls and provide a consistent view.

**File reference**: `/home/user/BASTION/src/bastion/watchdog.py` lines 276-325

---

## 2. VRAM Prediction Capabilities

### 2.1 What Data Exists for Prediction

BASTION currently tracks:
- **Per-model VRAM sizes** in config (`ModelInfo.vram_gb` -- 13 models with hand-measured values)
- **Real-time VRAM state** from nvidia-smi (used, free, total)
- **Model residency** from Ollama `/api/ps` (which models are loaded)
- **VRAM journal** snapshots at `/tmp/bastion-vram-journal.jsonl` (timestamped events with GPU state)
- **Swap history** in scheduler's `_swap_timestamps` deque (rolling window)
- **VRAMManager ledger** (allocated, reserved, available bytes with per-model breakdown)

### 2.2 Predictive Capabilities That Could Be Built

**A. VRAM Usage Forecasting**

The VRAM journal (`log_vram_snapshot()` in `vram.py` lines 279-307) already writes timestamped snapshots for events like `model_load_approved`, `model_unload`, `pre_swap`, and `tick`. These contain:
```json
{
    "timestamp": "2026-03-13T...",
    "event": "pre_swap",
    "gpu_vram_used_mb": 18432,
    "gpu_vram_free_mb": 14336,
    "gpu_temp_c": 52,
    "loaded_models": [{"name": "qwen3:14b", "vram_gb": 9.3}],
    "total_loaded_vram_gb": 9.3
}
```

This data could feed a simple model: given the current queue (models with pending requests), predict VRAM state N seconds in the future. The scheduler already knows the model sequence from intent declarations (`IntentDeclaration.model_sequence`), so it could pre-compute whether the planned sequence fits within budget.

**B. Model Swap Cost Prediction**

The scheduler tracks swap count but not swap *duration*. The Prometheus metric `MODEL_SWAP_DURATION` is defined but never populated (same pattern as GPU temperature gauge). By timing the interval between `pre_swap` journal events and the subsequent `model_load_approved` events, the system could build a per-model swap cost table:

| Model | Avg Load Time | P95 Load Time | Avg Unload Time |
|-------|--------------|---------------|-----------------|
| qwen3:30b | 12.3s | 18.1s | 2.1s |
| qwen3:14b | 6.7s | 9.2s | 1.4s |
| nomic-embed | 0.8s | 1.2s | 0.3s |

This would enable the scheduler to make cost-aware decisions: "swapping from model A to model B takes 15 seconds; is it worth it for 3 queued requests, or should we wait for more to batch?"

**C. Proactive Eviction Scoring**

Currently, `_evict_for_model()` in `scheduler.py` (lines 581-620) evicts based on a simple sort: prefer models with no queued requests, then smallest VRAM first. A richer eviction score could incorporate:
- Time since last request for each model (idle duration)
- Historical request frequency per model (from `_recent_requests` ring buffer)
- Upcoming intent declarations (models about to be needed)
- Swap cost (larger models are more expensive to reload)

### 2.3 KV Cache VRAM Growth

The config tracks `default_num_ctx` per model, but there is no mechanism to estimate KV cache growth during inference. KV cache scales with context length:

```
KV_cache_bytes = 2 * num_layers * num_heads * head_dim * seq_len * precision_bytes
```

For a qwen3:30b at q8_0 with 8192 context: ~2GB KV overhead on top of weight VRAM. The 2GB safety margin in `can_load_model()` (line 214: `required_free = model_vram + 2.0`) is a flat approximation. A per-model KV growth estimator based on `default_num_ctx` would be more accurate, especially for models with large context windows.

**File reference**: `/home/user/BASTION/src/bastion/vram.py` line 214

---

## 3. nvidia-smi + Ollama Fusion: How It Works and What Is Discarded

### 3.1 The Dual-Source Architecture

The fusion architecture is described in the `vram.py` module docstring (lines 1-6):

> *"The key insight from the crash investigation: you MUST check both nvidia-smi (hardware truth) and Ollama /api/ps (model state) because they can disagree -- Ollama may auto-unload models that nvidia-smi still reports as allocated."*

The two sources serve complementary purposes:

| Source | What It Provides | Latency | Reliability |
|--------|-----------------|---------|-------------|
| **nvidia-smi** | Hardware truth: actual VRAM used/free/total, temperature, power | ~50-200ms (subprocess) | Can timeout (5s limit), unavailable without NVIDIA driver |
| **Ollama `/api/ps`** | Model truth: which models are loaded, their names, sizes, details | ~5-50ms (HTTP) | Dependent on Ollama process, can be stale by seconds |

### 3.2 How They Combine

**For model load decisions** (`can_load_model()`, lines 161-230):
1. First checks GPU health via nvidia-smi (temperature gate)
2. Then queries Ollama `/api/ps` for loaded models (budget check against config-known VRAM sizes)
3. Finally cross-checks nvidia-smi free VRAM as a "hard gate" (line 211-224)

This is the critical three-check sequence:
```
Temperature OK? --> Ollama budget OK? --> nvidia-smi free VRAM OK? --> APPROVE
```

**For VRAM snapshots** (`log_vram_snapshot()`, lines 279-307):
- Queries both sources and merges into a single JSON record
- nvidia-smi provides `gpu_vram_used_mb`, `gpu_vram_free_mb`, `gpu_temp_c`
- Ollama provides `loaded_models` list with per-model `vram_gb`

**For reconciliation** (`VRAMManager.reconcile()`, lines 525-563):
- Compares VRAMManager's internal ledger against Ollama `/api/ps`
- Removes stale allocations for models Ollama no longer reports
- This catches Ollama's `keep_alive` auto-unloads that bypass BASTION

### 3.3 Data Discarded in the Fusion

**A. Ollama `/api/ps` `details` dict**

When `get_loaded_models()` (lines 113-135) parses Ollama's response, it captures the `details` dict into `LoadedModel.details`. However, this dict is **never surfaced in any API response**. Ollama's `/api/ps` returns:

```json
{
    "models": [{
        "name": "qwen3:14b",
        "size": 9965000000,
        "details": {
            "family": "qwen2",
            "parameter_size": "14.8B",
            "quantization_level": "Q4_K_M",
            "parent_model": ""
        },
        "digest": "sha256:abc...",
        "expires_at": "2026-03-13T12:00:00Z"
    }]
}
```

BASTION preserves `name`, `size`, and `details` but discards `digest` and `expires_at` entirely. The `expires_at` field is particularly valuable -- it tells you when Ollama will auto-unload the model, which could inform proactive scheduling decisions.

**B. nvidia-smi disaggregation**

The nvidia-smi query only captures total GPU stats. It does not capture per-process VRAM (which would require `nvidia-smi pmon` or `/proc/driver/nvidia/gpus/*/vram_usage`). This means BASTION cannot distinguish between Ollama's VRAM and other GPU consumers (display server, CUDA applications).

**C. The gap between Ollama-reported and nvidia-smi-reported VRAM**

`can_load_model()` computes loaded VRAM from Ollama's config-known sizes (line 197-199) and *separately* checks nvidia-smi free VRAM (line 212-213). These two numbers can diverge significantly:
- Ollama may report 15GB of model weights, but nvidia-smi shows 20GB used (difference = KV cache + CUDA runtime + display)
- The 2GB safety margin (line 214) is meant to cover this gap, but it is a hardcoded constant, not derived from observation

A dynamic safety margin that tracks the historical delta between Ollama-reported and nvidia-smi-reported VRAM would be more robust.

**File reference**: `/home/user/BASTION/src/bastion/vram.py` lines 113-135, 161-230

---

## 4. Model Size Estimation

### 4.1 Current Approach

Model size estimation follows a three-tier fallback in `get_loaded_models()` (lines 123-126):

1. **Config-known models**: If the model name matches a key in `config.models`, use `ModelInfo.vram_gb` (hand-measured values)
2. **Ollama-reported size**: For unknown models, divide `size_bytes` by `1024^3` to get a rough GB estimate
3. **Default estimate**: For completely unknown models via `_estimate_vram()` (lines 309-320), use fuzzy name matching then fall back to `gpu.default_vram_estimate_gb` (10GB)

### 4.2 Problems with Current Estimation

**A. Fuzzy matching is fragile**

`_estimate_vram()` uses bidirectional substring matching:
```python
for known_name, info in self.config.models.items():
    if known_name in model_name or model_name in known_name:
        return info.vram_gb
```

This means `qwen3:14b` matches `qwen3:14b-q4_K_M` (correct) but also `qwen3` matches `qwen3:30b-a3b-instruct-2507-q4_K_M` (incorrect -- 9.8GB estimate for a 19.5GB model). The iteration order of `config.models.items()` determines which match wins, making this non-deterministic for ambiguous prefixes.

**B. Ollama `size_bytes` is model weight size, not VRAM usage**

The `size` field from `/api/ps` reflects the model file size, not actual VRAM consumption. VRAM includes weights + KV cache + CUDA overhead. For quantized models, the relationship between file size and VRAM is roughly:
- Q4_K_M: VRAM ~ 1.1-1.3x file size (overhead for dequantization buffers)
- Q8_0: VRAM ~ 1.05-1.1x file size
- FP16: VRAM ~ 1.0x file size + KV cache

The current code uses file size directly as VRAM estimate, which underestimates for quantized models.

**C. 10GB default is arbitrary**

The `default_vram_estimate_gb: 10.0` config value is a rough median for 7B-14B parameter models. For 70B models, this dramatically underestimates. For small models (1-3B), it dramatically overestimates.

### 4.3 Improvement: Use Ollama `/api/show`

Ollama's `/api/show` endpoint returns detailed model metadata including:
```json
{
    "model_info": {
        "general.parameter_count": 14800000000,
        "general.quantization_version": 2
    },
    "details": {
        "family": "qwen2",
        "parameter_size": "14.8B",
        "quantization_level": "Q4_K_M"
    }
}
```

With parameter count and quantization level, VRAM can be estimated much more accurately:
```
vram_gb = (parameter_count * bits_per_param / 8) / (1024^3) * overhead_factor
```

Where `bits_per_param` depends on quantization (Q4 = 4.5 bits avg, Q8 = 8 bits, FP16 = 16 bits) and `overhead_factor` = 1.1-1.2 for CUDA/KV overhead.

This could be done lazily: on first encounter of an unknown model, query `/api/show`, compute VRAM estimate, and cache it for future requests. This avoids requiring manual config entries for every model.

**File reference**: `/home/user/BASTION/src/bastion/vram.py` lines 309-320

---

## 5. Multi-GPU Support Assessment

### 5.1 The Plan (ref-multi-gpu-plan.md)

The multi-GPU plan (`docs/audit/ref-multi-gpu-plan.md`) outlines a two-phase approach:
- **Phase 1 (S9a)**: Per-GPU VRAM tracking and GPU-aware scheduling
- **Phase 2 (S9b, deferred)**: Distributed broker cluster, load balancer, model migration

Minimum viable S9a requires:
1. Extend `VRAMTracker` and `GPUStatus` for N GPUs
2. Per-GPU `_current_model` and per-GPU cooldown in scheduler
3. New `placement.py` for model-to-GPU assignment
4. `gpu_affinity` config in broker.yaml
5. Tests must pass on single-GPU (backward compatible)

### 5.2 Current Single-GPU Assumptions

The codebase has deep single-GPU assumptions:

**health.py**:
- `query_gpu_status()` parses only the *first line* of nvidia-smi output: `output.strip().split("\n")[0]` (line 64)
- nvidia-smi outputs one CSV line per GPU; multi-GPU simply requires parsing all lines
- Returns a single `GPUStatus` object (would need to become `list[GPUStatus]` or indexed)

**models.py**:
- `GPUConfig` has single values: `total_vram_gb`, `headroom_gb`, `max_temperature_c`, `max_power_watts` (lines 41-47)
- `GPUStatus` is a single GPU snapshot (lines 387-414)
- No GPU index/identifier field on either model

**vram.py**:
- `VRAMTracker` has a single `config.gpu.max_vram_gb` budget (line 146)
- `VRAMManager` has a single `_total` bytes pool (line 372)
- `can_load_model()` checks a single global temperature, single free VRAM value (lines 178-224)

**scheduler.py**:
- Single `_current_model` for affinity tracking (line 78)
- Single `_last_swap_time` for cooldown enforcement (line 79)
- Single `_swap_timestamps` deque for rate limiting (line 87)

**watchdog.py**:
- `_check_gpu()` queries single temperature value (line 286-288)

### 5.3 What Already Exists for Multi-GPU

Surprisingly, a few patterns already support extension:

1. **nvidia-smi multi-GPU output**: The existing query format (`--format=csv,noheader,nounits`) naturally outputs one line per GPU. Parsing all lines instead of `[0]` is a minimal change.

2. **Ollama's device awareness**: Ollama `/api/ps` can return GPU assignment information when running with `CUDA_VISIBLE_DEVICES` or multi-GPU configs.

3. **VRAMManager's per-model tracking**: `_model_allocations` already tracks per-model VRAM (line 377). Extending to per-GPU-per-model is structural but not architectural.

4. **Config extensibility**: `GPUConfig` could gain a `gpus: list[PerGPUConfig]` field with backward compatibility via a property that returns `[self]` when `gpus` is empty.

### 5.4 Effort Estimate

| Component | Change | Complexity |
|-----------|--------|-----------|
| `health.py` | Parse all nvidia-smi lines, return list | Low |
| `models.py` | Add GPU index to `GPUStatus`, `PerGPUConfig` | Low |
| `vram.py` | Per-GPU VRAMTracker + VRAMManager instances | Medium |
| `scheduler.py` | Per-GPU current_model, cooldown, swap timestamps | High |
| New `placement.py` | Model-to-GPU assignment logic | Medium |
| `config.py` / `broker.yaml` | GPU affinity config | Low |
| `dashboard.py` | Per-GPU panels | Medium |
| `server.py` | Wire per-GPU state into status endpoints | Medium |

The scheduler is the hardest part. Currently, the single-GPU scheduler makes decisions in a tight loop: "which model next, does it fit, should I evict?" With multi-GPU, this becomes: "which model on which GPU, considering cross-GPU affinity, per-GPU thermal state, and per-GPU swap rate limits."

---

## 6. VRAM Fragmentation and Model Eviction Optimization

### 6.1 Current Eviction Strategy

Model eviction is handled in two places:

**Proactive eviction** (`scheduler.py` lines 538-562):
After a model swap, if resident model count exceeds `ollama_max_loaded_models` (default 4), excess models are evicted. Eviction order:
1. Exclude: the newly loaded model, `always_allowed` models, reserved models, in-flight models
2. Sort by: (queued_request_count ascending, vram_gb ascending)
3. Evict the first N excess models

**Reactive eviction** (`scheduler.py` lines 581-620):
When `can_load_model()` or `VRAMManager.reserve()` fails, `_evict_for_model()` tries to free space:
1. Same exclusion criteria as proactive
2. Same sort order: (queued_request_count ascending, vram_gb ascending)
3. Evict one at a time, checking `can_load_model()` after each

### 6.2 Optimization Opportunities

**A. Eviction sort order could incorporate more signals**

Current: `(queue_depth, vram_gb)` -- models with no pending requests and small size evicted first.

Better: `(has_active_lease, queue_depth, recency_of_last_request, -vram_gb, reload_cost)`
- Active leases should be weighted heavily (already excluded, but reservations without leases are not)
- Recency: models accessed recently are more likely to be accessed again (temporal locality)
- Inverted VRAM size: evicting one large model frees more space than evicting multiple small ones, reducing total eviction count (and swap count)
- Reload cost: estimated time to reload (proportional to model size, measurable from journal data)

**B. VRAM fragmentation is not tracked**

CUDA memory allocation can fragment over time. Ollama's model loading may allocate non-contiguous blocks. The 2GB safety margin in `can_load_model()` partially addresses this, but there is no measurement of actual fragmentation.

nvidia-smi's `--query-gpu=memory.used,memory.free` gives total values. For fragmentation detection, `nvidia-smi --query-compute-apps=used_memory` could reveal per-process allocation patterns, and repeated failures of `can_load_model()` when budget says there should be space is an indirect fragmentation signal.

**C. Pre-emptive eviction based on queue state**

The scheduler could look ahead: if the queue has requests for models A, B, and C (none resident), and only models A and B fit simultaneously, it could proactively evict to make room for B while loading A, instead of waiting for A's request to complete and then discovering B needs eviction.

### 6.3 VRAM Convergence After Unload

`VRAMManager.wait_for_vram_convergence()` (lines 565-589) polls nvidia-smi until free VRAM stabilizes (delta < 1MB between consecutive reads). This is essential because Ollama's `keep_alive=0` is an *acknowledgement*, not a completion -- VRAM release is asynchronous.

The convergence check has a configurable timeout (default 5s) and interval (default 250ms). In practice, VRAM convergence after unloading a large model (19.5GB qwen3:30b) can take 1-3 seconds. The current timeout is adequate but the convergence data (how long did it actually take?) is not recorded. Tracking convergence duration per-model would be valuable for predicting future swap costs.

**File reference**: `/home/user/BASTION/src/bastion/vram.py` lines 565-589

---

## 7. Historical GPU Data and Trend Analysis

### 7.1 VRAM Journal (Existing but Trapped)

The VRAM journal at `/tmp/bastion-vram-journal.jsonl` is the richest source of historical GPU data. `log_vram_snapshot()` writes entries for:
- `model_load_approved` -- with proposed total VRAM
- `model_unload` -- with confirmation status
- `hard_gate_blocked` -- with free/required VRAM
- `pre_swap` -- with from/to models and queue depth
- `dispatch` -- with model and event metadata
- `tick` -- periodic scheduler heartbeats

**Problems with the journal**:

1. **Ephemeral storage**: `/tmp` is cleared on reboot. After the GPU crashes that motivated BASTION, the forensic data was lost. The journal path is hardcoded (line 303) with no config option.

2. **No read API**: There is no endpoint to query the journal. `docs/audit/scout-data-models.md` identified this gap and recommended a `GET /broker/vram-journal?limit=N` endpoint.

3. **No rotation**: The journal appends indefinitely. Unlike the audit log (which uses `RotatingFileHandler` with 10MB/5 backup rotation), the VRAM journal has no size limit. On a busy system, it could grow to gigabytes.

4. **No indexing**: The JSONL format supports only sequential scan. Finding "all `hard_gate_blocked` events in the last hour" requires reading the entire file.

### 7.2 Audit Log GPU Events

The audit log (`/tmp/bastion-audit.jsonl`) captures:
- `vram_alert` -- when VRAM usage exceeds 85% or 95% of budget (from `get_loaded_vram_gb()`)
- `swap_rate` -- when swap rate level transitions (from `_get_swap_cooldown()`)
- `scheduler_stall` -- when scheduler cannot dispatch (with reason)
- `model_swap` -- with from/to models, queue depth, VRAM before
- `vram_reconciliation` -- when stale allocations are freed

These events could feed a trend analysis system if they were queryable. Currently, both journals write to `/tmp` with no read path.

### 7.3 What Trend Analysis Could Enable

**A. Swap rate trending**: Track swap rate over hours/days. Detect gradual acceleration that approaches crash thresholds before the rate limiter trips.

**B. VRAM pressure trending**: Track how often the 85% and 95% alerts fire. Persistent high-pressure signals that `headroom_gb` should be increased or models should be smaller.

**C. Model popularity analysis**: From the journal and recent requests buffer, determine which models are most frequently requested. Inform `ollama_max_loaded_models` and preload strategy.

**D. Temperature correlation**: Track temperature alongside swap rate and VRAM usage. The crash investigation noted temperature was 39-57C at crash time (low), but long-term trends might reveal thermal accumulation patterns.

**E. Alerting on anomalies**: If typical query_gpu_status latency is 50ms and it suddenly jumps to 2000ms, that could be an early warning of GPU driver issues -- the watchdog already detects timeouts, but a latency trend could warn earlier.

---

## 8. RTX 5090 Crash Prevention: Additional Protective Measures

### 8.1 Current Defense Layers

BASTION implements four crash prevention layers, documented in `docs/audit/ref-crash-prevention.md`:

1. **`use_mmap: false` injection** (`proxy.py` lines 150-152): Eliminates PCIe DMA power transients from memory-mapped model loading
2. **VRAM budget enforcement** (`vram.py` `can_load_model()` + `VRAMManager`): Prevents overcommit that forces eviction cycling
3. **Serialized model scheduling** (scheduler load semaphore + blocking dispatch): One model transition at a time
4. **Dynamic cooldown** (`_get_swap_cooldown()` lines 130-176): Escalates from 2s to 5s to 10s based on rolling swap rate

### 8.2 Additional Protective Measures

**A. PCIe Power State Management**

nvidia-smi can set power limits: `nvidia-smi -pl <watts>`. During model transitions, temporarily reducing the power limit (e.g., from 450W to 300W) would reduce the magnitude of PCIe power transients. The crash investigation noted that power transients are the crash vector; reducing peak power during the vulnerable model-load phase directly addresses this.

Implementation: call `nvidia-smi -pl 300` before model load, then `nvidia-smi -pl 450` after. Risk: adds subprocess overhead and requires root/nvidia-smi admin permissions.

**B. CUDA Memory Pool Pre-allocation**

Ollama supports `CUDA_MEMORY_POOL` environment variables. Pre-allocating a CUDA memory pool at Ollama startup (via `CUDA_DEVICE_DEFAULT_PERSISTING_L2_CACHE_PERCENTAGE_LIMIT` or Ollama's internal pool settings) could reduce fragmentation and eliminate dynamic allocation/deallocation transients during model loads.

This is an Ollama-side configuration rather than a BASTION change, but BASTION's watchdog could verify the setting at startup.

**C. Thermal Ramp Detection**

Instead of only gating on absolute temperature (`max_temperature_c: 82`), detect the *rate of temperature change*. A GPU going from 45C to 75C in 30 seconds is more concerning than one sitting stably at 78C. The scheduler could extend cooldown when temperature is rising rapidly, even if it has not hit the absolute threshold.

Implementation: track last N temperature readings, compute `dT/dt`, add a threshold like `max_temp_rise_rate_c_per_min: 30`.

**D. num_ctx Clamping Based on Available VRAM**

Currently, `proxy.py` injects `default_num_ctx` from config if the client does not set it (lines 155-160). But the client can override with any value, and a large `num_ctx` (e.g., 32768) can cause KV cache to consume far more VRAM than the model weights.

A protective measure: clamp `num_ctx` to a maximum based on available VRAM. For example, if only 15GB is free and the model weights are 9.3GB, cap `num_ctx` at a value that keeps KV cache under 5.7GB - margin.

**E. Post-Load VRAM Verification**

After loading a model (scheduler commits reservation), verify that actual nvidia-smi VRAM usage is within expected range. If VRAM jumped by significantly more than the configured `vram_gb` for that model, log a warning and consider tightening the estimate. This catches model updates that change VRAM footprint without config changes.

### 8.3 Dispatch Stagger (Already Implemented)

The `concurrent_dispatch_delay_seconds: 0.1` config (used in `_process_tick()` line 288-289) already staggers concurrent dispatches by 100ms. The docstring explains: *"460W->80W->460W spikes stress VRMs; 100ms delay staggers ramp-up"*. This is a good pattern that could be dynamically adjusted based on power draw readings -- stagger more aggressively when power draw is already high.

---

## 9. Summary of Findings and Recommendations

### Critical (Should Fix)

| # | Finding | Impact | Location |
|---|---------|--------|----------|
| C1 | Prometheus GPU gauges (`VRAM_USED_BYTES`, `GPU_TEMPERATURE`) defined but never updated | GPU alerting via Prometheus is non-functional | `metrics.py` lines 162-170 (definition), no call sites |
| C2 | VRAM journal on `/tmp` is ephemeral, unrotated, and has no read API | Crash forensics data lost on reboot; no operational visibility | `vram.py` line 303 |
| C3 | `_estimate_vram()` fuzzy matching can return wrong model's size for ambiguous prefixes | Could underestimate VRAM for large models, leading to overcommit | `vram.py` lines 309-320 |

### High Value (Should Implement)

| # | Finding | Impact | Location |
|---|---------|--------|----------|
| H1 | Ollama `/api/ps` `expires_at` field discarded | Cannot predict auto-unload timing for proactive scheduling | `vram.py` lines 120-131 |
| H2 | `MODEL_SWAP_DURATION` Prometheus metric defined but never populated | Cannot track/alert on swap performance degradation | `metrics.py` line 173-178 |
| H3 | Model size estimation could use Ollama `/api/show` for unknown models | 10GB flat default causes large errors for small/large models | `vram.py` `_estimate_vram()` |
| H4 | Safety margin (2GB) is hardcoded, not derived from observation | May be too small for large-context models or too large for small ones | `vram.py` line 214 |
| H5 | No temperature rate-of-change detection | Misses rapidly rising temperature as early warning | `health.py` (only absolute threshold) |

### Medium Value (Nice to Have)

| # | Finding | Impact | Location |
|---|---------|--------|----------|
| M1 | Additional nvidia-smi fields (utilization, clocks, pstate) available at near-zero cost | Richer GPU health picture for dashboarding and alerting | `health.py` line 43 |
| M2 | Watchdog runs separate nvidia-smi call from health module | Redundant subprocess, potential state inconsistency | `watchdog.py` lines 285-288 |
| M3 | Eviction sort could incorporate recency, reload cost, and VRAM size inversion | Fewer evictions, lower swap count, better user experience | `scheduler.py` lines 598-599 |
| M4 | VRAM convergence duration not tracked per-model | Cannot predict future swap costs | `vram.py` lines 565-589 |
| M5 | `LoadedModel.details` captured from Ollama but never surfaced | Quantization and parameter info invisible to operators | `vram.py` line 131, `models.py` line 283 |

### Multi-GPU Readiness

| # | Finding | Impact |
|---|---------|--------|
| G1 | `health.py` parses only first GPU line | Blocks multi-GPU monitoring |
| G2 | `GPUConfig` / `GPUStatus` have no GPU index | Cannot distinguish GPUs |
| G3 | Scheduler has single `_current_model` / `_last_swap_time` | Cannot track per-GPU state |
| G4 | VRAMManager has single VRAM pool | Cannot manage per-GPU budgets |
| G5 | nvidia-smi multi-GPU output is already parseable | Low-effort extension point |

---

## Key File Reference

| File | Lines | Role in GPU/VRAM System |
|------|-------|------------------------|
| `/home/user/BASTION/src/bastion/health.py` | 133 | nvidia-smi queries, GPU safety checks |
| `/home/user/BASTION/src/bastion/vram.py` | 616 | VRAM tracking, Ollama fusion, VRAMManager ledger |
| `/home/user/BASTION/src/bastion/models.py` | 528 | GPUStatus, GPUConfig, LoadedModel, ResidencyState |
| `/home/user/BASTION/src/bastion/scheduler.py` | 710 | Eviction logic, swap rate limiter, dispatch |
| `/home/user/BASTION/src/bastion/proxy.py` | 442 | use_mmap injection, num_ctx injection |
| `/home/user/BASTION/src/bastion/watchdog.py` | 326 | GPU lockup detection, health transitions |
| `/home/user/BASTION/src/bastion/metrics.py` | 522 | GPU Prometheus metrics (defined but unused) |
| `/home/user/BASTION/src/bastion/server.py` | 1561 | Status endpoints, VRAM ledger wiring |
| `/home/user/BASTION/config/broker.yaml` | 222 | GPU thresholds, model VRAM sizes, swap rate config |
| `/home/user/BASTION/docs/audit/ref-crash-prevention.md` | 114 | Crash investigation findings and prevention mechanisms |
| `/home/user/BASTION/docs/audit/ref-multi-gpu-plan.md` | 26 | Multi-GPU roadmap |
| `/home/user/BASTION/docs/audit/ref-gpu-patterns.md` | 146 | the-batch-client GPU pattern extraction history |

---

**End of Report**

Generated by VRAM & GPU Analyst
Session: S0 (Audit Phase)
Next: Integration with scheduler analyst, observability analyst, and security analyst findings
