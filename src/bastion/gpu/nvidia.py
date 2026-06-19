"""NVIDIA GPU backend — queries nvidia-smi for GPU health metrics.

These are the proven query patterns that survived multiple GPU crash
investigations.  All queries use async subprocess with configurable
timeouts and graceful fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from bastion.models import GPUStatus

logger = logging.getLogger(__name__)


class NvidiaBackend:
    """GPU backend using nvidia-smi."""

    async def query_status(self, timeout_seconds: int = 5) -> GPUStatus:
        """Query all GPU metrics in a single nvidia-smi call.

        Uses ``asyncio.create_subprocess_exec()`` to avoid blocking the
        event loop.  Extends the original five-field query to sixteen fields
        so the eleven new fast-path :class:`GPUStatus` signals (compute/memory
        utilization, SM/graphics/memory clocks, fan speed, GDDR junction temp,
        and PCIe link gen/width current/max) are populated from this *single*
        subprocess — no second nvidia-smi on the fast path (observability spec
        Section 5.1).  Parsing is **per-field**: each value is read positionally
        and guarded with ``len(parts) > i``, so a driver returning fewer columns
        (or ``[N/A]`` in any column) degrades that field to ``None`` rather than
        dropping every field after the first gap.  The nvidia-smi field names
        live only here inside ``NvidiaBackend`` (protocol seam, Constraint #7c).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                (
                    "--query-gpu="
                    "temperature.gpu,memory.used,memory.free,memory.total,power.draw,"
                    "utilization.gpu,utilization.memory,clocks.sm,clocks.gr,clocks.mem,"
                    "fan.speed,temperature.memory,"
                    "pcie.link.gen.current,pcie.link.gen.max,"
                    "pcie.link.width.current,pcie.link.width.max"
                ),
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug("nvidia-smi timed out after %ds", timeout_seconds)
                return GPUStatus()

            if proc.returncode != 0 or not stdout.strip():
                logger.debug("nvidia-smi returned no data (rc=%d)", proc.returncode)
                return GPUStatus()

            output = stdout.decode()
            # GPU 0 only (single-GPU shipped path; multi-GPU line selection is a
            # future track per spec Section 5.1).  ``[N/A]`` and malformed tokens
            # are absorbed by ``_safe_int``/``_safe_float`` per-field.
            parts = [p.strip() for p in output.strip().split("\n")[0].split(",")]

            def _int(i: int) -> int | None:
                return _safe_int(parts[i]) if len(parts) > i else None

            def _float(i: int) -> float | None:
                return _safe_float(parts[i]) if len(parts) > i else None

            return GPUStatus(
                temperature_c=_int(0),
                vram_used_mb=_int(1),
                vram_free_mb=_int(2),
                vram_total_mb=_int(3),
                power_draw_watts=_float(4),
                compute_utilization_pct=_int(5),
                memory_bandwidth_utilization_pct=_int(6),
                sm_clock_mhz=_int(7),
                gr_clock_mhz=_int(8),
                mem_clock_mhz=_int(9),
                fan_speed_pct=_int(10),
                memory_junction_temp_c=_int(11),
                pcie_link_gen_current=_int(12),
                pcie_link_gen_max=_int(13),
                pcie_link_width_current=_int(14),
                pcie_link_width_max=_int(15),
            )
        except FileNotFoundError:
            logger.debug("nvidia-smi not found")
            return GPUStatus()

    async def get_vram_free_gb(self) -> float | None:
        """Query free VRAM in GB via nvidia-smi."""
        status = await self.query_status()
        if status.vram_free_mb is not None:
            return status.vram_free_mb / 1024.0
        return None

    def query_processes(self) -> list[dict[str, str]]:
        """List GPU compute processes via nvidia-smi."""
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        processes: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                processes.append({
                    "pid": parts[0],
                    "name": parts[1],
                    "vram_mb": parts[2],
                })
        return processes

    async def check_gpu_responsive(self, timeout_seconds: float = 5.0) -> bool | None:
        """Check if nvidia-smi responds within timeout.

        Returns ``True`` if responsive, ``False`` if timed out (possible
        GPU lockup), ``None`` if nvidia-smi is not available.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds,
                )
                return proc.returncode == 0 and bool(stdout.strip())
            except TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return False
        except FileNotFoundError:
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(s: str) -> int | None:
    try:
        return int(float(s.strip()))
    except (ValueError, AttributeError):
        return None


def _safe_float(s: str) -> float | None:
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None
