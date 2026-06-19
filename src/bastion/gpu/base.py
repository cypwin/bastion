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
        """Query the GPU fast-path status record.

        Returns a :class:`GPUStatus` with whatever fields are available.
        Unavailable fields are ``None`` (graceful degradation).

        Beyond the original temperature / VRAM / power fields, the extended
        fast-path contract (observability spec Section 5.1) is that an
        implementation populates the eleven new ``GPUStatus`` fields from this
        *single* query when the hardware exposes them:
        ``compute_utilization_pct``, ``memory_bandwidth_utilization_pct``,
        ``sm_clock_mhz``, ``gr_clock_mhz``, ``mem_clock_mhz``, ``fan_speed_pct``,
        ``memory_junction_temp_c`` and the four
        ``pcie_link_gen_{current,max}`` / ``pcie_link_width_{current,max}``
        fields.  Any field the device cannot report (``[N/A]``, pre-Ampere
        memory-junction temp, fanless server GPU, non-NVIDIA hardware) MUST be
        left ``None`` — never a misleading ``0``.  :class:`NvidiaBackend`
        parses these from one ``nvidia-smi`` call; :class:`StubBackend`
        (non-NVIDIA / no-GPU) leaves them all ``None`` as the *correct
        complete* value.  Vendor field names belong only inside the
        implementing backend, never in higher layers (Constraint #7c).
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
