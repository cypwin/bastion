"""Tests for Phase-1 observability fast-path host-pressure collectors (T4).

Covers ``SystemDataCollector`` additions (spec 5.2):
  - ``get_psi_data()``         — /proc/pressure/{cpu,memory,io} some/full avg10
  - ``get_swap_rate_data()``   — /proc/vmstat pswpin/pswpout delta -> MB/s
  - ``get_oom_data()``         — /proc/vmstat oom_kill cumulative + delta rate
  - ``_read_vmstat()``         — shared /proc/vmstat reader (key->int)

Test strategy (mirrors ``read_cpu_temp`` / ``get_network_data`` patterns):
  - Feed fixture file contents via tmp_path + monkeypatched proc paths and
    assert the parsed floats / rate deltas.
  - Missing source files -> ``None``/skip, never an exception, never a
    misleading ``0`` (graceful-degradation tested default, spec 3.4 / 5.2).
  - First read returns ``None`` for rate signals (no prior delta yet).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bastion.dashboard.collectors import SystemDataCollector

# ---------------------------------------------------------------------------
# Fixture file contents (real-kernel formats)
# ---------------------------------------------------------------------------

PSI_CPU = (
    "some avg10=1.50 avg60=0.80 avg300=0.40 total=12423961\n"
    "full avg10=0.10 avg60=0.05 avg300=0.02 total=0\n"
)
PSI_MEM = (
    "some avg10=2.25 avg60=1.10 avg300=0.55 total=999\n"
    "full avg10=0.75 avg60=0.30 avg300=0.15 total=500\n"
)
PSI_IO = (
    "some avg10=30.00 avg60=12.00 avg300=4.00 total=88888\n"
    "full avg10=27.50 avg60=10.00 avg300=3.00 total=77777\n"
)

# /proc/vmstat is "key value" per line; only a handful of keys matter here.
VMSTAT_BASE = (
    "nr_free_pages 100000\n"
    "pgpgin 1234\n"
    "pswpin 1000\n"
    "pswpout 2000\n"
    "oom_kill 5\n"
)
VMSTAT_LATER = (
    "nr_free_pages 99000\n"
    "pgpgin 9999\n"
    "pswpin 1100\n"  # +100 pages in
    "pswpout 2200\n"  # +200 pages out
    "oom_kill 7\n"  # +2 kills
)


def _write_pressure(tmp_path: Path) -> Path:
    """Create a fake /proc/pressure dir with cpu/memory/io files."""
    pdir = tmp_path / "pressure"
    pdir.mkdir()
    (pdir / "cpu").write_text(PSI_CPU)
    (pdir / "memory").write_text(PSI_MEM)
    (pdir / "io").write_text(PSI_IO)
    return pdir


# ---------------------------------------------------------------------------
# _read_vmstat
# ---------------------------------------------------------------------------


def test_read_vmstat_parses_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vmstat = tmp_path / "vmstat"
    vmstat.write_text(VMSTAT_BASE)
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))

    c = SystemDataCollector()
    parsed = c._read_vmstat()
    assert parsed is not None
    assert parsed["pswpin"] == 1000
    assert parsed["pswpout"] == 2000
    assert parsed["oom_kill"] == 5


def test_read_vmstat_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        SystemDataCollector, "_VMSTAT_PATH", str(tmp_path / "does_not_exist")
    )
    c = SystemDataCollector()
    assert c._read_vmstat() is None  # no exception


def test_read_vmstat_garbage_lines_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pswpin 1000\nthis is junk\n\nnumeric_only 42\noom_kill notanint\n")
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    parsed = c._read_vmstat()
    assert parsed is not None
    assert parsed["pswpin"] == 1000
    assert parsed["numeric_only"] == 42
    assert "oom_kill" not in parsed  # non-int value dropped, no crash


# ---------------------------------------------------------------------------
# get_psi_data
# ---------------------------------------------------------------------------


def test_get_psi_data_parses_all_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = _write_pressure(tmp_path)
    monkeypatch.setattr(SystemDataCollector, "_PSI_DIR", str(pdir))

    c = SystemDataCollector()
    psi = c.get_psi_data()
    assert psi is not None
    assert psi["psi_cpu_some_avg10"] == pytest.approx(1.50)
    assert psi["psi_cpu_full_avg10"] == pytest.approx(0.10)
    assert psi["psi_mem_some_avg10"] == pytest.approx(2.25)
    assert psi["psi_mem_full_avg10"] == pytest.approx(0.75)
    assert psi["psi_io_some_avg10"] == pytest.approx(30.00)
    assert psi["psi_io_full_avg10"] == pytest.approx(27.50)


def test_get_psi_data_missing_dir_returns_all_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PSI absent (kernel <4.20 / container) is a tested default: all None."""
    monkeypatch.setattr(SystemDataCollector, "_PSI_DIR", str(tmp_path / "no_pressure"))
    c = SystemDataCollector()
    psi = c.get_psi_data()
    assert psi is not None
    assert psi["psi_cpu_some_avg10"] is None
    assert psi["psi_io_full_avg10"] is None
    # Never a misleading 0.
    assert all(v is None for v in psi.values())


def test_get_psi_data_partial_resource_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resource file missing -> only that resource's fields are None."""
    pdir = tmp_path / "pressure"
    pdir.mkdir()
    (pdir / "cpu").write_text(PSI_CPU)
    # memory + io files absent
    monkeypatch.setattr(SystemDataCollector, "_PSI_DIR", str(pdir))
    c = SystemDataCollector()
    psi = c.get_psi_data()
    assert psi is not None
    assert psi["psi_cpu_some_avg10"] == pytest.approx(1.50)
    assert psi["psi_mem_some_avg10"] is None
    assert psi["psi_io_full_avg10"] is None


def test_get_psi_data_malformed_file_returns_none_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = tmp_path / "pressure"
    pdir.mkdir()
    (pdir / "cpu").write_text("garbage content no avg here\n")
    (pdir / "memory").write_text(PSI_MEM)
    (pdir / "io").write_text(PSI_IO)
    monkeypatch.setattr(SystemDataCollector, "_PSI_DIR", str(pdir))
    c = SystemDataCollector()
    psi = c.get_psi_data()
    assert psi is not None
    assert psi["psi_cpu_some_avg10"] is None
    assert psi["psi_mem_some_avg10"] == pytest.approx(2.25)


# ---------------------------------------------------------------------------
# get_swap_rate_data
# ---------------------------------------------------------------------------


def test_get_swap_rate_first_read_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First read has no prior delta -> both rates None."""
    vmstat = tmp_path / "vmstat"
    vmstat.write_text(VMSTAT_BASE)
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    swap = c.get_swap_rate_data()
    assert swap is not None
    assert swap["swap_in_rate_mb_s"] is None
    assert swap["swap_out_rate_mb_s"] is None


def test_get_swap_rate_computes_delta_mb_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """+100 pages in / +200 pages out over a known interval -> MB/s."""
    vmstat = tmp_path / "vmstat"
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()

    # Control the clock so the rate denominator is deterministic.
    fake = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    vmstat.write_text(VMSTAT_BASE)
    assert c.get_swap_rate_data()["swap_in_rate_mb_s"] is None  # priming read

    fake["t"] = 1002.0  # 2 seconds elapsed
    vmstat.write_text(VMSTAT_LATER)
    swap = c.get_swap_rate_data()
    # 100 pages * 4096 B = 409600 B over 2s = 204800 B/s = 0.1953125 MB/s
    page = 4096
    expected_in = (100 * page) / 2.0 / (1024 * 1024)
    expected_out = (200 * page) / 2.0 / (1024 * 1024)
    assert swap["swap_in_rate_mb_s"] == pytest.approx(expected_in)
    assert swap["swap_out_rate_mb_s"] == pytest.approx(expected_out)


def test_get_swap_rate_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        SystemDataCollector, "_VMSTAT_PATH", str(tmp_path / "missing")
    )
    c = SystemDataCollector()
    swap = c.get_swap_rate_data()
    assert swap is not None
    assert swap["swap_in_rate_mb_s"] is None
    assert swap["swap_out_rate_mb_s"] is None


def test_get_swap_rate_missing_keys_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vmstat present but without pswp* keys -> None, no exception."""
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("nr_free_pages 1\npgpgin 2\n")
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    # Two reads to rule out the first-read-None path masking the missing key.
    c.get_swap_rate_data()
    swap = c.get_swap_rate_data()
    assert swap["swap_in_rate_mb_s"] is None
    assert swap["swap_out_rate_mb_s"] is None


# ---------------------------------------------------------------------------
# get_oom_data
# ---------------------------------------------------------------------------


def test_get_oom_data_first_read_total_no_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First read exposes the cumulative total but rate is None (no delta)."""
    vmstat = tmp_path / "vmstat"
    vmstat.write_text(VMSTAT_BASE)
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    oom = c.get_oom_data()
    assert oom is not None
    assert oom["oom_kill_total"] == 5
    assert oom["oom_kill_rate"] is None


def test_get_oom_data_delta_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vmstat = tmp_path / "vmstat"
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    fake = {"t": 500.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    vmstat.write_text(VMSTAT_BASE)  # oom_kill 5
    first = c.get_oom_data()
    assert first["oom_kill_total"] == 5
    assert first["oom_kill_rate"] is None

    fake["t"] = 502.0
    vmstat.write_text(VMSTAT_LATER)  # oom_kill 7 -> +2 over 2s
    oom = c.get_oom_data()
    assert oom["oom_kill_total"] == 7
    assert oom["oom_kill_rate"] == pytest.approx(2.0 / 2.0)


def test_get_oom_data_no_new_kills_zero_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal totals -> rate 0.0 (genuine, not misleading) and no false alert."""
    vmstat = tmp_path / "vmstat"
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    fake = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    vmstat.write_text(VMSTAT_BASE)
    c.get_oom_data()
    fake["t"] = 2.0
    oom = c.get_oom_data()  # same content, oom_kill still 5
    assert oom["oom_kill_total"] == 5
    assert oom["oom_kill_rate"] == pytest.approx(0.0)


def test_get_oom_data_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(tmp_path / "missing"))
    c = SystemDataCollector()
    oom = c.get_oom_data()
    assert oom is not None
    assert oom["oom_kill_total"] is None
    assert oom["oom_kill_rate"] is None


def test_get_oom_data_missing_key_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vmstat = tmp_path / "vmstat"
    vmstat.write_text("pswpin 1\npswpout 2\n")  # no oom_kill key
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    oom = c.get_oom_data()
    assert oom["oom_kill_total"] is None
    assert oom["oom_kill_rate"] is None


# ---------------------------------------------------------------------------
# Shared-read integration: swap + oom should each be independently correct
# ---------------------------------------------------------------------------


def test_swap_and_oom_share_vmstat_read_consistently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vmstat = tmp_path / "vmstat"
    monkeypatch.setattr(SystemDataCollector, "_VMSTAT_PATH", str(vmstat))
    c = SystemDataCollector()
    fake = {"t": 10.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake["t"])

    vmstat.write_text(VMSTAT_BASE)
    c.get_swap_rate_data()
    c.get_oom_data()

    fake["t"] = 12.0
    vmstat.write_text(VMSTAT_LATER)
    swap = c.get_swap_rate_data()
    oom = c.get_oom_data()
    assert swap["swap_in_rate_mb_s"] is not None
    assert oom["oom_kill_total"] == 7
    assert oom["oom_kill_rate"] == pytest.approx(1.0)
