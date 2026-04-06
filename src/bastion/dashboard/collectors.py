"""System metrics collector — CPU, memory, network, disk, GPU processes."""
from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from bastion.dashboard.helpers import core_char

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False


class SystemDataCollector:
    """Collects system metrics using psutil (when available)."""

    def __init__(self) -> None:
        self.cpu_history: deque[float] = deque(maxlen=60)
        self.net_recv_history: deque[float] = deque(maxlen=60)
        self.net_sent_history: deque[float] = deque(maxlen=60)

        # Rate tracking state
        self._last_net_io: Any | None = None
        self._last_net_time: float | None = None
        self._last_disk_io: Any | None = None
        self._last_disk_time: float | None = None

        # Prime psutil cpu_percent so the first real call returns meaningful values
        if _HAS_PSUTIL:
            psutil.cpu_percent(percpu=True)

    # ------------------------------------------------------------------
    # CPU
    # ------------------------------------------------------------------

    def get_cpu_data(self) -> dict[str, Any]:
        """Return CPU metrics including per-core percentages."""
        if not _HAS_PSUTIL:
            return {
                "percent": 0.0,
                "per_core": [],
                "load_avg": (0.0, 0.0, 0.0),
                "freq_mhz": 0.0,
                "core_count": 0,
            }

        per_core = psutil.cpu_percent(percpu=True)
        avg = sum(per_core) / len(per_core) if per_core else 0.0
        self.cpu_history.append(avg)

        freq = psutil.cpu_freq()
        freq_mhz = freq.current if freq else 0.0

        return {
            "percent": avg,
            "per_core": per_core,
            "load_avg": os.getloadavg(),
            "freq_mhz": freq_mhz,
            "core_count": psutil.cpu_count(logical=True) or 0,
        }

    def cpu_per_core_text(self) -> str:
        """Return Rich markup string with one char per core."""
        if not _HAS_PSUTIL:
            return ""

        per_core = psutil.cpu_percent(percpu=True)
        parts: list[str] = []
        for pct in per_core:
            char, style = core_char(pct)
            parts.append(f"[{style}]{char}[/]")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def get_network_data(self) -> dict[str, float]:
        """Return network I/O rates and totals."""
        if not _HAS_PSUTIL:
            return {
                "recv_rate": 0.0,
                "sent_rate": 0.0,
                "recv_total_gb": 0.0,
                "sent_total_gb": 0.0,
            }

        counters = psutil.net_io_counters()
        now = time.monotonic()

        recv_rate = 0.0
        sent_rate = 0.0

        if self._last_net_io is not None and self._last_net_time is not None:
            dt = now - self._last_net_time
            if dt > 0:
                recv_rate = (counters.bytes_recv - self._last_net_io.bytes_recv) / dt
                sent_rate = (counters.bytes_sent - self._last_net_io.bytes_sent) / dt

        self._last_net_io = counters
        self._last_net_time = now

        # Store in KB/s for history
        self.net_recv_history.append(recv_rate / 1024.0)
        self.net_sent_history.append(sent_rate / 1024.0)

        return {
            "recv_rate": recv_rate,
            "sent_rate": sent_rate,
            "recv_total_gb": counters.bytes_recv / (1024**3),
            "sent_total_gb": counters.bytes_sent / (1024**3),
        }

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def get_memory_data(self) -> dict[str, float] | None:
        """Return system memory and swap metrics."""
        if not _HAS_PSUTIL:
            return None

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        return {
            "total_gb": vm.total / (1024**3),
            "used_gb": vm.used / (1024**3),
            "available_gb": vm.available / (1024**3),
            "percent": vm.percent,
            "swap_used_gb": swap.used / (1024**3),
            "swap_percent": swap.percent,
        }

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    def get_disk_data(self) -> dict[str, Any]:
        """Return disk usage per mount and I/O rates."""
        if not _HAS_PSUTIL:
            return {"mounts": [], "read_rate": 0.0, "write_rate": 0.0}

        mount_labels: dict[str, str] = {
            "/": "System",
            "/mnt/nvme_data": "Data",
        }

        mounts: list[dict[str, Any]] = []
        for mount, label in mount_labels.items():
            try:
                usage = psutil.disk_usage(mount)
                mounts.append({
                    "mount": label,
                    "used_tb": usage.used / (1024**4),
                    "free_tb": usage.free / (1024**4),
                    "percent": usage.percent,
                })
            except OSError:
                pass

        # Disk I/O rates
        read_rate = 0.0
        write_rate = 0.0
        try:
            disk_io = psutil.disk_io_counters()
            now = time.monotonic()
            if (
                disk_io is not None
                and self._last_disk_io is not None
                and self._last_disk_time is not None
            ):
                dt = now - self._last_disk_time
                if dt > 0:
                    read_rate = (
                        disk_io.read_bytes - self._last_disk_io.read_bytes
                    ) / dt
                    write_rate = (
                        disk_io.write_bytes - self._last_disk_io.write_bytes
                    ) / dt
            self._last_disk_io = disk_io
            self._last_disk_time = now
        except Exception:
            pass

        return {
            "mounts": mounts,
            "read_rate": read_rate,
            "write_rate": write_rate,
        }

    # ------------------------------------------------------------------
    # Temperatures
    # ------------------------------------------------------------------

    @staticmethod
    def read_cpu_temp() -> float | None:
        """Read CPU temperature from /sys/class/hwmon (k10temp or coretemp)."""
        hwmon_base = Path("/sys/class/hwmon")
        if not hwmon_base.exists():
            return None

        for hwmon_dir in hwmon_base.iterdir():
            name_file = hwmon_dir / "name"
            if not name_file.exists():
                continue
            try:
                name = name_file.read_text().strip()
            except OSError:
                continue
            if name not in ("k10temp", "coretemp"):
                continue
            # Look for temp1_input (Tctl / Package id 0)
            temp_file = hwmon_dir / "temp1_input"
            if temp_file.exists():
                try:
                    raw = temp_file.read_text().strip()
                    return int(raw) / 1000.0
                except (OSError, ValueError):
                    continue
        return None

    @staticmethod
    def read_nvme_temps() -> list[tuple[str, float]]:
        """Read NVMe temperatures from /sys/class/hwmon."""
        results: list[tuple[str, float]] = []
        hwmon_base = Path("/sys/class/hwmon")
        if not hwmon_base.exists():
            return results

        for hwmon_dir in hwmon_base.iterdir():
            name_file = hwmon_dir / "name"
            if not name_file.exists():
                continue
            try:
                name = name_file.read_text().strip()
            except OSError:
                continue
            if "nvme" not in name:
                continue
            temp_file = hwmon_dir / "temp1_input"
            if temp_file.exists():
                try:
                    raw = temp_file.read_text().strip()
                    temp = int(raw) / 1000.0
                    results.append((name, temp))
                except (OSError, ValueError):
                    continue
        return results

    # ------------------------------------------------------------------
    # GPU processes
    # ------------------------------------------------------------------

    @staticmethod
    def query_gpu_processes() -> list[dict[str, str]]:
        """Query GPU compute processes via the configured GPU backend."""
        from bastion.gpu import get_backend

        return get_backend().query_processes()

    # ------------------------------------------------------------------
    # Top processes
    # ------------------------------------------------------------------

    def get_top_processes(self, n: int = 8) -> list[dict[str, Any]]:
        """Return top n CPU-consuming processes."""
        if not _HAS_PSUTIL:
            return []

        procs: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
            try:
                info = proc.info
                mem_info = info.get("memory_info")
                mem_mb = mem_info.rss / (1024 * 1024) if mem_info else 0.0
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "",
                    "cpu_percent": info.get("cpu_percent") or 0.0,
                    "mem_percent": info.get("memory_percent") or 0.0,
                    "mem_mb": mem_mb,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        procs.sort(key=lambda p: p["cpu_percent"], reverse=True)
        return procs[:n]
