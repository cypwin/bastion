"""GPU health monitoring via nvidia-smi.

nvidia-smi query utilities for GPU health monitoring. These are the proven
query patterns that survived multiple GPU crash investigations.

All queries use async subprocess with 5-second timeouts and graceful fallbacks.
Uses asyncio.create_subprocess_exec() to avoid blocking the event loop —
synchronous subprocess.run() was identified as a critical latency source
(see reference/QUEUE_STALENESS_INVESTIGATION.md, Issue #1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bastion.models import GPUConfig, GPUStatus

logger = logging.getLogger(__name__)


async def query_gpu_status(timeout_seconds: int = 5) -> GPUStatus:
    """Query all GPU metrics in a single nvidia-smi call.

    Parameters
    ----------
    timeout_seconds : int
        Subprocess timeout for nvidia-smi (default: 5, configurable via
        gpu.nvidia_smi_timeout_seconds in broker.yaml).

    Returns a GPUStatus with whatever fields are available. Fields that
    fail to query are left as None (graceful degradation).

    .. note::
        Uses asyncio.create_subprocess_exec() instead of subprocess.run()
        to avoid blocking the event loop. The synchronous version was blocking
        all HTTP handling for 50-200ms per call (up to 5s on timeout).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=temperature.gpu,memory.used,memory.free,memory.total,power.draw",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.debug("nvidia-smi timed out after %ds", timeout_seconds)
            return GPUStatus()

        if proc.returncode != 0 or not stdout.strip():
            logger.debug("nvidia-smi returned no data (rc=%d)", proc.returncode)
            return GPUStatus()

        # Parse CSV: "42, 1234, 30766, 32000, 125.5"
        output = stdout.decode()
        parts = [p.strip() for p in output.strip().split("\n")[0].split(",")]
        return GPUStatus(
            temperature_c=_safe_int(parts[0]) if len(parts) > 0 else None,
            vram_used_mb=_safe_int(parts[1]) if len(parts) > 1 else None,
            vram_free_mb=_safe_int(parts[2]) if len(parts) > 2 else None,
            vram_total_mb=_safe_int(parts[3]) if len(parts) > 3 else None,
            power_draw_watts=_safe_float(parts[4]) if len(parts) > 4 else None,
        )
    except FileNotFoundError:
        logger.debug("nvidia-smi not found")
        return GPUStatus()


async def check_gpu_safe(config: GPUConfig) -> tuple[bool, str]:
    """Check if GPU is within safe operating limits.

    Returns
    -------
    tuple[bool, str]
        (is_safe, reason). If unsafe, reason explains why.
    """
    status = await query_gpu_status()

    if status.temperature_c is not None and status.temperature_c > config.max_temperature_c:
        return False, f"GPU temperature {status.temperature_c}°C exceeds limit {config.max_temperature_c}°C"

    if status.power_draw_watts is not None and status.power_draw_watts > config.max_power_watts:
        return False, f"GPU power {status.power_draw_watts}W exceeds limit {config.max_power_watts}W"

    if status.vram_used_mb is not None and status.vram_total_mb is not None:
        pct = (status.vram_used_mb / status.vram_total_mb) * 100
        if pct > 95:
            return False, f"VRAM utilization {pct:.1f}% exceeds 95% safety limit"

    return True, "OK"


async def get_vram_free_gb() -> Optional[float]:
    """Query free VRAM in GB. Returns None if nvidia-smi unavailable."""
    status = await query_gpu_status()
    if status.vram_free_mb is not None:
        return status.vram_free_mb / 1024.0
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(s: str) -> Optional[int]:
    try:
        # Parse through float first — nvidia-smi sometimes returns "1234.0"
        # for memory values, which int() alone cannot handle.
        return int(float(s.strip()))
    except (ValueError, AttributeError):
        return None


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None
