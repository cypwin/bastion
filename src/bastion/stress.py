"""GPU stress calibrator for BASTION.

Discovers safe operating limits through gradual ramp-up. Writes a
calibration profile to ~/.config/bastion/gpu-profile.yaml that BASTION
can use at runtime for hardware-tuned safety limits.

Requires BASTION to be running -- tests the full stack.

Usage::

    bastion --stress-test
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from bastion.health import query_gpu_status
from bastion.paths import config_dir


@dataclass
class StressConfig:
    """Configuration for the stress calibrator."""

    bastion_url: str = "http://127.0.0.1:11434"
    thermal_cutoff_pct: float = 0.90       # Stop at 90% of thermal ceiling
    max_inference_latency_s: float = 30.0  # Stop if latency exceeds this
    baseline_duration_s: float = 30.0      # Phase 1 duration
    sample_interval_s: float = 2.0         # GPU sampling interval
    test_prompt: str = "Count from 1 to 20. Be concise."
    max_tokens: int = 100


@dataclass
class PhaseResult:
    """Result of a single calibration phase."""

    phase: str
    success: bool
    data: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class CalibrationResult:
    """Aggregated results from all completed phases."""

    gpu_name: str = ""
    vram_total_mb: int = 0
    driver: str = ""
    phases: list[PhaseResult] = field(default_factory=list)
    calibrated: dict = field(default_factory=dict)


SAFETY_BANNER = """
=================================================================
  BASTION Stress Calibrator
=================================================================

  This will push your GPU through rapid model swaps and high load.

  Before continuing:
  1. Save all open work in other applications
  2. Close other GPU-intensive programs
  3. Ensure no critical processes depend on this GPU

  This test will:
  - Load and unload models rapidly
  - Measure GPU thermal response under swap stress
  - Discover your hardware's safe operating thresholds
  - Take approximately 10-15 minutes

  Results are written to ~/.config/bastion/gpu-profile.yaml
  BASTION can use this profile for hardware-tuned safety limits.

  Type 'I understand' to continue, or Ctrl+C to abort:
=================================================================
"""


async def check_prerequisites(config: StressConfig) -> tuple[bool, str]:
    """Verify BASTION is running and has enough models for testing.

    Returns (ok, message).
    """
    # Check BASTION is running
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.bastion_url}/broker/status", timeout=5.0)
        if resp.status_code != 200:
            return False, f"BASTION responded with HTTP {resp.status_code}"
    except Exception:
        return False, (
            "BASTION is unreachable. Start it first with: bastion\n"
            "The stress test needs to go through the full proxy stack."
        )

    # Check for at least 2 small models
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.bastion_url}/api/tags", timeout=5.0)
        data = resp.json()
        models = data.get("models", [])
        small_models = [
            m["name"] for m in models
            if m.get("size", 0) < 5 * 1024**3  # Under 5 GB
        ]
        if len(small_models) < 2:
            return False, (
                f"Need at least 2 small models for swap testing (found {len(small_models)}).\n"
                "Install small models:\n"
                "  ollama pull qwen3:1.7b\n"
                "  ollama pull llama3.2:1b"
            )
    except Exception:
        return False, "Could not query Ollama models through BASTION."

    return True, f"Ready -- {len(small_models)} small models available"


async def baseline_phase(
    duration_seconds: float = 30.0,
    sample_interval: float = 2.0,
) -> PhaseResult:
    """Phase 1: Measure idle GPU metrics.

    Samples GPU status repeatedly to establish baseline temperature,
    power draw, and VRAM usage.
    """
    temps: list[int] = []
    powers: list[float] = []
    vrams: list[int] = []

    end_time = time.monotonic() + duration_seconds
    while time.monotonic() < end_time:
        status = await query_gpu_status()
        if status.temperature_c is not None:
            temps.append(status.temperature_c)
        if status.power_draw_watts is not None:
            powers.append(status.power_draw_watts)
        if status.vram_used_mb is not None:
            vrams.append(status.vram_used_mb)
        await asyncio.sleep(sample_interval)

    if not temps:
        return PhaseResult(
            phase="baseline",
            success=False,
            error="Could not read GPU temperature -- is nvidia-smi working?",
        )

    return PhaseResult(
        phase="baseline",
        success=True,
        data={
            "idle_temp_c": round(statistics.median(temps)),
            "idle_power_w": round(statistics.median(powers), 1) if powers else 0,
            "vram_in_use_mb": round(statistics.median(vrams)) if vrams else 0,
            "temp_samples": len(temps),
        },
    )


async def single_load_phase(
    bastion_url: str,
    model: str,
    baseline_temp: int,
    test_prompt: str = "Count from 1 to 20. Be concise.",
    max_tokens: int = 100,
) -> PhaseResult:
    """Phase 2: Load one model, run inference, unload.

    Measures load time, inference latency, VRAM usage, and thermal impact.
    """
    data: dict = {}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            # Load + inference (BASTION handles scheduling)
            t0 = time.monotonic()
            resp = await client.post(
                f"{bastion_url}/api/generate",
                json={
                    "model": model,
                    "prompt": test_prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
            t1 = time.monotonic()

            if resp.status_code != 200:
                return PhaseResult(
                    phase="single_load",
                    success=False,
                    error=f"Inference failed with HTTP {resp.status_code}",
                )

            result = resp.json()
            data["inference_latency_s"] = round(t1 - t0, 2)
            data["eval_count"] = result.get("eval_count", 0)

            # Check GPU after load
            status = await query_gpu_status()
            data["peak_vram_mb"] = status.vram_used_mb or 0
            data["thermal_delta_c"] = (status.temperature_c or baseline_temp) - baseline_temp

            # Unload
            await client.post(
                f"{bastion_url}/api/generate",
                json={"model": model, "keep_alive": 0},
            )

    except Exception as e:
        return PhaseResult(phase="single_load", success=False, error=str(e))

    return PhaseResult(phase="single_load", success=True, data=data)


async def swap_ramp_phase(
    bastion_url: str,
    models: list[str],
    thermal_ceiling: int,
    thermal_cutoff_pct: float = 0.90,
    test_prompt: str = "Say hello.",
) -> PhaseResult:
    """Phase 3: Alternate models at decreasing intervals.

    Ramps swap frequency: 10s -> 8s -> 6s -> 4s -> 2s gaps.
    Stops when thermal threshold or swap failure occurs.
    """
    intervals = [10, 8, 6, 4, 2]
    swaps_per_interval = 3
    cutoff_temp = int(thermal_ceiling * thermal_cutoff_pct)
    last_safe_rate: int | None = None
    swap_durations: list[float] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for interval in intervals:
            rate_per_min = 60 // interval
            interval_ok = True

            for swap_idx in range(swaps_per_interval):
                model = models[swap_idx % len(models)]
                t0 = time.monotonic()

                try:
                    resp = await client.post(
                        f"{bastion_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": test_prompt,
                            "stream": False,
                            "options": {"num_predict": 10},
                        },
                    )
                    t1 = time.monotonic()

                    if resp.status_code != 200:
                        interval_ok = False
                        break

                    swap_durations.append(t1 - t0)

                except Exception:
                    interval_ok = False
                    break

                # Check temperature
                status = await query_gpu_status()
                if status.temperature_c and status.temperature_c >= cutoff_temp:
                    return PhaseResult(
                        phase="swap_ramp",
                        success=True,
                        data={
                            "safe_swap_rate_per_min": last_safe_rate or rate_per_min,
                            "stopped_at_interval_s": interval,
                            "stop_reason": f"thermal cutoff ({status.temperature_c}C >= {cutoff_temp}C)",
                            "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
                        },
                    )

                if interval > 2:
                    await asyncio.sleep(interval)

            if not interval_ok:
                return PhaseResult(
                    phase="swap_ramp",
                    success=True,
                    data={
                        "safe_swap_rate_per_min": last_safe_rate or 3,
                        "stopped_at_interval_s": interval,
                        "stop_reason": "swap failed or errored",
                        "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
                    },
                )

            last_safe_rate = rate_per_min

    return PhaseResult(
        phase="swap_ramp",
        success=True,
        data={
            "safe_swap_rate_per_min": last_safe_rate or 3,
            "stopped_at_interval_s": 2,
            "stop_reason": "completed all intervals",
            "swap_duration_avg_s": round(statistics.mean(swap_durations), 2) if swap_durations else 0,
        },
    )


async def concurrent_load_phase(
    bastion_url: str,
    model: str,
    test_prompt: str = "Say hello.",
    max_latency_s: float = 30.0,
) -> PhaseResult:
    """Phase 4: Send concurrent requests to a loaded model.

    Tests 2, 4, then 8 simultaneous requests. Stops at first
    error or latency breach.
    """
    concurrency_levels = [2, 4, 8]
    last_safe_level = 1

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # Pre-load the model
        await client.post(
            f"{bastion_url}/api/generate",
            json={"model": model, "prompt": "warmup", "stream": False,
                  "options": {"num_predict": 5}},
        )

        for level in concurrency_levels:

            async def _single_request() -> float:
                t0 = time.monotonic()
                resp = await client.post(
                    f"{bastion_url}/api/generate",
                    json={"model": model, "prompt": test_prompt, "stream": False,
                          "options": {"num_predict": 20}},
                )
                t1 = time.monotonic()
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                return t1 - t0

            try:
                latencies = await asyncio.gather(
                    *[_single_request() for _ in range(level)]
                )
                p95 = sorted(latencies)[int(len(latencies) * 0.95)]
                if p95 > max_latency_s:
                    return PhaseResult(
                        phase="concurrent_load",
                        success=True,
                        data={
                            "max_concurrent_requests": last_safe_level,
                            "stopped_at_level": level,
                            "stop_reason": f"p95 latency {p95:.1f}s > {max_latency_s}s",
                        },
                    )
                last_safe_level = level
            except Exception as e:
                return PhaseResult(
                    phase="concurrent_load",
                    success=True,
                    data={
                        "max_concurrent_requests": last_safe_level,
                        "stopped_at_level": level,
                        "stop_reason": str(e),
                    },
                )

    return PhaseResult(
        phase="concurrent_load",
        success=True,
        data={
            "max_concurrent_requests": last_safe_level,
            "stopped_at_level": concurrency_levels[-1],
            "stop_reason": "completed all levels",
        },
    )


async def recovery_phase(
    bastion_url: str,
    baseline_temp: int,
    temp_tolerance: int = 3,
    timeout_seconds: float = 120.0,
) -> PhaseResult:
    """Phase 5: Unload everything and wait for GPU to cool down."""
    # Unload all models via admin API
    try:
        async with httpx.AsyncClient() as client:
            status_resp = await client.get(f"{bastion_url}/broker/status", timeout=5.0)
            if status_resp.status_code == 200:
                data = status_resp.json()
                for model_info in data.get("loaded_models", []):
                    model_name = model_info.get("name", "")
                    if model_name:
                        await client.post(
                            f"{bastion_url}/api/generate",
                            json={"model": model_name, "keep_alive": 0},
                            timeout=10.0,
                        )
    except Exception:
        pass  # Best effort unload

    # Wait for cooldown
    target_temp = baseline_temp + temp_tolerance
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_seconds:
        status = await query_gpu_status()
        if status.temperature_c and status.temperature_c <= target_temp:
            return PhaseResult(
                phase="recovery",
                success=True,
                data={
                    "cooldown_duration_s": round(time.monotonic() - t0, 1),
                    "final_temp_c": status.temperature_c,
                },
            )
        await asyncio.sleep(2.0)

    status = await query_gpu_status()
    return PhaseResult(
        phase="recovery",
        success=True,
        data={
            "cooldown_duration_s": round(time.monotonic() - t0, 1),
            "final_temp_c": status.temperature_c,
            "note": "timeout -- GPU did not fully cool down",
        },
    )


def write_profile(result: CalibrationResult) -> Path:
    """Write calibration results to gpu-profile.yaml.

    Returns the path to the written file.
    """
    profile = {
        "gpu": {
            "name": result.gpu_name,
            "vram_total_mb": result.vram_total_mb,
            "driver": result.driver,
        },
        "calibrated": result.calibrated,
        "tested": {
            "date": time.strftime("%Y-%m-%d"),
            "phases_completed": len([p for p in result.phases if p.success]),
            "models_used": result.calibrated.get("models_used", []),
        },
    }

    # Add baseline data if available
    for phase in result.phases:
        if phase.phase == "baseline" and phase.success:
            profile["baseline"] = {
                "idle_temp_c": phase.data.get("idle_temp_c", 0),
                "idle_power_w": phase.data.get("idle_power_w", 0),
                "vram_in_use_mb": phase.data.get("vram_in_use_mb", 0),
            }
            break

    dest = config_dir() / "gpu-profile.yaml"
    header = (
        f"# Auto-generated by 'bastion stress-test'\n"
        f"# Date: {time.strftime('%Y-%m-%d')}\n"
        f"# GPU: {result.gpu_name} ({result.vram_total_mb} MB)\n"
        f"# Driver: {result.driver}\n\n"
    )

    dest.write_text(header + yaml.dump(profile, default_flow_style=False), encoding="utf-8")
    return dest
