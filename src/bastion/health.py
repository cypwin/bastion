"""GPU health monitoring — public API for BASTION components.

Delegates to the configured GPU backend (see :mod:`bastion.gpu`).
All functions maintain their original signatures for backward compatibility.

All queries use async operations with configurable timeouts and graceful
fallbacks — fields that fail to query are left as ``None``.
"""

from __future__ import annotations

import logging

from bastion.gpu import get_backend
from bastion.metrics import update_gpu_temperature
from bastion.models import GPUConfig, GPUStatus

logger = logging.getLogger(__name__)


async def query_gpu_status(timeout_seconds: int = 5) -> GPUStatus:
    """Query all GPU metrics via the configured backend.

    Returns a :class:`GPUStatus` with whatever fields are available.
    Fields that fail to query are left as ``None`` (graceful degradation).
    """
    return await get_backend().query_status(timeout_seconds)


async def check_gpu_safe(config: GPUConfig) -> tuple[bool, str]:
    """Check if GPU is within safe operating limits.

    Returns
    -------
    tuple[bool, str]
        (is_safe, reason). If unsafe, reason explains why.
    """
    status = await query_gpu_status()

    # Tier-0 dead-metric activation (spec 5.1 row 357): the die temperature is
    # already in hand here on the fast cadence (this is the periodic chokepoint
    # that fetches GPUStatus every scheduler tick). Publish it guarded on
    # not-None so StubBackend / non-NVIDIA hosts skip the gauge rather than
    # emitting a misleading 0.
    if status.temperature_c is not None:
        update_gpu_temperature(status.temperature_c)

    if status.temperature_c is not None and status.temperature_c > config.max_temperature_c:
        return False, (
            f"GPU temperature {status.temperature_c}\u00b0C"
            f" exceeds limit {config.max_temperature_c}\u00b0C"
        )

    if status.power_draw_watts is not None and status.power_draw_watts > config.max_power_watts:
        return False, (
            f"GPU power {status.power_draw_watts}W"
            f" exceeds limit {config.max_power_watts}W"
        )

    if status.vram_used_mb is not None and status.vram_total_mb is not None:
        pct = (status.vram_used_mb / status.vram_total_mb) * 100
        if pct > 95:
            return False, f"VRAM utilization {pct:.1f}% exceeds 95% safety limit"

    return True, "OK"


async def get_vram_free_gb() -> float | None:
    """Query free VRAM in GB. Returns None if GPU backend unavailable."""
    status = await query_gpu_status()
    if status.vram_free_mb is not None:
        return status.vram_free_mb / 1024.0
    return None
