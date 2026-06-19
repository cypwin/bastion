"""NVIDIA GPU backend — queries nvidia-smi for GPU health metrics.

These are the proven query patterns that survived multiple GPU crash
investigations.  All queries use async subprocess with configurable
timeouts and graceful fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque

from bastion.models import GPUStatus

logger = logging.getLogger(__name__)

# Fixed throttle-reason vocabulary (spec 5.1).  The ``clocks_throttle_reasons.*``
# boolean columns are queried in this order; the Prometheus counter stays bounded
# because any future backend maps its vendor reasons onto this same set.
_THROTTLE_REASONS: tuple[str, ...] = (
    "sw_thermal_slowdown",   # clocks_throttle_reasons.sw_thermal_slowdown
    "hw_thermal_slowdown",   # clocks_throttle_reasons.hw_thermal_slowdown
    "hw_power_brake_slowdown",  # clocks_throttle_reasons.hw_power_brake_slowdown
    "sw_power_cap_slowdown",  # clocks_throttle_reasons.sw_power_cap
    "gpu_idle",              # clocks_throttle_reasons.gpu_idle
)

# Matches a kernel ``NVRM: Xid (PCI:0000:01:00): 79, ...`` line.  The Xid literal
# lives only inside this backend (Constraint #7c).  Group 1 is the device tag,
# group 2 the numeric code.
_XID_RE = re.compile(r"NVRM:\s*Xid\s*\(([^)]*)\)\s*:?\s*(\d+)")

# Bound on the rising-edge dedup memory (spec 4.3 / constraint #1): the dedup set
# derives from this bounded deque so long uptime cannot grow it.
_RECENT_XIDS_MAXLEN = 20


class NvidiaBackend:
    """GPU backend using nvidia-smi."""

    def __init__(self) -> None:
        # Bounded ring of recently-seen Xid ``(timestamp, code)`` keys.  The
        # rising-edge dedup set is *derived* from this deque (never an unbounded
        # set), so it cannot grow across long uptime (spec constraint #1).
        self._recent_xids: deque[tuple[str, int]] = deque(maxlen=_RECENT_XIDS_MAXLEN)
        self.xid_count_since_start: int = 0

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

    async def query_processes(self, timeout_seconds: int = 5) -> list[dict[str, str]]:
        """List GPU compute processes via nvidia-smi (async).

        Async-converted (observability spec 5.3): uses
        ``asyncio.create_subprocess_exec`` exactly like :meth:`query_status` so
        the 10s slow tick of ``_machine_snapshot_loop`` never blocks the event
        loop for up to 5s on a synchronous ``subprocess.run``.  Returns one dict
        per compute process with keys ``pid``, ``name``, ``vram_mb`` (string
        values, mirroring the prior contract).  Any failure (non-zero exit,
        timeout, missing binary) degrades to ``[]``.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
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
                logger.debug("nvidia-smi compute-apps query timed out")
                return []

            if proc.returncode != 0:
                logger.debug(
                    "nvidia-smi compute-apps query returned no data (rc=%s)",
                    proc.returncode,
                )
                return []
        except FileNotFoundError:
            logger.debug("nvidia-smi not found (compute-apps query)")
            return []

        processes: list[dict[str, str]] = []
        for line in stdout.decode(errors="replace").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                processes.append({
                    "pid": parts[0],
                    "name": parts[1],
                    "vram_mb": parts[2],
                })
        return processes

    async def query_process_utilization(self, timeout_seconds: int = 5) -> list[dict]:
        """Per-PID GPU utilization via ``nvidia-smi pmon -s u -c 1`` (async, 10s).

        Returns one dict per process with keys ``pid`` (int), ``name`` (str),
        and ``sm_pct``/``mem_pct``/``enc_pct``/``dec_pct`` (int | None).  ``pmon``
        emits two ``#``-prefixed header lines then one space-delimited row per
        process: ``gpu_idx pid type sm mem enc dec command``.  Older/headless
        drivers may omit the ``enc``/``dec`` columns, and an idle GPU reports
        ``-``/``[N/A]`` cells — both degrade **per field** to ``None`` (never a
        misleading ``0``); the row is still returned with whatever it has.

        Graceful degradation (Constraint #4): ``pmon`` unsupported on old drivers
        (non-zero exit), timeout, or a missing binary all yield ``[]``.  The
        ``pmon`` column layout lives only here inside ``NvidiaBackend``
        (Constraint #7c).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "pmon", "-s", "u", "-c", "1",
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
                logger.debug("nvidia-smi pmon query timed out")
                return []

            if proc.returncode != 0 or not stdout.strip():
                logger.debug(
                    "nvidia-smi pmon query returned no data (rc=%s)",
                    proc.returncode,
                )
                return []
        except FileNotFoundError:
            logger.debug("nvidia-smi not found (pmon query)")
            return []

        return _parse_pmon(stdout.decode(errors="replace"))

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

    # ------------------------------------------------------------------
    # Slow-path signals (spec 5.1: throttle reasons 10s, PCIe tx/rx 10s,
    # Xid dmesg scan 30s).  Each is individually try/except-wrapped and
    # degrades to an empty/None value, never an exception (Constraint #4).
    # ------------------------------------------------------------------

    async def query_throttle_reasons(self, timeout_seconds: int = 5) -> list[str]:
        """Parse active ``clocks_throttle_reasons.*`` columns (second call).

        Issues a *separate* ``nvidia-smi`` query (boolean throttle columns
        mis-align with the numeric :meth:`query_status` fields in one CSV pass)
        and collapses the ``Active`` columns into :data:`_THROTTLE_REASONS`.
        Any failure (non-zero exit, timeout, missing binary, ``[N/A]``) yields
        ``[]``.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                (
                    "--query-gpu="
                    "clocks_throttle_reasons.sw_thermal_slowdown,"
                    "clocks_throttle_reasons.hw_thermal_slowdown,"
                    "clocks_throttle_reasons.hw_power_brake_slowdown,"
                    "clocks_throttle_reasons.sw_power_cap,"
                    "clocks_throttle_reasons.gpu_idle"
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
                logger.debug("nvidia-smi throttle query timed out")
                return []

            if proc.returncode != 0 or not stdout.strip():
                logger.debug(
                    "nvidia-smi throttle query returned no data (rc=%s)",
                    proc.returncode,
                )
                return []

            # Single-GPU shipped path: first line only.
            first = stdout.decode().strip().split("\n")[0]
            cols = [c.strip() for c in first.split(",")]
            reasons: list[str] = []
            for i, name in enumerate(_THROTTLE_REASONS):
                if i < len(cols) and cols[i].lower() == "active":
                    reasons.append(name)
            return reasons
        except FileNotFoundError:
            logger.debug("nvidia-smi not found (throttle query)")
            return []

    async def query_pcie_throughput(
        self, timeout_seconds: int = 5,
    ) -> tuple[int | None, int | None]:
        """Return ``(pcie_tx_kb_s, pcie_rx_kb_s)`` from ``pcie.tx/rx_util``.

        ``[N/A]`` (pre-R418 / virtualized) and any failure degrade each element
        to ``None`` — never a misleading ``0``.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=pcie.tx_util,pcie.rx_util",
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
                logger.debug("nvidia-smi pcie query timed out")
                return (None, None)

            if proc.returncode != 0 or not stdout.strip():
                logger.debug(
                    "nvidia-smi pcie query returned no data (rc=%s)",
                    proc.returncode,
                )
                return (None, None)

            first = stdout.decode().strip().split("\n")[0]
            cols = [c.strip() for c in first.split(",")]
            tx = _safe_int(cols[0]) if len(cols) > 0 else None
            rx = _safe_int(cols[1]) if len(cols) > 1 else None
            return (tx, rx)
        except FileNotFoundError:
            logger.debug("nvidia-smi not found (pcie query)")
            return (None, None)

    async def query_xid_errors(self, timeout_seconds: int = 5) -> list[dict]:
        """Scan ``dmesg`` for new ``NVRM: Xid`` lines (rising-edge, bounded).

        Returns one dict per *newly-seen* ``(timestamp, xid_code)`` with keys
        ``timestamp``, ``xid_code``, ``raw_message``.  The dedup memory is the
        bounded :attr:`_recent_xids` deque (maxlen 20), so it cannot grow across
        long uptime.  ``dmesg_restrict=1`` (``PermissionError``) and ``rc=1`` +
        empty stdout both degrade to ``[]`` (the most likely paths).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "dmesg",
                "--time-format", "iso",
                "--since", "30 seconds ago",
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
                logger.debug("dmesg Xid scan timed out")
                return []

            # rc=1 with empty stdout (rotated logs / unreadable kmsg) -> [].
            # Non-zero rc with data still gets parsed (some dmesg builds exit 1
            # on --since with older util-linux); empty output -> [].
            if not stdout.strip():
                logger.debug("dmesg Xid scan returned no data (rc=%s)", proc.returncode)
                return []

            return self._parse_xid_lines(stdout.decode(errors="replace"))
        except FileNotFoundError:
            logger.debug("dmesg not found (Xid scan)")
            return []
        except PermissionError:
            # dmesg_restrict=1 — the most likely path; [] is the tested default.
            logger.debug("dmesg denied (dmesg_restrict=1) — Xid scan -> []")
            return []

    def _parse_xid_lines(self, output: str) -> list[dict]:
        """Extract new Xid events from dmesg text, with bounded rising-edge dedup."""
        seen = set(self._recent_xids)
        new_events: list[dict] = []
        for line in output.splitlines():
            m = _XID_RE.search(line)
            if not m:
                continue
            code = _safe_int(m.group(2))
            if code is None:
                continue
            timestamp = _dmesg_timestamp(line)
            key = (timestamp, code)
            if key in seen:
                continue
            seen.add(key)
            self._recent_xids.append(key)
            self.xid_count_since_start += 1
            new_events.append({
                "timestamp": timestamp,
                "xid_code": code,
                "raw_message": line.strip(),
            })
        return new_events


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


def _parse_pmon(output: str) -> list[dict]:
    """Parse ``nvidia-smi pmon -s u -c 1`` text into per-PID utilization dicts.

    Skips the ``#``-prefixed header lines and parses each whitespace-delimited
    row as ``gpu_idx pid type sm mem [enc dec] command``.  ``sm``/``mem`` are at
    fixed offsets 3/4; ``enc``/``dec`` (offsets 5/6) are absent on older/headless
    drivers, in which case those fields are ``None``.  Non-numeric cells (``-`` /
    ``[N/A]`` on an idle GPU) degrade per field to ``None``.  The trailing
    command token is the process name; a row with no parseable integer PID is
    skipped.
    """
    rows: list[dict] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cols = stripped.split()
        if len(cols) < 4:
            continue
        pid = _safe_int(cols[1])
        if pid is None:
            continue
        sm = _safe_int(cols[3]) if len(cols) > 3 else None
        mem = _safe_int(cols[4]) if len(cols) > 4 else None
        # enc/dec exist only when the row carries the full 8-column layout
        # (gpu pid type sm mem enc dec command); a 6-column row (…sm mem command)
        # has the name in cols[5], so enc/dec must stay None.
        has_enc_dec = len(cols) >= 8
        enc = _safe_int(cols[5]) if has_enc_dec else None
        dec = _safe_int(cols[6]) if has_enc_dec else None
        name = cols[-1]
        rows.append({
            "pid": pid,
            "name": name,
            "sm_pct": sm,
            "mem_pct": mem,
            "enc_pct": enc,
            "dec_pct": dec,
        })
    return rows


def _dmesg_timestamp(line: str) -> str:
    """Extract the leading ISO timestamp from a ``dmesg --time-format iso`` line.

    Such lines begin with e.g. ``2026-06-19T14:32:07,000000+00:00 host ...``.
    The first whitespace-delimited token is the timestamp; if the line is not
    iso-prefixed (older util-linux without ``--since``, ``[  123.456]`` clock
    format) the raw first token is still returned as a best-effort key — the
    rising-edge dedup only needs a stable string, not a parsed datetime.
    """
    stripped = line.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0]
