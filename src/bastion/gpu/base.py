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

    async def query_throttle_reasons(self) -> list[str]:
        """Return active GPU clock-throttle reasons (slow path, ~10s cadence).

        :class:`NvidiaBackend` issues a *second* ``nvidia-smi`` call parsing the
        boolean ``clocks_throttle_reasons.*`` columns (they mis-align with the
        numeric fields of :meth:`query_status` in one CSV pass) and collapses
        the ``Active`` ones into the fixed reason vocabulary
        ``{sw_thermal_slowdown, hw_thermal_slowdown, hw_power_brake_slowdown,
        sw_power_cap_slowdown, gpu_idle}``.  A future ``AMDBackend`` would map
        its vendor reasons onto the same fixed set so the Prometheus counter
        stays bounded.  :class:`StubBackend` (non-NVIDIA / no-GPU) returns
        ``[]`` — the *correct complete* value, never a crash.  Any failure
        (non-zero exit, timeout, missing binary, ``[N/A]`` columns) degrades to
        ``[]``.  The ``clocks_throttle_reasons.*`` field names live only inside
        :class:`NvidiaBackend` (Constraint #7c).
        """
        ...

    async def query_pcie_throughput(self) -> tuple[int | None, int | None]:
        """Return ``(pcie_tx_kb_s, pcie_rx_kb_s)`` (slow path, ~10s cadence).

        :class:`NvidiaBackend` parses ``pcie.tx_util``/``pcie.rx_util`` (KB/s,
        R418+).  Older drivers / virtualized GPUs report ``[N/A]`` for these,
        which degrades each element to ``None`` rather than a misleading ``0``.
        :class:`StubBackend` returns ``(None, None)``.  Any failure degrades to
        ``(None, None)``.
        """
        ...

    async def query_xid_errors(self) -> list[dict]:
        """Return newly-seen GPU device error events (slow path, ~30s cadence).

        :class:`NvidiaBackend` scans ``dmesg`` for ``NVRM: Xid`` lines, with a
        **rising-edge dedup** keyed on ``(timestamp, xid_code)`` sourced from a
        bounded ``recent_xids`` deque (``maxlen=20``) so the dedup memory cannot
        grow across long uptime.  Each returned dict carries the keys
        ``timestamp``, ``xid_code`` and ``raw_message``.  ``xid_code`` is a
        *generic device error-code* int (NVIDIA Xid today; a future
        ``AMDBackend`` can map amdgpu reset events onto the same shape).
        ``dmesg_restrict=1`` (``PermissionError``) and ``rc=1`` with empty
        stdout both degrade to ``[]`` (the most likely paths), as does a
        timeout or missing binary.  :class:`StubBackend` returns ``[]`` — Xid is
        an NVIDIA kernel-module concept, so the empty list is the correct and
        complete value on non-NVIDIA hardware.  The ``NVRM: Xid`` literal lives
        only inside :class:`NvidiaBackend` (Constraint #7c).
        """
        ...
