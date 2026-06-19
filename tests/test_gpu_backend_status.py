"""Tests for the extended ``GPUBackend`` status query (T3-backend).

Covers spec ``docs/design/specs/2026-06-19-observability-expansion.md`` Section
5.1: ``NvidiaBackend.query_status()`` extends its single async ``nvidia-smi
--query-gpu`` call from 5 to 16 fields and populates the eleven new
``GPUStatus`` fast-path fields with per-field ``_safe_int``/``_safe_float``
parsing.  ``[N/A]``/malformed fields degrade to ``None`` per-field (no crash),
and ``StubBackend.query_status()`` leaves every new field ``None`` — the
*correct complete* value on non-NVIDIA / no-GPU hosts.

All GPU access is routed through the ``GPUBackend`` protocol seam: the
nvidia-smi field names appear only inside ``NvidiaBackend``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bastion.gpu.nvidia import NvidiaBackend
from bastion.gpu.stub import StubBackend

# Field order matches the extended ``--query-gpu`` list in NvidiaBackend:
#   temperature.gpu, memory.used, memory.free, memory.total, power.draw,
#   utilization.gpu, utilization.memory, clocks.sm, clocks.gr, clocks.mem,
#   fan.speed, temperature.memory,
#   pcie.link.gen.current, pcie.link.gen.max,
#   pcie.link.width.current, pcie.link.width.max
_FULL_LINE = (
    b"55, 8192, 24576, 32768, 185.50, 87, 45, 2520, 2505, 10501, "
    b"72, 98, 4, 4, 16, 16\n"
)


def _mock_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate.return_value = (stdout, b"")
    proc.returncode = returncode
    return proc


class TestNvidiaBackendExtendedStatus:
    @pytest.mark.asyncio
    async def test_parses_all_sixteen_fields(self):
        """A full extended nvidia-smi line populates every new field."""
        proc = _mock_proc(_FULL_LINE)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status = await NvidiaBackend().query_status()

        # Pre-existing five fields still parse.
        assert status.temperature_c == 55
        assert status.vram_used_mb == 8192
        assert status.vram_free_mb == 24576
        assert status.vram_total_mb == 32768
        assert status.power_draw_watts == 185.5
        # Eleven new fast-path fields.
        assert status.compute_utilization_pct == 87
        assert status.memory_bandwidth_utilization_pct == 45
        assert status.sm_clock_mhz == 2520
        assert status.gr_clock_mhz == 2505
        assert status.mem_clock_mhz == 10501
        assert status.fan_speed_pct == 72
        assert status.memory_junction_temp_c == 98
        assert status.pcie_link_gen_current == 4
        assert status.pcie_link_gen_max == 4
        assert status.pcie_link_width_current == 16
        assert status.pcie_link_width_max == 16
        # Computed field follows from the parsed link fields.
        assert status.pcie_downgraded is False

    @pytest.mark.asyncio
    async def test_parses_twelve_field_line(self):
        """A 12-field line (5 existing + util/clocks/fan/memtemp) parses; the
        absent PCIe columns degrade per-field to None (driver returns fewer
        columns -> per-field degradation, never an all-after-gap drop)."""
        twelve = b"60, 4096, 28672, 32768, 200.00, 90, 50, 2400, 2400, 9500, 65, 95\n"
        proc = _mock_proc(twelve)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status = await NvidiaBackend().query_status()

        assert status.compute_utilization_pct == 90
        assert status.memory_bandwidth_utilization_pct == 50
        assert status.sm_clock_mhz == 2400
        assert status.gr_clock_mhz == 2400
        assert status.mem_clock_mhz == 9500
        assert status.fan_speed_pct == 65
        assert status.memory_junction_temp_c == 95
        # The four PCIe columns are absent -> None per-field, no IndexError.
        assert status.pcie_link_gen_current is None
        assert status.pcie_link_gen_max is None
        assert status.pcie_link_width_current is None
        assert status.pcie_link_width_max is None
        assert status.pcie_downgraded is False

    @pytest.mark.asyncio
    async def test_na_and_malformed_fields_degrade_to_none(self):
        """`[N/A]` (P8/D3 power state, pre-Ampere mem-temp, fanless server GPU)
        and malformed tokens degrade to None per-field without crashing."""
        # power.draw, fan.speed, temperature.memory are [N/A]; one garbage clock.
        line = (
            b"55, 8192, 24576, 32768, [N/A], 87, 45, 2520, garbage, 10501, "
            b"[N/A], [N/A], 4, 4, 16, 16\n"
        )
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status = await NvidiaBackend().query_status()

        # Surrounding good fields still parse.
        assert status.temperature_c == 55
        assert status.compute_utilization_pct == 87
        assert status.memory_bandwidth_utilization_pct == 45
        assert status.sm_clock_mhz == 2520
        assert status.mem_clock_mhz == 10501
        assert status.pcie_link_gen_current == 4
        # [N/A] / malformed -> None, no exception.
        assert status.power_draw_watts is None
        assert status.gr_clock_mhz is None
        assert status.fan_speed_pct is None
        assert status.memory_junction_temp_c is None

    @pytest.mark.asyncio
    async def test_pcie_downgraded_true_when_parsed_below_max(self):
        """A genuinely downgraded link (Gen5x16 negotiated, running Gen1x4)."""
        line = (
            b"55, 8192, 24576, 32768, 185.50, 87, 45, 2520, 2505, 10501, "
            b"72, 98, 1, 5, 4, 16\n"
        )
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            status = await NvidiaBackend().query_status()

        assert status.pcie_link_gen_current == 1
        assert status.pcie_link_gen_max == 5
        assert status.pcie_link_width_current == 4
        assert status.pcie_link_width_max == 16
        assert status.pcie_downgraded is True

    @pytest.mark.asyncio
    async def test_query_status_includes_new_query_fields(self):
        """The single nvidia-smi call requests the new --query-gpu fields (the
        field names live only inside NvidiaBackend)."""
        captured: dict[str, tuple] = {}

        def _fake_exec(*args, **kwargs):
            captured["args"] = args
            return _mock_proc(_FULL_LINE)

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await NvidiaBackend().query_status()

        joined = " ".join(captured["args"])
        for field in (
            "utilization.gpu",
            "utilization.memory",
            "clocks.sm",
            "clocks.gr",
            "clocks.mem",
            "fan.speed",
            "temperature.memory",
            "pcie.link.gen.current",
            "pcie.link.gen.max",
            "pcie.link.width.current",
            "pcie.link.width.max",
        ):
            assert field in joined, f"{field} missing from nvidia-smi query"
        # Still one subprocess on the fast path (no second call).
        assert joined.count("nvidia-smi") == 1


class TestStubBackendExtendedStatus:
    @pytest.mark.asyncio
    async def test_query_status_new_fields_are_none(self):
        """StubBackend (non-NVIDIA / no-GPU) leaves every new field None."""
        status = await StubBackend().query_status()

        for field in (
            "compute_utilization_pct",
            "memory_bandwidth_utilization_pct",
            "sm_clock_mhz",
            "gr_clock_mhz",
            "mem_clock_mhz",
            "fan_speed_pct",
            "memory_junction_temp_c",
            "pcie_link_gen_current",
            "pcie_link_gen_max",
            "pcie_link_width_current",
            "pcie_link_width_max",
        ):
            assert getattr(status, field) is None, f"{field} should be None on StubBackend"
        # gpu_index keeps its default and no false downgrade alarm.
        assert status.gpu_index == 0
        assert status.pcie_downgraded is False
