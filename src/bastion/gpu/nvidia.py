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
        event loop.
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
            except TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug("nvidia-smi timed out after %ds", timeout_seconds)
                return GPUStatus()

            if proc.returncode != 0 or not stdout.strip():
                logger.debug("nvidia-smi returned no data (rc=%d)", proc.returncode)
                return GPUStatus()

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
