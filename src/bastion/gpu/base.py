"""GPU backend protocol — defines the interface for GPU health monitoring."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bastion.models import GPUStatus


@runtime_checkable
class GPUBackend(Protocol):
    """Protocol for GPU health monitoring backends.

    Implementations provide GPU status queries, safety checks, and
    process listing.  BASTION ships with :class:`NvidiaBackend` (nvidia-smi)
    and :class:`StubBackend` (no-op for systems without a supported GPU).
    """

    async def query_status(self, timeout_seconds: int = 5) -> GPUStatus:
        """Query GPU temperature, VRAM, and power draw.

        Returns a :class:`GPUStatus` with whatever fields are available.
        Unavailable fields are ``None`` (graceful degradation).
        """
        ...

    async def get_vram_free_gb(self) -> float | None:
        """Return free VRAM in GB, or ``None`` if unavailable."""
        ...

    def query_processes(self) -> list[dict[str, str]]:
        """List GPU compute processes (synchronous).

        Returns a list of dicts with keys ``pid``, ``name``, ``vram_mb``.
        """
        ...
