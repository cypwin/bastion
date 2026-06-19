"""Tests for the T3 host collectors: block-device IO + CPU package power (RAPL).

Covers ``SystemDataCollector`` additions (spec 5.2):
  - ``get_block_io_data()``   — psutil ``disk_io_counters(perdisk=True)``
        ``busy_time``/``read_time``/``write_time`` deltas over discovered BASE
        block devices (``nvme*/sd*/vd*/mmcblk*/hd*``), NOT NVMe-only.
  - ``read_package_power()``  — RAPL ``energy_uj`` delta -> watts, rollover-safe,
        probing Intel (``intel-rapl``) AND AMD (``amd_energy`` / AMD powercap)
        sysfs, ``None`` when no powercap exists or ``energy_uj`` is denied.

Test strategy (mirrors ``test_observability_collectors.py``):
  - Block IO: monkeypatch ``psutil.disk_io_counters`` to return fixture
    per-disk counters across two ticks with a controlled monotonic clock;
    assert util%/await/rate deltas and generic device naming + partition/loop
    exclusion.
  - RAPL: build a fake ``/sys/class/powercap`` (Intel) and a fake AMD
    ``amd_energy`` hwmon tree under ``tmp_path``; point the collector's
    overridable base-path class attributes at them; assert watts on both
    paths, rollover handling, first-read ``None``, AccessDenied ``None``, and
    absent-powercap ``None``.
  - Graceful degradation is the tested default: missing source / permission
    -> ``None``/``[]``, never an exception, never a misleading ``0``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from bastion.dashboard import collectors as collectors_mod
from bastion.dashboard.collectors import SystemDataCollector

# ---------------------------------------------------------------------------
# A minimal psutil ``sdiskio``-shaped stand-in. Real psutil returns a
# namedtuple; the collector only reads attributes, so a tiny object suffices.
# ---------------------------------------------------------------------------


class FakeDiskIO:
    def __init__(
        self,
        *,
        read_count: int = 0,
        write_count: int = 0,
        read_bytes: int = 0,
        write_bytes: int = 0,
        read_time: int = 0,
        write_time: int = 0,
        busy_time: int = 0,
    ) -> None:
        self.read_count = read_count
        self.write_count = write_count
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.read_time = read_time
        self.write_time = write_time
        self.busy_time = busy_time


def _patch_perdisk(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, FakeDiskIO]
) -> None:
    """Make ``psutil.disk_io_counters(perdisk=True)`` return ``mapping``."""

    def fake_counters(perdisk: bool = False, **_: Any) -> Any:
        assert perdisk is True
        return mapping

    monkeypatch.setattr(collectors_mod.psutil, "disk_io_counters", fake_counters)


# ===========================================================================
# get_block_io_data
# ===========================================================================


def test_block_io_first_read_primes_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First read has no prior delta -> empty list (priming), never a row of 0s."""
    _patch_perdisk(monkeypatch, {"nvme0n1": FakeDiskIO(busy_time=100)})
    c = SystemDataCollector()
    assert c.get_block_io_data() == []


def test_block_io_util_and_rate_deltas_two_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two devices, two ticks: util% = busy_time delta / elapsed; rates from bytes."""
    c = SystemDataCollector()
    fake = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    mb = 1024 * 1024
    # Priming tick.
    _patch_perdisk(
        monkeypatch,
        {
            "nvme0n1": FakeDiskIO(
                read_count=10,
                write_count=20,
                read_bytes=0,
                write_bytes=0,
                read_time=100,
                write_time=200,
                busy_time=5000,
            ),
            "sda": FakeDiskIO(
                read_count=0,
                write_count=0,
                read_bytes=0,
                write_bytes=0,
                read_time=0,
                write_time=0,
                busy_time=1000,
            ),
        },
    )
    assert c.get_block_io_data() == []  # priming

    # Second tick, +2000ms elapsed.
    fake["t"] = 1002.0
    _patch_perdisk(
        monkeypatch,
        {
            "nvme0n1": FakeDiskIO(
                read_count=10 + 4,  # +4 reads
                write_count=20 + 5,  # +5 writes
                read_bytes=200 * mb,  # +200 MiB over 2s -> 100 MB/s
                write_bytes=100 * mb,  # +100 MiB over 2s -> 50 MB/s
                read_time=100 + 400,  # +400 ms over 4 reads -> 100 ms await
                write_time=200 + 500,  # +500 ms over 5 writes -> 100 ms await
                busy_time=5000 + 1000,  # +1000 ms busy over 2000 ms -> 50% util
            ),
            "sda": FakeDiskIO(
                read_count=0,
                write_count=0,
                read_bytes=0,
                write_bytes=0,
                read_time=0,
                write_time=0,
                busy_time=1000 + 2000,  # +2000 ms over 2000 ms -> 100% util
            ),
        },
    )
    rows = {r["device"]: r for r in c.get_block_io_data()}
    assert set(rows) == {"nvme0n1", "sda"}

    nvme = rows["nvme0n1"]
    assert nvme["util_pct"] == pytest.approx(50.0)
    assert nvme["read_rate_mb_s"] == pytest.approx(100.0)
    assert nvme["write_rate_mb_s"] == pytest.approx(50.0)
    assert nvme["read_await_ms"] == pytest.approx(100.0)
    assert nvme["write_await_ms"] == pytest.approx(100.0)

    sda = rows["sda"]
    assert sda["util_pct"] == pytest.approx(100.0)
    # No ops this interval -> await is None, never a misleading 0.
    assert sda["read_await_ms"] is None
    assert sda["write_await_ms"] is None


def test_block_io_read_count_zero_await_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_count delta 0 -> read_await None; write ops still produce await."""
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    _patch_perdisk(
        monkeypatch,
        {"vdb": FakeDiskIO(read_count=5, write_count=5, read_time=10, write_time=10, busy_time=0)},
    )
    c.get_block_io_data()  # prime

    fake["t"] = 2.0
    _patch_perdisk(
        monkeypatch,
        {
            "vdb": FakeDiskIO(
                read_count=5,  # +0 reads
                write_count=5 + 2,  # +2 writes
                read_time=10,  # unchanged
                write_time=10 + 60,  # +60 ms over 2 writes -> 30 ms await
                busy_time=0,
            )
        },
    )
    rows = {r["device"]: r for r in c.get_block_io_data()}
    assert rows["vdb"]["read_await_ms"] is None
    assert rows["vdb"]["write_await_ms"] == pytest.approx(30.0)


def test_block_io_generic_device_names_partitions_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sda/vdb/mmcblk0 base devices produce rows; partitions/loop/dm excluded."""
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    devices = {
        "nvme0n1": FakeDiskIO(busy_time=0),
        "sda": FakeDiskIO(busy_time=0),
        "vdb": FakeDiskIO(busy_time=0),
        "mmcblk0": FakeDiskIO(busy_time=0),
        # The following MUST be excluded by the base-device regex:
        "nvme0n1p1": FakeDiskIO(busy_time=0),  # partition
        "sda1": FakeDiskIO(busy_time=0),  # partition
        "mmcblk0p1": FakeDiskIO(busy_time=0),  # partition
        "loop0": FakeDiskIO(busy_time=0),  # loopback
        "dm-0": FakeDiskIO(busy_time=0),  # device-mapper
        "sr0": FakeDiskIO(busy_time=0),  # optical
    }
    _patch_perdisk(monkeypatch, devices)
    c.get_block_io_data()  # prime
    fake["t"] = 2.0
    _patch_perdisk(monkeypatch, devices)
    seen = {r["device"] for r in c.get_block_io_data()}
    assert seen == {"nvme0n1", "sda", "vdb", "mmcblk0"}


def test_block_io_storage_device_filter_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit allow-list pins which base devices are reported."""
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])
    devices = {"nvme0n1": FakeDiskIO(busy_time=0), "sda": FakeDiskIO(busy_time=0)}
    _patch_perdisk(monkeypatch, devices)
    c.get_block_io_data(device_filter=["sda"])  # prime
    fake["t"] = 2.0
    _patch_perdisk(monkeypatch, devices)
    seen = {r["device"] for r in c.get_block_io_data(device_filter=["sda"])}
    assert seen == {"sda"}


def test_block_io_none_counters_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disk_io_counters(perdisk=True) returning None -> [] (no crash)."""

    monkeypatch.setattr(
        collectors_mod.psutil, "disk_io_counters", lambda perdisk=False, **_: None
    )
    c = SystemDataCollector()
    assert c.get_block_io_data() == []


def test_block_io_access_denied_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising disk_io_counters degrades to [] rather than propagating."""

    def boom(perdisk: bool = False, **_: Any) -> Any:
        raise OSError("permission denied")

    monkeypatch.setattr(collectors_mod.psutil, "disk_io_counters", boom)
    c = SystemDataCollector()
    assert c.get_block_io_data() == []


# ===========================================================================
# read_package_power — RAPL energy_uj delta -> watts
# ===========================================================================


def _write_intel_rapl(
    tmp_path: Path, energy_uj: int, max_uj: int = 262143328850
) -> Path:
    """Create a fake /sys/class/powercap with one intel-rapl:0 package domain."""
    pc = tmp_path / "powercap"
    dom = pc / "intel-rapl:0"
    dom.mkdir(parents=True)
    (dom / "name").write_text("package-0\n")
    (dom / "energy_uj").write_text(f"{energy_uj}\n")
    (dom / "max_energy_range_uj").write_text(f"{max_uj}\n")
    return pc


def _write_amd_energy_hwmon(tmp_path: Path, energy_uj: int) -> Path:
    """Create a fake /sys/class/hwmon with an amd_energy energy*_input domain."""
    hw = tmp_path / "hwmon"
    dom = hw / "hwmon3"
    dom.mkdir(parents=True)
    (dom / "name").write_text("amd_energy\n")
    # amd_energy exposes cumulative energy in microjoules.
    (dom / "energy1_input").write_text(f"{energy_uj}\n")
    return hw


def _point_collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    powercap: Path | None = None,
    hwmon: Path | None = None,
) -> None:
    """Repoint the collector's overridable RAPL base paths at fixtures.

    A non-existent path stands in for "this source is absent on the host."
    """
    if powercap is None:
        powercap = Path("/nonexistent/powercap")
    if hwmon is None:
        hwmon = Path("/nonexistent/hwmon")
    monkeypatch.setattr(SystemDataCollector, "_POWERCAP_DIR", str(powercap))
    monkeypatch.setattr(SystemDataCollector, "_HWMON_DIR", str(hwmon))


def test_rapl_intel_first_read_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First read primes the energy counter and returns None (no prior delta)."""
    pc = _write_intel_rapl(tmp_path, energy_uj=1_000_000)
    _point_collector(monkeypatch, powercap=pc)
    c = SystemDataCollector()
    assert c.read_package_power() is None


def test_rapl_intel_delta_to_watts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intel: +4_000_000 uJ over 2s -> 2.0 W."""
    pc = _write_intel_rapl(tmp_path, energy_uj=1_000_000)
    _point_collector(monkeypatch, powercap=pc)
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    assert c.read_package_power() is None  # prime

    fake["t"] = 2.0
    (pc / "intel-rapl:0" / "energy_uj").write_text(f"{1_000_000 + 4_000_000}\n")
    watts = c.read_package_power()
    assert watts == pytest.approx(2.0)


def test_rapl_intel_rollover_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the counter wraps (new < last), add max_energy_range_uj -> positive."""
    max_uj = 10_000_000
    pc = _write_intel_rapl(tmp_path, energy_uj=max_uj - 1_000_000, max_uj=max_uj)
    _point_collector(monkeypatch, powercap=pc)
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    assert c.read_package_power() is None  # prime at max-1_000_000

    fake["t"] = 2.0
    # Wrapped to 1_000_000: raw delta is negative; rollover adds max_uj.
    (pc / "intel-rapl:0" / "energy_uj").write_text("1000000\n")
    watts = c.read_package_power()
    # Effective delta = (1_000_000 + max_uj) - (max_uj - 1_000_000) = 2_000_000 uJ
    assert watts is not None
    assert watts > 0
    assert watts == pytest.approx(2_000_000 / 1_000_000 / 2.0)


def test_rapl_amd_path_when_intel_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """intel-rapl absent + amd_energy present -> value from the AMD path, not None."""
    hw = _write_amd_energy_hwmon(tmp_path, energy_uj=5_000_000)
    # Intel powercap dir does not exist; AMD hwmon does.
    _point_collector(monkeypatch, powercap=None, hwmon=hw)
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    assert c.read_package_power() is None  # prime via AMD path

    fake["t"] = 2.0
    (hw / "hwmon3" / "energy1_input").write_text(f"{5_000_000 + 6_000_000}\n")
    watts = c.read_package_power()
    assert watts is not None
    assert watts == pytest.approx(3.0)  # 6_000_000 uJ / 2s / 1e6


def test_rapl_absent_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No powercap and no amd_energy hwmon -> None (tested default), no crash."""
    _point_collector(monkeypatch, powercap=None, hwmon=None)
    c = SystemDataCollector()
    assert c.read_package_power() is None
    # A second read must still be None (not a bogus delta off primed state).
    assert c.read_package_power() is None


def test_rapl_access_denied_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """energy_uj present but permission-denied -> None, never an exception."""
    pc = _write_intel_rapl(tmp_path, energy_uj=1_000_000)
    _point_collector(monkeypatch, powercap=pc)

    orig_read_text = Path.read_text

    def denied(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "energy_uj":
            raise PermissionError("EACCES")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", denied)
    c = SystemDataCollector()
    assert c.read_package_power() is None


def test_rapl_domain_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit rapl_domain_path pins the energy source directly."""
    dom = tmp_path / "mydomain"
    dom.mkdir()
    (dom / "energy_uj").write_text("1000000\n")
    (dom / "max_energy_range_uj").write_text("262143328850\n")
    # Point default probes at nothing so only the override can satisfy the read.
    _point_collector(monkeypatch, powercap=None, hwmon=None)
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    assert c.read_package_power(rapl_domain_path=str(dom)) is None  # prime
    fake["t"] = 2.0
    (dom / "energy_uj").write_text(f"{1_000_000 + 4_000_000}\n")
    assert c.read_package_power(rapl_domain_path=str(dom)) == pytest.approx(2.0)
