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

    async def query_processes(self, timeout_seconds: int = 5) -> list[dict[str, str]]:
        return []

    async def query_process_utilization(self, timeout_seconds: int = 5) -> list[dict]:
        # pmon is an NVIDIA concept; [] is the correct complete value here.
        return []

    async def query_throttle_reasons(self) -> list[str]:
        # NVIDIA concept; the empty list is the correct complete value here.
        return []

    async def query_pcie_throughput(self) -> tuple[int | None, int | None]:
        return (None, None)

    async def query_xid_errors(self) -> list[dict]:
        # Xid is an NVIDIA kernel-module concept; [] is correct and complete.
        return []
