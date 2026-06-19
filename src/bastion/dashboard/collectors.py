"""System metrics collector — CPU, memory, network, disk, GPU processes."""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
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
    # RAPL energy sources (spec 5.2 CPU-package-power). Probed in order:
    # Intel powercap, then AMD ``amd_energy`` hwmon / AMD powercap domain.
    # Class attributes so tests point them at fixtures; never inlined.
    _POWERCAP_DIR: str = "/sys/class/powercap"
    _HWMON_DIR: str = "/sys/class/hwmon"

    # Portable BASE block-device regex (spec 5.2): matches whole disks
    # (``nvme0n1``/``sda``/``vdb``/``mmcblk0``/``hdc``), NOT NVMe-only, and
    # excludes partitions (``nvme0n1p1``/``sda1``), loop, and dm devices.
    _BASE_DEVICE_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+|hd[a-z]+)$")

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

        # Per-block-device IO delta state (spec 5.2). Maps base device name to
        # the prior psutil ``sdiskio`` snapshot + the monotonic timestamp it was
        # taken at, so util%/await/rate are computed from deltas. First read of
        # any device primes its entry and emits no row (no misleading 0).
        self._last_block_io: dict[str, Any] = {}
        self._last_block_time: dict[str, float] = {}

        # RAPL energy delta state (spec 5.2). Keyed on the resolved energy path
        # so swapping override paths does not produce a bogus cross-source delta.
        self._last_rapl_uj: int | None = None
        self._last_rapl_path: str | None = None
        self._last_rapl_time: float | None = None

        # Per-process IO delta state (spec 5.3). Maps pid -> (read_bytes,
        # write_bytes, monotonic_ts) from the prior tick so io_counters() can be
        # turned into bytes/s. A process seen for the first time primes its entry
        # and emits None io rates (no misleading 0). Bounded by reaping stale
        # pids each tick so it cannot grow unbounded across long uptime.
        self._last_proc_io: dict[int, tuple[int, int, float]] = {}
        # Process-churn state (spec 5.3): the prior slow-tick PID set + a bounded
        # event deque(maxlen=10) of ProcessChurnEvent. First slow tick primes the
        # baseline (no event); thereafter a set-diff above churn_threshold emits.
        self._last_pid_set: set[int] | None = None
        self._churn_events: deque[Any] = deque(maxlen=10)
        # Own-PID registry cache (spec 5.3, ~30s refresh): pid -> role. Refreshed
        # only on the slowest tick; reused on intervening ticks so the fast path
        # never re-scans /proc/net for the Ollama port.
        self._own_pids: dict[int, str] = {}
        # Most-recent slow-tick GPU sub-data (compute-apps VRAM + pmon util),
        # keyed by pid. Reattached on fast ticks so the 2s path never blocks on a
        # 10s subprocess. None until the first slow tick runs.
        self._gpu_rows_cache: list[Any] = []
        self._gpu_collected_at: float | None = None

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
    # Block-device IO — util% / await / throughput (spec 5.2, fast 2s path)
    # ------------------------------------------------------------------

    def get_block_io_data(
        self, device_filter: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return per-base-device IO stats from psutil ``disk_io_counters``.

        Discovers BASE block devices dynamically (``nvme*/sd*/vd*/mmcblk*/hd*``
        — **not** NVMe only) via :attr:`_BASE_DEVICE_RE`, excluding partitions,
        loop, and dm devices.  ``device_filter`` (the
        ``observability.storage_device_filter`` override) pins an explicit
        allow-list of base device names instead of the regex.

        Each returned dict matches the ``BlockDeviceIOStats`` field names:
        ``device``, ``util_pct`` (``busy_time`` delta / elapsed, as a percent),
        ``read_await_ms``/``write_await_ms`` (``read_time``/``write_time`` delta
        over the op-count delta; ``None`` when no ops occurred this interval —
        never a misleading ``0``), and ``read_rate_mb_s``/``write_rate_mb_s``.

        The **first** observation of any device primes its delta state and the
        device contributes **no row** (mirrors the swap/OOM first-read-``None``
        contract).  A missing psutil, ``disk_io_counters`` returning ``None``,
        or any raised error (e.g. ``AccessDenied``) degrades to ``[]`` — never
        an exception, never a row of zeros.
        """
        if not _HAS_PSUTIL:
            return []
        try:
            perdisk = psutil.disk_io_counters(perdisk=True)
        except Exception:
            return []
        if not perdisk:
            return []

        now = time.monotonic()
        bytes_per_mb = 1024 * 1024
        allow = set(device_filter) if device_filter is not None else None
        rows: list[dict[str, Any]] = []

        for device, cur in perdisk.items():
            if allow is not None:
                if device not in allow:
                    continue
            elif not self._BASE_DEVICE_RE.match(device):
                continue

            prev = self._last_block_io.get(device)
            prev_time = self._last_block_time.get(device)
            # Record current snapshot for the next tick regardless of outcome.
            self._last_block_io[device] = cur
            self._last_block_time[device] = now

            if prev is None or prev_time is None:
                continue  # priming read for this device -> no row
            dt = now - prev_time
            if dt <= 0:
                continue

            row = self._block_io_row(device, prev, cur, dt, bytes_per_mb)
            rows.append(row)

        return rows

    @staticmethod
    def _block_io_row(
        device: str, prev: Any, cur: Any, dt: float, bytes_per_mb: int
    ) -> dict[str, Any]:
        """Build one ``BlockDeviceIOStats``-shaped dict from two snapshots."""
        # busy_time / read_time / write_time are milliseconds; dt is seconds.
        busy_delta_ms = cur.busy_time - prev.busy_time
        util_pct = busy_delta_ms / (dt * 1000.0) * 100.0

        read_ops = cur.read_count - prev.read_count
        write_ops = cur.write_count - prev.write_count
        read_await: float | None = None
        write_await: float | None = None
        if read_ops > 0:
            read_await = (cur.read_time - prev.read_time) / read_ops
        if write_ops > 0:
            write_await = (cur.write_time - prev.write_time) / write_ops

        read_rate = (cur.read_bytes - prev.read_bytes) / dt / bytes_per_mb
        write_rate = (cur.write_bytes - prev.write_bytes) / dt / bytes_per_mb

        return {
            "device": device,
            "util_pct": util_pct,
            "read_await_ms": read_await,
            "write_await_ms": write_await,
            "read_rate_mb_s": read_rate,
            "write_rate_mb_s": write_rate,
        }

    # ------------------------------------------------------------------
    # CPU package power — RAPL energy_uj delta (spec 5.2, fast 2s path)
    # ------------------------------------------------------------------

    def _resolve_rapl_energy_path(self, override: str | None) -> Path | None:
        """Resolve the RAPL ``energy_uj`` source, probing Intel then AMD.

        Order (spec 5.2 / 4.8 ``rapl_domain_path``):
          1. ``override`` (``observability.rapl_domain_path``) — a domain dir
             containing ``energy_uj``, or the ``energy_uj`` file itself.
          2. Intel ``<powercap>/intel-rapl:0/energy_uj``.
          3. AMD ``amd_energy`` hwmon ``energy*_input`` (cumulative µJ), then any
             AMD powercap domain exposing ``energy_uj``.
        Returns the ``Path`` to a microjoule energy counter, or ``None`` when no
        source exists.  Counters from all sources use identical rollover math.
        """
        if override:
            p = Path(override)
            cand = p / "energy_uj" if p.is_dir() else p
            if cand.exists():
                return cand

        # (2) Intel powercap.
        intel = Path(self._POWERCAP_DIR) / "intel-rapl:0" / "energy_uj"
        if intel.exists():
            return intel

        # (3a) AMD amd_energy hwmon — cumulative energy*_input in µJ.
        hwmon_base = Path(self._HWMON_DIR)
        if hwmon_base.exists():
            for hwmon_dir in sorted(hwmon_base.iterdir()):
                name_file = hwmon_dir / "name"
                try:
                    if not name_file.exists() or name_file.read_text().strip() != "amd_energy":
                        continue
                except OSError:
                    continue
                for energy_file in sorted(hwmon_dir.glob("energy*_input")):
                    if energy_file.exists():
                        return energy_file

        # (3b) AMD powercap domain — any non-Intel package domain.
        powercap_base = Path(self._POWERCAP_DIR)
        if powercap_base.exists():
            for dom in sorted(powercap_base.iterdir()):
                energy = dom / "energy_uj"
                if energy.exists():
                    return energy

        return None

    def read_package_power(self, rapl_domain_path: str | None = None) -> float | None:
        """Return CPU package power in watts from a RAPL energy_uj delta.

        Probes Intel (``intel-rapl``) **and** AMD (``amd_energy`` / AMD
        powercap) sources via :meth:`_resolve_rapl_energy_path`.  Power is the
        energy delta (µJ) over elapsed time, rollover-safe: when the counter
        wraps (``new < last``) the domain's ``max_energy_range_uj`` is added.

        Returns ``None`` (never a misleading ``0``) when:
          - no powercap / amd_energy source exists (container, ARM, no kernel
            support);
          - ``energy_uj`` is permission-denied (``PermissionError``);
          - this is the first read of the resolved path (no prior delta yet);
          - the resolved energy path changed since the last read (the prior
            sample is from a different source and cannot form a valid delta).
        """
        path = self._resolve_rapl_energy_path(rapl_domain_path)
        if path is None:
            # Source absent: clear any primed state so we never compute a bogus
            # delta if the source reappears against a stale baseline.
            self._last_rapl_uj = None
            self._last_rapl_path = None
            self._last_rapl_time = None
            return None

        try:
            cur_uj = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            # AccessDenied / unreadable / malformed -> None, keep no baseline.
            self._last_rapl_uj = None
            self._last_rapl_path = None
            self._last_rapl_time = None
            return None

        now = time.monotonic()
        path_str = str(path)
        watts: float | None = None
        if (
            self._last_rapl_uj is not None
            and self._last_rapl_time is not None
            and self._last_rapl_path == path_str
        ):
            dt = now - self._last_rapl_time
            if dt > 0:
                delta_uj = cur_uj - self._last_rapl_uj
                if delta_uj < 0:
                    delta_uj += self._read_rapl_max_range(path)
                watts = delta_uj / 1_000_000.0 / dt

        self._last_rapl_uj = cur_uj
        self._last_rapl_path = path_str
        self._last_rapl_time = now
        return watts

    @staticmethod
    def _read_rapl_max_range(energy_path: Path) -> int:
        """Read the sibling ``max_energy_range_uj`` for rollover correction.

        Returns 0 when the file is absent/unreadable so a missing range simply
        yields no correction (the delta stays as computed) rather than raising.
        """
        max_file = energy_path.parent / "max_energy_range_uj"
        try:
            return int(max_file.read_text().strip())
        except (OSError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # GPU processes
    # ------------------------------------------------------------------

    @staticmethod
    def query_gpu_processes() -> list[dict[str, str]]:
        """Query GPU compute processes via the configured GPU backend (sync bridge).

        The backend's ``query_processes`` is **async** (observability spec 5.3) so
        it never blocks the asyncio event loop when polled by the snapshot loop.
        This wrapper preserves the *synchronous* contract its Textual call sites
        rely on (``GPUProcessListModal.compose()`` and the ``action_gpu_kill``
        screen callback cannot ``await``) by driving the coroutine to completion:

        * No running loop in this thread → ``asyncio.run`` the coroutine.
        * A loop already running in this thread (e.g. called inline from a
          Textual handler) → run the coroutine on a throwaway loop in a worker
          thread and block for its result, so we neither raise
          ``RuntimeError("...from a running event loop")`` nor re-enter the live
          loop.

        Any failure degrades to ``[]`` (the backend already returns ``[]`` on a
        missing GPU / nvidia-smi error; this guards the bridge itself).
        """
        from bastion.gpu import get_backend

        async def _run() -> list[dict[str, str]]:
            return await get_backend().query_processes()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop running in this thread — safe to own one for the call.
            try:
                return asyncio.run(_run())
            except Exception:
                return []

        # A loop is already running here; hand the coroutine to a worker thread
        # with its own event loop and wait synchronously for the result.
        result: list[dict[str, str]] = []

        def _worker() -> None:
            nonlocal result
            try:
                result = asyncio.run(_run())
            except Exception:
                result = []

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join()
        return result

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

    # ------------------------------------------------------------------
    # Process attribution (spec 5.3 / 4.5 — TUI + JSON only, never a label)
    # ------------------------------------------------------------------

    # Ollama process-name match for the own-PID registry. A list so a future
    # rename / wrapper does not require a code edit; matched case-insensitively
    # against the leaf process name.
    _OLLAMA_PROC_NAMES = ("ollama",)

    # Per-section row caps (spec 5.3): 6 GPU / 8 CPU / 5 watchlist / 3 churn,
    # matching the LeasePanel/A2ATaskPanel cap pattern. Applied at render in the
    # panel; the collector keeps a slightly larger top-N so the panel can pick.
    _TOP_N = 12

    async def collect_process_snapshot(
        self,
        config: Any | None = None,
        *,
        slow_tick: bool = False,
    ) -> Any:
        """Assemble a ``ProcessSnapshot`` (spec 5.3 / 4.5).

        TUI + JSON only — process identity is never a Prometheus label.

        Fast path (every tick): top-N by CPU **and** by memory joined into one
        ``ProcessRow`` set, per-process ``io_counters()`` bytes/s (a per-process
        ``AccessDenied`` leaves the io fields ``None`` but **keeps** the row),
        and the watchlist partition (``observability.process_watchlist`` names
        or ``pid:NNN``).  Slow path (``slow_tick=True``, ~10s): the process-churn
        set-diff (bounded ``deque(maxlen=10)``) and the GPU sub-data join
        (compute-apps VRAM + pmon SM%/mem%/enc%/dec% through the ``GPUBackend``
        seam — empty on ``StubBackend`` / no-GPU, no error).  The own-PID
        registry is refreshed on the slow tick and cached for the fast ticks.

        Graceful degradation (Constraint #4): a wholesale failure yields a valid
        (empty) ``ProcessSnapshot``, never an exception, never a misleading 0.
        """
        from bastion.models import (
            ProcessGPURow,
            ProcessRow,
            ProcessSnapshot,
        )

        collected_at = time.time()
        if not _HAS_PSUTIL:
            return ProcessSnapshot(collected_at=collected_at)

        watchlist_names, watchlist_pids = self._parse_watchlist(config)
        churn_threshold = 5
        if config is not None:
            try:
                churn_threshold = int(config.observability.churn_threshold)
            except Exception:
                churn_threshold = 5

        # Refresh the own-PID registry on the slow tick; reuse the cache otherwise.
        if slow_tick or not self._own_pids:
            with contextlib.suppress(Exception):
                # Keep the prior cache rather than dropping all role tags.
                self._own_pids = self._build_own_pid_registry(config)
        own_pids = dict(self._own_pids)

        # ── Scan processes once: cpu/mem/io into one row per pid ────────────
        rows: list[ProcessRow] = []
        now_mono = time.monotonic()
        seen_pids: set[int] = set()
        try:
            proc_attrs = ["pid", "name", "cpu_percent", "memory_info"]
            for proc in psutil.process_iter(proc_attrs):
                try:
                    info = proc.info
                    pid = int(info["pid"])
                except (psutil.NoSuchProcess, psutil.ZombieProcess, KeyError, TypeError):
                    continue
                except psutil.AccessDenied:
                    continue
                seen_pids.add(pid)
                name = info.get("name") or ""
                cpu = info.get("cpu_percent")
                mem_info = info.get("memory_info")
                rss_mb = (mem_info.rss / (1024 * 1024)) if mem_info else None

                io_read_s, io_write_s = self._proc_io_rate(proc, pid, now_mono)

                role = own_pids.get(pid)
                rows.append(
                    ProcessRow(
                        pid=pid,
                        name=name,
                        cpu_pct=float(cpu) if cpu is not None else None,
                        rss_mb=rss_mb,
                        io_read_bytes_s=io_read_s,
                        io_write_bytes_s=io_write_s,
                        is_inference_owned=role is not None,
                        role=role,
                        watchlisted=(
                            name.lower() in watchlist_names or pid in watchlist_pids
                        ),
                    )
                )
        except Exception:
            # process_iter itself blew up — return a valid empty snapshot.
            return ProcessSnapshot(
                own_pids=own_pids, collected_at=collected_at
            )

        # Reap stale per-process IO state so the dict cannot grow unbounded.
        if seen_pids:
            stale = set(self._last_proc_io) - seen_pids
            for dead in stale:
                self._last_proc_io.pop(dead, None)

        # ── Top-N = union of top-by-CPU and top-by-memory (4.5 composite) ───
        top_processes = self._select_top_n(rows)

        # ── Watchlist partition (always present regardless of rank) ─────────
        watchlist_hits = [r for r in rows if r.watchlisted]

        # ── Slow path: churn + GPU join ─────────────────────────────────────
        if slow_tick:
            # Churn uses the cheap psutil.pids() int list (spec 5.3), not the
            # process_iter set, so transient workers that already exited between
            # the iter and this read are still captured by the set-diff.
            try:
                churn_pids = set(psutil.pids())
            except Exception:
                churn_pids = set(seen_pids)
            self._update_churn(churn_pids, churn_threshold)
            try:
                self._gpu_rows_cache = await self._collect_gpu_rows(own_pids)
                self._gpu_collected_at = time.time()
            except Exception:
                # Keep the prior cache; a failed GPU join must not drop the snap.
                pass

        gpu_processes = list(self._gpu_rows_cache)
        # Annotate top rows that hold a GPU row (best-effort join by pid).
        if gpu_processes:
            gpu_by_pid: dict[int, ProcessGPURow] = {g.pid: g for g in gpu_processes}
            for r in top_processes:
                if r.pid in gpu_by_pid:
                    r.gpu_row = gpu_by_pid[r.pid]

        return ProcessSnapshot(
            top_processes=top_processes,
            gpu_processes=gpu_processes,
            own_pids=own_pids,
            watchlist_hits=watchlist_hits,
            recent_churn_events=list(self._churn_events),
            collected_at=collected_at,
            gpu_collected_at=self._gpu_collected_at,
        )

    @staticmethod
    def _parse_watchlist(config: Any | None) -> tuple[set[str], set[int]]:
        """Split ``observability.process_watchlist`` into name + pid sets.

        Entries are process names (matched case-insensitively against the leaf
        name) or ``pid:NNN``.  An empty/absent watchlist returns two empty sets
        (the common case — a single ``len()``-free early exit in the caller).
        """
        names: set[str] = set()
        pids: set[int] = set()
        if config is None:
            return names, pids
        try:
            entries = list(config.observability.process_watchlist or [])
        except Exception:
            return names, pids
        for entry in entries:
            text = str(entry).strip()
            if not text:
                continue
            if text.lower().startswith("pid:"):
                try:
                    pids.add(int(text.split(":", 1)[1]))
                except (ValueError, IndexError):
                    continue
            else:
                names.add(text.lower())
        return names, pids

    def _build_own_pid_registry(self, config: Any | None) -> dict[int, str]:
        """Build the own-PID role registry (spec 5.3, ~30s refresh).

        ``os.getpid()`` is this BASTION process -> ``'bastion'``.  The Ollama
        process is matched by leaf name (``ollama``) and, when discoverable,
        confirmed by its listening port read from ``BrokerConfig.upstream.port``
        (never hard-coded).  ``net_connections()`` may raise ``AccessDenied`` on
        locked-down hosts / non-Linux — that degrades to name-only matching, not
        a crash.
        """
        registry: dict[int, str] = {os.getpid(): "bastion"}
        if not _HAS_PSUTIL:
            return registry

        ollama_port = self._ollama_port(config)
        # Port -> pid map (best-effort; AccessDenied => name-only fallback).
        port_pid: dict[int, int] = {}
        if ollama_port is not None:
            try:
                for conn in psutil.net_connections(kind="inet"):
                    if (
                        conn.status == psutil.CONN_LISTEN
                        and conn.laddr
                        and conn.laddr.port == ollama_port
                        and conn.pid is not None
                    ):
                        port_pid[ollama_port] = conn.pid
            except (psutil.AccessDenied, PermissionError, OSError):
                port_pid = {}  # name-only fallback

        port_matched_pid = port_pid.get(ollama_port) if ollama_port else None
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pid = int(proc.info["pid"])
                    name = (proc.info.get("name") or "").lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
                    continue
                if pid in registry:
                    continue
                is_ollama = any(n in name for n in self._OLLAMA_PROC_NAMES)
                # Prefer a port-confirmed pid; otherwise fall back to name match.
                if (port_matched_pid is not None and pid == port_matched_pid
                        or port_matched_pid is None and is_ollama):
                    registry[pid] = "ollama"
        except Exception:
            return registry
        return registry

    @staticmethod
    def _ollama_port(config: Any | None) -> int | None:
        """Read the Ollama upstream port from config (never hard-coded)."""
        if config is None:
            return None
        for attr_path in (("upstream", "port"), ("ollama", "port")):
            try:
                obj: Any = config
                for attr in attr_path:
                    obj = getattr(obj, attr)
                if isinstance(obj, int):
                    return obj
            except AttributeError:
                continue
        return None

    def _proc_io_rate(
        self, proc: Any, pid: int, now_mono: float
    ) -> tuple[float | None, float | None]:
        """Return ``(read_bytes_s, write_bytes_s)`` from an io_counters() delta.

        First sighting of a pid primes the baseline and returns ``(None, None)``
        — no misleading 0.  A per-process ``AccessDenied`` (common even as the
        broker user) returns ``(None, None)`` so the CALLER keeps the row with
        ``io_*`` None rather than dropping it (spec 5.3).
        """
        try:
            counters = proc.io_counters()
            read_b = int(counters.read_bytes)
            write_b = int(counters.write_bytes)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess,
                AttributeError, NotImplementedError, OSError):
            return None, None

        prior = self._last_proc_io.get(pid)
        self._last_proc_io[pid] = (read_b, write_b, now_mono)
        if prior is None:
            return None, None  # first sighting -> prime only
        prev_read, prev_write, prev_ts = prior
        dt = now_mono - prev_ts
        if dt <= 0:
            return None, None
        read_rate = max(0.0, (read_b - prev_read) / dt)
        write_rate = max(0.0, (write_b - prev_write) / dt)
        return read_rate, write_rate

    def _select_top_n(self, rows: list[Any]) -> list[Any]:
        """Top-N = union of top-by-CPU and top-by-memory (spec 4.5 composite).

        An NVMe burst from a 5%-CPU but high-RSS process is the real stall
        cause, so memory pressure must not be hidden behind the CPU ranking.
        """
        half = max(1, self._TOP_N // 2)
        by_cpu = sorted(rows, key=lambda r: (r.cpu_pct or 0.0), reverse=True)[:half]
        by_mem = sorted(rows, key=lambda r: (r.rss_mb or 0.0), reverse=True)[:half]
        out: list[Any] = []
        seen: set[int] = set()
        for r in by_cpu + by_mem:
            if r.pid in seen:
                continue
            seen.add(r.pid)
            out.append(r)
            if len(out) >= self._TOP_N:
                break
        return out

    def _update_churn(self, current_pids: set[int], threshold: int) -> None:
        """Symmetric PID set-diff per slow tick (spec 5.3, bounded deque(10)).

        First slow tick primes the baseline and emits nothing.  Thereafter a
        new-PID count above ``threshold`` appends a ``ProcessChurnEvent``; the
        deque(maxlen=10) drops the oldest by design.
        """
        from bastion.models import ProcessChurnEvent

        if self._last_pid_set is None:
            self._last_pid_set = set(current_pids)
            return
        new_pids = current_pids - self._last_pid_set
        exited_pids = self._last_pid_set - current_pids
        self._last_pid_set = set(current_pids)
        if len(new_pids) > threshold:
            new_names: list[str] = []
            for pid in list(new_pids)[:16]:
                try:
                    new_names.append(psutil.Process(pid).name())
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.ZombieProcess, OSError):
                    continue
            self._churn_events.append(
                ProcessChurnEvent(
                    timestamp=time.time(),
                    new_count=len(new_pids),
                    exited_count=len(exited_pids),
                    new_names=new_names,
                )
            )

    async def _collect_gpu_rows(self, own_pids: dict[int, str]) -> list[Any]:
        """Join compute-apps VRAM + pmon utilization into ``ProcessGPURow`` rows.

        Both queries route through the async ``GPUBackend`` seam (spec 5.3 / T0)
        so the event loop never blocks on a synchronous subprocess.  On a
        ``StubBackend`` / no-GPU host both return ``[]`` and this yields ``[]``
        (the correct complete value — the panel shows ``(no GPU)``, no error).
        Each PID seen in either source becomes one row; the own-PID registry
        tags inference-owned rows so the TUI can colour competitors distinctly.
        """
        from bastion.gpu import get_backend
        from bastion.models import ProcessGPURow

        backend = get_backend()
        try:
            compute_apps = await backend.query_processes()
        except Exception:
            compute_apps = []
        try:
            pmon = await backend.query_process_utilization()
        except Exception:
            pmon = []

        # vram by pid (compute-apps dicts carry string pid/vram_mb).
        vram_by_pid: dict[int, int | None] = {}
        name_by_pid: dict[int, str] = {}
        for entry in compute_apps:
            try:
                pid = int(entry.get("pid"))
            except (TypeError, ValueError):
                continue
            name_by_pid[pid] = entry.get("name") or ""
            raw_vram = entry.get("vram_mb")
            try:
                vram_by_pid[pid] = int(raw_vram) if raw_vram not in (None, "") else None
            except (TypeError, ValueError):
                vram_by_pid[pid] = None

        util_by_pid: dict[int, dict[str, Any]] = {}
        for entry in pmon:
            try:
                pid = int(entry.get("pid"))
            except (TypeError, ValueError):
                continue
            util_by_pid[pid] = entry
            if pid not in name_by_pid and entry.get("name"):
                name_by_pid[pid] = entry.get("name") or ""

        rows: list[ProcessGPURow] = []
        for pid in sorted(set(vram_by_pid) | set(util_by_pid)):
            util = util_by_pid.get(pid, {})
            role = own_pids.get(pid)
            rows.append(
                ProcessGPURow(
                    pid=pid,
                    name=name_by_pid.get(pid, ""),
                    vram_mb=vram_by_pid.get(pid),
                    sm_pct=util.get("sm_pct"),
                    mem_pct=util.get("mem_pct"),
                    enc_pct=util.get("enc_pct"),
                    dec_pct=util.get("dec_pct"),
                    is_inference_owned=role is not None,
                    role=role,
                )
            )
        return rows
