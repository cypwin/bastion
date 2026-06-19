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

    # Source paths for host-pressure collectors (spec 5.2). Class attributes
    # so tests can point them at fixture files; overridden, never inlined.
    _PSI_DIR: str = "/proc/pressure"
    _VMSTAT_PATH: str = "/proc/vmstat"

    # Page size for swap page->byte conversion. ``os.sysconf`` is unavailable
    # on some platforms; fall back to the canonical 4 KiB.
    try:
        _PAGE_SIZE: int = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        _PAGE_SIZE = 4096

    def __init__(self) -> None:
        self.cpu_history: deque[float] = deque(maxlen=60)
        self.net_recv_history: deque[float] = deque(maxlen=60)
        self.net_sent_history: deque[float] = deque(maxlen=60)

        # Rate tracking state
        self._last_net_io: Any | None = None
        self._last_net_time: float | None = None
        self._last_disk_io: Any | None = None
        self._last_disk_time: float | None = None

        # vmstat-derived rate state (swap pages, oom kills) — spec 5.2.
        # First read primes these; subsequent reads compute deltas.
        self._last_pswpin: int | None = None
        self._last_pswpout: int | None = None
        self._last_swap_time: float | None = None
        self._last_oom_kill: int | None = None
        self._last_oom_time: float | None = None

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
    # Host pressure — PSI / swap-rate / OOM (spec 5.2, fast 2s path)
    # ------------------------------------------------------------------

    def _read_vmstat(self) -> dict[str, int] | None:
        """Read ``/proc/vmstat`` into a ``key -> int`` dict.

        Shared by the swap-rate and OOM collectors so a tick touches the file
        once.  Returns ``None`` when the file is absent or unreadable (e.g. a
        sandboxed container).  Non-integer / malformed lines are skipped
        rather than raising, so a single odd line never loses the whole read.
        """
        if not os.path.exists(self._VMSTAT_PATH):
            return None
        try:
            raw = Path(self._VMSTAT_PATH).read_text()
        except OSError:
            return None
        result: dict[str, int] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return result

    @staticmethod
    def _parse_psi_avg10(text: str) -> tuple[float | None, float | None]:
        """Parse a PSI file body into ``(some_avg10, full_avg10)``.

        Format per line: ``some avg10=1.50 avg60=0.80 avg300=0.40 total=...``.
        A missing/garbled field yields ``None`` for that value (never a
        misleading ``0``).
        """
        some_val: float | None = None
        full_val: float | None = None
        for line in text.splitlines():
            tokens = line.split()
            if not tokens:
                continue
            label = tokens[0]
            if label not in ("some", "full"):
                continue
            avg10: float | None = None
            for tok in tokens[1:]:
                if tok.startswith("avg10="):
                    try:
                        avg10 = float(tok.split("=", 1)[1])
                    except ValueError:
                        avg10 = None
                    break
            if label == "some":
                some_val = avg10
            else:
                full_val = avg10
        return some_val, full_val

    def get_psi_data(self) -> dict[str, float | None]:
        """Return PSI some/full avg10 for cpu/memory/io (spec 5.2).

        Reads ``/proc/pressure/{cpu,memory,io}``.  PSI requires Linux 4.20+
        with ``CONFIG_PSI``; on older kernels and many containers the directory
        is absent and **every field is ``None``** (the tested default).  A
        missing or malformed individual resource file leaves only that
        resource's two fields ``None``.  The returned dict keys match the
        ``ContentionSnapshot`` PSI field names exactly.
        """
        keys = (
            "psi_cpu_some_avg10",
            "psi_cpu_full_avg10",
            "psi_mem_some_avg10",
            "psi_mem_full_avg10",
            "psi_io_some_avg10",
            "psi_io_full_avg10",
        )
        data: dict[str, float | None] = {k: None for k in keys}
        if not os.path.exists(self._PSI_DIR):
            return data
        for resource, prefix in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
            path = Path(self._PSI_DIR) / resource
            if not path.exists():
                continue
            try:
                text = path.read_text()
            except OSError:
                continue
            some_val, full_val = self._parse_psi_avg10(text)
            data[f"psi_{prefix}_some_avg10"] = some_val
            data[f"psi_{prefix}_full_avg10"] = full_val
        return data

    def get_swap_rate_data(self) -> dict[str, float | None]:
        """Return swap in/out rates in MB/s from ``pswpin``/``pswpout`` deltas.

        First read primes the counters and returns ``None`` for both rates (no
        prior delta).  A missing ``/proc/vmstat`` or absent ``pswp*`` keys also
        yield ``None`` — never a misleading ``0`` for a host that cannot report
        swap.  Pages are converted to bytes via the system page size.
        """
        result: dict[str, float | None] = {
            "swap_in_rate_mb_s": None,
            "swap_out_rate_mb_s": None,
        }
        vmstat = self._read_vmstat()
        now = time.monotonic()
        if vmstat is None:
            return result
        cur_in = vmstat.get("pswpin")
        cur_out = vmstat.get("pswpout")
        if cur_in is None or cur_out is None:
            # Keys absent on this host; reset state so we don't compute a
            # bogus delta later, and report None.
            self._last_pswpin = None
            self._last_pswpout = None
            self._last_swap_time = None
            return result

        if (
            self._last_pswpin is not None
            and self._last_pswpout is not None
            and self._last_swap_time is not None
        ):
            dt = now - self._last_swap_time
            if dt > 0:
                bytes_per_mb = 1024 * 1024
                in_pages = cur_in - self._last_pswpin
                out_pages = cur_out - self._last_pswpout
                result["swap_in_rate_mb_s"] = (
                    in_pages * self._PAGE_SIZE / dt / bytes_per_mb
                )
                result["swap_out_rate_mb_s"] = (
                    out_pages * self._PAGE_SIZE / dt / bytes_per_mb
                )

        self._last_pswpin = cur_in
        self._last_pswpout = cur_out
        self._last_swap_time = now
        return result

    def get_oom_data(self) -> dict[str, float | None]:
        """Return OOM-kill cumulative total and per-second delta rate.

        ``oom_kill`` (Linux 4.13+) is cumulative since boot; the total is
        reported on every read, while the rate needs a prior sample (first read
        -> ``rate is None``).  Equal totals yield a genuine ``0.0`` rate (no new
        kills), which callers treat as "no alert."  A missing file or absent
        ``oom_kill`` key yields ``None`` for both — never a misleading ``0``.
        """
        result: dict[str, float | None] = {
            "oom_kill_total": None,
            "oom_kill_rate": None,
        }
        vmstat = self._read_vmstat()
        now = time.monotonic()
        if vmstat is None:
            return result
        cur = vmstat.get("oom_kill")
        if cur is None:
            self._last_oom_kill = None
            self._last_oom_time = None
            return result

        result["oom_kill_total"] = cur
        if self._last_oom_kill is not None and self._last_oom_time is not None:
            dt = now - self._last_oom_time
            if dt > 0:
                result["oom_kill_rate"] = (cur - self._last_oom_kill) / dt

        self._last_oom_kill = cur
        self._last_oom_time = now
        return result

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
        proc_attrs = ["pid", "name", "cpu_percent", "memory_percent", "memory_info"]
        for proc in psutil.process_iter(proc_attrs):
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
