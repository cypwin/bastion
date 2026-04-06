"""Stub GPU backend — no-op for systems without a supported GPU.

All queries return empty/None values.  BASTION still functions as a
proxy and scheduler, but without GPU health gating or VRAM monitoring.
"""

from __future__ import annotations

from bastion.models import GPUStatus


class StubBackend:
    """No-op GPU backend for systems without nvidia-smi."""

    async def query_status(self, timeout_seconds: int = 5) -> GPUStatus:
        return GPUStatus()

    async def get_vram_free_gb(self) -> float | None:
        return None

    def query_processes(self) -> list[dict[str, str]]:
        return []
