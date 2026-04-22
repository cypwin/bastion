# Hardware Guide

## Supported GPUs

BASTION works with NVIDIA GPUs that support CUDA and have working `nvidia-smi`.

### Tested

| GPU | VRAM | Status | Notes |
|-----|------|--------|-------|
| RTX 5090 | 32 GB | Fully tested | Primary development hardware |

### Expected to Work

| GPU Family | VRAM Range | Notes |
|------------|-----------|-------|
| RTX 40-series | 8-24 GB | Consumer desktop, widely available |
| RTX 30-series | 8-24 GB | Previous generation, well-supported |
| RTX 20-series | 8-11 GB | Minimum viable for small models |
| A100 | 40-80 GB | Data center, HBM2e memory |
| A6000 | 48 GB | Professional workstation |
| L40/L4 | 24-48 GB | Inference-optimized data center |

### Not Supported

- **AMD GPUs (ROCm)** -- Ollama supports ROCm but BASTION's GPU monitoring uses nvidia-smi
- **Apple Silicon** -- Ollama runs natively but BASTION's crash prevention is NVIDIA-specific
- **Intel Arc** -- not tested, no driver integration
- **CPU-only** -- BASTION starts but GPU safety features are disabled

## VRAM Requirements

### How VRAM Budget Works

BASTION reserves headroom from your total VRAM:

```
Usable VRAM = Total VRAM - Headroom (default 6 GB)
```

The headroom covers: OS display, CUDA runtime, KV cache growth during inference, and a safety margin. Configure via `gpu.headroom_gb` in `broker.yaml`.

### Model Size vs VRAM

Rough estimates for quantized (Q4_K_M) models:

| Parameter Count | Approximate VRAM | Example Models |
|----------------|-----------------|----------------|
| 1-3B | 1-2 GB | qwen3:1.7b, llama3.2:1b, phi-3:mini |
| 7-8B | 4-5 GB | llama3.1:8b, mistral:7b, qwen3:8b |
| 13-14B | 8-10 GB | llama2:13b, qwen3:14b |
| 30-34B | 18-20 GB | codellama:34b |
| 70B | 38-42 GB | llama3.1:70b (needs 48+ GB GPU) |

### Configuration by GPU Size

#### 8 GB GPU (RTX 3060 8GB, RTX 4060)

```yaml
gpu:
  total_vram_gb: 0     # auto-detect
  headroom_gb: 2       # smaller headroom for small GPUs

scheduler:
  cooldown_seconds: 3.0                # longer cooldown
  swap_rate_warn_threshold: 3          # more conservative
  swap_rate_critical_threshold: 4
  max_concurrent_dispatches: 1         # single dispatch only
```

Recommendation: stick to one 7B model. Multi-model workflows will queue heavily.

#### 12 GB GPU (RTX 3060 12GB, RTX 4070)

```yaml
gpu:
  total_vram_gb: 0     # auto-detect
  headroom_gb: 3

scheduler:
  cooldown_seconds: 2.5
  swap_rate_warn_threshold: 3
  swap_rate_critical_threshold: 5
  max_concurrent_dispatches: 2
```

Can run one 7-8B model comfortably, or two small (1-3B) models concurrently.

#### 24 GB GPU (RTX 3090, RTX 4090)

```yaml
gpu:
  total_vram_gb: 0     # auto-detect
  headroom_gb: 6       # default

scheduler:
  cooldown_seconds: 2.0
  swap_rate_warn_threshold: 4
  swap_rate_critical_threshold: 6
  max_concurrent_dispatches: 3
```

Sweet spot for multi-model workflows. Can run 2-3 models (7-8B each) concurrently within budget.

#### 32+ GB GPU (RTX 5090, A6000)

```yaml
gpu:
  total_vram_gb: 0     # auto-detect
  headroom_gb: 8

scheduler:
  cooldown_seconds: 2.0
  swap_rate_warn_threshold: 4
  swap_rate_critical_threshold: 6
  max_concurrent_dispatches: 3
```

Room for larger models (13-14B) or multiple 7-8B models simultaneously.

## Running bastion --validate

The pre-flight validator checks your GPU hardware automatically:

```bash
bastion --validate
```

This reports your GPU name, VRAM, driver version, and whether a known profile exists. See [Getting Started](getting-started.md#7-validate-your-setup) for full details.

## Running bastion --stress-test

To discover your GPU's actual safe operating limits:

```bash
bastion --stress-test
```

The stress calibrator runs 5 phases (baseline, single load, swap ramp, concurrent load, recovery) and writes a calibration profile to `~/.config/bastion/gpu-profile.yaml`. BASTION uses this profile at startup for hardware-tuned safety limits.

**Warning:** The stress test pushes your GPU through rapid model swaps. Save all work and close other GPU-intensive applications before running.

## Overheating and Thermal Safety

BASTION monitors GPU temperature via nvidia-smi. When the temperature exceeds `gpu.max_temperature_c` (default: 83C), the scheduler pauses model loads until the GPU cools down.

To monitor temperature:

```bash
curl http://localhost:11434/broker/status | python -m json.tool | grep temperature
```

To adjust the threshold:

```yaml
gpu:
  max_temperature_c: 80   # lower for extra safety margin
```

The watchdog also monitors power draw against `gpu.max_power_watts` (default: 300W, auto-detected from nvidia-smi TDP at startup).
