# Crash Prevention: How BASTION Protects GPU Inference

## The Problem

Running multiple LLM inference requests concurrently on NVIDIA GPUs can cause system instability, including hard reboots. This is not a hardware defect -- it is a consequence of how model loading interacts with GPU memory management under concurrent access patterns.

The failure mode is specific: rapid model load/unload cycling (swapping one model out and another in repeatedly) triggers PCIe bus-mastered DMA power transients. After approximately 60 rapid swaps in a short window (roughly 8-9 per minute sustained), the system hits a protection threshold and reboots. The audible relay click before each crash suggests a hardware protection circuit is tripping.

This problem is invisible in single-model workloads. It appears when multiple agents, pipelines, or users share a single GPU and request different models in rapid succession.

## Investigation Methodology

The root cause was identified through systematic elimination across 9 crash events:

1. **Hardware testing**: GPU stress tests (gpu-burn) passed at maximum power draw with zero errors. PCIe AER logs showed no bus errors. MCE logs were clean. Temperature never exceeded safe limits (39-57C at crash time). This ruled out hardware failure.

2. **Crash pattern analysis**: All crashes occurred during Ollama model loading, never during inference. Crashes 7-9 showed accelerating frequency (3 crashes in 3 hours), correlating with a configuration change that forced model cycling.

3. **Configuration archaeology**: A critical discovery was that `OLLAMA_MMAP=false`, believed to disable memory-mapped model loading, was never a valid Ollama environment variable. The pull request to add it (ollama/ollama#6854) was never merged. The env var had been silently ignored across all 9 crashes.

4. **Swap rate correlation**: Crash events correlated with model swap frequency, not with any specific model, VRAM usage level, or temperature. The system was stable below 4 swaps/minute and consistently crashed above 8 swaps/minute.

## The mmap Discovery

When Ollama loads a model, it can use either of two strategies:

- **mmap (memory-mapped)**: Map the model file directly into virtual address space. The OS pages data in from disk on demand via PCIe DMA transfers. Fast startup, but each load triggers bus-mastered DMA from storage to GPU memory.

- **Direct read**: Read the model file into allocated GPU memory sequentially. Slower startup, but more predictable memory behavior.

Ollama defaults to `mmap = true`. The only way to control this is per-request via the `use_mmap` option in the API request body. There is no environment variable, no config file option, and no command-line flag.

With mmap enabled, each model load triggers PCIe bus-mastered DMA transfers. When models are rapidly cycled (loaded, unloaded, loaded again), these DMA transfers create power transients on the PCIe bus. Under sustained rapid cycling, something -- likely VRM thermal limits, CUDA memory fragmentation, or PSU transient response -- hits a protection threshold.

## Swap Rate Analysis

Analyzing the 9 crash events revealed a clear relationship between model swap frequency and system stability:

| Swap Rate | Observed Behavior |
|-----------|-------------------|
| < 4 swaps/min | Stable operation across all test sessions |
| 4-6 swaps/min | Warning zone; occasional GPU driver slowdowns |
| 6-8 swaps/min | Danger zone; nvidia-smi timeouts begin appearing |
| > 8 swaps/min | Crash zone; system reboots within 4-6 minutes |

The crash threshold is not a hard line -- it depends on model sizes, VRAM pressure, and ambient conditions. BASTION's rate limiter uses conservative thresholds well below the observed danger zone.

## BASTION's Prevention Mechanisms

BASTION addresses GPU crash prevention through four complementary layers:

### 1. use_mmap:false Injection

Every request that passes through BASTION has `use_mmap: false` injected into the `options` dictionary, regardless of what the client specified. This is implemented in the proxy layer and applies to all Ollama API calls (`/api/generate`, `/api/chat`, `/api/embed`).

This eliminates the PCIe DMA power transients that are the primary crash trigger. With mmap disabled, model loading uses sequential reads instead of demand-paged DMA, producing a smooth and predictable power draw.

Clients do not need to cooperate -- the injection is transparent and unconditional.

### 2. VRAM Budget Enforcement

BASTION maintains a VRAM budget (configurable via `gpu.max_vram_gb`) that reserves headroom for OS overhead, display, CUDA runtime, and KV cache growth. The VRAMManager uses an assume/confirm/forget pattern to eliminate TOCTOU races:

- **Reserve**: Deduct estimated VRAM atomically before starting an async model load
- **Confirm**: Mark the reservation as allocated after successful load
- **Release**: Return reserved VRAM to the pool on failure or TTL expiry

This prevents VRAM overcommit, which compounds the swap problem by forcing the system to cycle models more frequently as memory pressure increases.

### 3. Serialized Model Scheduling

The scheduler serializes model swap operations. Only one model transition (load or unload) can happen at a time. Concurrent inference to already-loaded models is allowed, but new model loads are queued and dispatched one at a time.

This prevents the most dangerous pattern: multiple concurrent model loads competing for VRAM and PCIe bandwidth simultaneously.

The affinity queue groups requests by model, reducing total swap count from O(N * M) (N requests across M models) to O(M) by batching same-model requests together.

### 4. Cooldown Periods

A mandatory cooldown period (default 2 seconds) is enforced between model transitions. The cooldown is dynamic and escalates based on recent swap frequency:

```
Normal   (< 4 swaps/min):  2.0s cooldown
Warning  (4-5 swaps/min):  5.0s cooldown
Critical (>= 6 swaps/min): 10.0s cooldown
```

The swap rate limiter uses a rolling window of swap timestamps to calculate the current rate. Level transitions are logged for operational visibility.

For co-resident models (already loaded in VRAM), the cooldown is skipped entirely -- switching between loaded models does not trigger a model load and carries no crash risk.

## Monitoring: What to Watch

BASTION exposes several signals relevant to crash prevention:

### nvidia-smi Timeout
The GPU lockup detector runs `nvidia-smi` on a configurable interval (default 10 seconds) with a 5-second timeout. A timeout suggests the GPU driver is wedged, which is a precursor to crashes. Three consecutive timeouts trigger automatic scheduler drain.

### Swap Rate
The `/broker/status` endpoint reports `total_model_swaps`. A sustained rate above 4 swaps/minute warrants investigation. The Prometheus metric `bastion_model_swap_total` enables alerting on swap rate.

### VRAM Utilization
Monitor `bastion_vram_used_bytes` relative to the configured budget. Sustained operation above 90% of budget increases eviction frequency and raises crash risk.

### GPU Temperature
While temperature was not the root cause in the investigated crashes, high temperatures reduce the thermal margin for PCIe power transients. The watchdog pauses scheduling when temperature exceeds the configured threshold.

### Circuit Breaker State
A circuit breaker in `open` state means Ollama is unreachable. Requests during this period are fast-failed with 503 rather than piling up in the queue. The breaker transitions through `half-open` (probe with one request) before returning to `closed`.

## Summary

GPU inference crashes from rapid model cycling are preventable. The key insight is that the crash trigger is not any single model load, but the cumulative effect of rapid PCIe DMA power transients from memory-mapped model loading. BASTION prevents this through a defense-in-depth approach: disabling mmap at the request level, enforcing VRAM budgets to reduce eviction pressure, serializing model transitions, and dynamically adjusting cooldown based on observed swap rates.
