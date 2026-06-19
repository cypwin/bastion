"""Tests for the slow-path ``GPUBackend`` signals (T4-gpu-slowpoll).

Covers spec ``docs/design/specs/2026-06-19-observability-expansion.md`` Section
5.1 (slow path) + 4.3 (``GPUExtendedStatus``).  Three new async protocol methods
populate the slow-path GPU model:

* ``query_throttle_reasons() -> list[str]`` — a *second* ``nvidia-smi`` call
  parsing ``clocks_throttle_reasons.*`` boolean columns into the fixed reason
  vocabulary.  ``StubBackend`` -> ``[]``.
* ``query_pcie_throughput() -> tuple[int | None, int | None]`` — PCIe tx/rx
  KB/s.  ``[N/A]`` (pre-R418 / virtualized) -> ``(None, None)``.  ``StubBackend``
  -> ``(None, None)``.
* ``query_xid_errors() -> list[dict]`` — ``dmesg`` scan for ``NVRM: Xid`` lines
  with a bounded rising-edge dedup deque (``recent_xids``, maxlen 20) so long
  uptime cannot grow it.  ``dmesg_restrict=1`` (PermissionError) and rc=1 with
  empty stdout both degrade to ``[]``.  ``StubBackend`` -> ``[]``.

The nvidia-smi field names and the ``NVRM: Xid`` literal live *only* inside
``NvidiaBackend`` (protocol seam, Constraint #7c).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.gpu.base import GPUBackend
from bastion.gpu.nvidia import NvidiaBackend
from bastion.gpu.stub import StubBackend


def _mock_proc(stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    # asyncio.subprocess.Process.kill() is synchronous; model it as such so the
    # backend's timeout path (kill(); await wait()) leaves no un-awaited coro.
    proc.kill = MagicMock()
    return proc


def _timeout_proc() -> AsyncMock:
    """A process mock whose communicate() raises TimeoutError (slow GPU/lockup)."""
    proc = _mock_proc(b"")
    proc.communicate.side_effect = TimeoutError()
    return proc


# ---------------------------------------------------------------------------
# Throttle reasons
# ---------------------------------------------------------------------------

class TestThrottleReasons:
    @pytest.mark.asyncio
    async def test_parses_active_reasons(self):
        """The canonical spec example: only `Active` columns become reasons.

        Column order is sw_thermal, hw_thermal, hw_power_brake, sw_power_cap,
        gpu_idle.  `Active,Not Active,Active,Not Active,Not Active` selects
        sw_thermal_slowdown and hw_power_brake_slowdown.
        """
        line = b"Active, Not Active, Active, Not Active, Not Active\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            reasons = await NvidiaBackend().query_throttle_reasons()

        assert reasons == ["sw_thermal_slowdown", "hw_power_brake_slowdown"]

    @pytest.mark.asyncio
    async def test_no_active_reasons_returns_empty(self):
        line = b"Not Active, Not Active, Not Active, Not Active, Not Active\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == []

    @pytest.mark.asyncio
    async def test_hw_thermal_and_sw_power_cap(self):
        line = b"Not Active, Active, Not Active, Active, Not Active\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == ["hw_thermal_slowdown", "sw_power_cap_slowdown"]

    @pytest.mark.asyncio
    async def test_second_subprocess_uses_throttle_query(self):
        """The throttle call is a *separate* nvidia-smi querying
        clocks_throttle_reasons.* (boolean fields mis-align with numerics)."""
        captured: dict[str, tuple] = {}

        def _fake_exec(*args, **kwargs):
            captured["args"] = args
            return _mock_proc(b"Active, Not Active, Not Active, Not Active, Not Active\n")

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await NvidiaBackend().query_throttle_reasons()

        joined = " ".join(captured["args"])
        assert "clocks_throttle_reasons" in joined
        assert "nvidia-smi" in captured["args"][0]

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_empty(self):
        proc = _mock_proc(b"", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == []

    @pytest.mark.asyncio
    async def test_na_columns_degrade_to_empty(self):
        """`[N/A]` boolean columns (pre-R525 driver) are not `Active` -> skipped."""
        line = b"[N/A], [N/A], [N/A], [N/A], [N/A]\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", return_value=_timeout_proc()):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == []

    @pytest.mark.asyncio
    async def test_nvidia_smi_missing_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            reasons = await NvidiaBackend().query_throttle_reasons()
        assert reasons == []


# ---------------------------------------------------------------------------
# PCIe throughput
# ---------------------------------------------------------------------------

class TestPcieThroughput:
    @pytest.mark.asyncio
    async def test_parses_tx_rx(self):
        # nvidia-smi reports tx/rx in KB/s when queried with pcie.tx/rx util.
        line = b"125000, 98000\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            tx, rx = await NvidiaBackend().query_pcie_throughput()
        assert tx == 125000
        assert rx == 98000

    @pytest.mark.asyncio
    async def test_na_degrades_to_none(self):
        """`[N/A]` (pre-R418 / virtualized) -> (None, None), no crash."""
        line = b"[N/A], [N/A]\n"
        proc = _mock_proc(line)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            tx, rx = await NvidiaBackend().query_pcie_throughput()
        assert tx is None
        assert rx is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none_pair(self):
        proc = _mock_proc(b"", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            tx, rx = await NvidiaBackend().query_pcie_throughput()
        assert (tx, rx) == (None, None)

    @pytest.mark.asyncio
    async def test_timeout_returns_none_pair(self):
        with patch("asyncio.create_subprocess_exec", return_value=_timeout_proc()):
            tx, rx = await NvidiaBackend().query_pcie_throughput()
        assert (tx, rx) == (None, None)

    @pytest.mark.asyncio
    async def test_missing_smi_returns_none_pair(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await NvidiaBackend().query_pcie_throughput()
        assert result == (None, None)


# ---------------------------------------------------------------------------
# Xid errors
# ---------------------------------------------------------------------------

_XID_LINE = (
    b"2026-06-19T14:32:07,000000+00:00 host kernel: NVRM: Xid (PCI:0000:01:00): 79, "
    b"pid=1234, GPU has fallen off the bus.\n"
)


class TestXidErrors:
    @pytest.mark.asyncio
    async def test_parses_single_xid(self):
        proc = _mock_proc(_XID_LINE)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            events = await NvidiaBackend().query_xid_errors()

        assert len(events) == 1
        assert events[0]["xid_code"] == 79
        assert "Xid" in events[0]["raw_message"]
        assert events[0]["timestamp"]

    @pytest.mark.asyncio
    async def test_permission_error_returns_empty(self):
        """dmesg_restrict=1 -> PermissionError -> [] (the most likely path)."""
        with patch(
            "asyncio.create_subprocess_exec", side_effect=PermissionError()
        ):
            events = await NvidiaBackend().query_xid_errors()
        assert events == []

    @pytest.mark.asyncio
    async def test_rc1_empty_stdout_returns_empty(self):
        """rc=1 with empty stdout (rotated logs / unreadable kmsg) -> [], not error."""
        proc = _mock_proc(b"", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            events = await NvidiaBackend().query_xid_errors()
        assert events == []

    @pytest.mark.asyncio
    async def test_no_xid_lines_returns_empty(self):
        proc = _mock_proc(b"some unrelated kernel line\nanother line\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            events = await NvidiaBackend().query_xid_errors()
        assert events == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", return_value=_timeout_proc()):
            events = await NvidiaBackend().query_xid_errors()
        assert events == []

    @pytest.mark.asyncio
    async def test_rising_edge_dedup_no_reemit(self):
        """A second identical poll does not re-emit the same (ts, code) event."""
        backend = NvidiaBackend()
        proc1 = _mock_proc(_XID_LINE)
        with patch("asyncio.create_subprocess_exec", return_value=proc1):
            first = await backend.query_xid_errors()
        assert len(first) == 1

        proc2 = _mock_proc(_XID_LINE)
        with patch("asyncio.create_subprocess_exec", return_value=proc2):
            second = await backend.query_xid_errors()
        assert second == []

    @pytest.mark.asyncio
    async def test_new_xid_after_seen_one_is_emitted(self):
        """A genuinely new (ts, code) is emitted even after a prior one is seen."""
        backend = NvidiaBackend()
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(_XID_LINE)):
            await backend.query_xid_errors()

        new_line = (
            b"2026-06-19T14:33:10,000000+00:00 host kernel: "
            b"NVRM: Xid (PCI:0000:01:00): 31, pid=5678, channel error.\n"
        )
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(new_line)):
            events = await backend.query_xid_errors()
        assert len(events) == 1
        assert events[0]["xid_code"] == 31

    @pytest.mark.asyncio
    async def test_dedup_set_bounded_by_recent_xids_deque(self):
        """The rising-edge dedup must be sourced from the bounded recent_xids
        deque (maxlen 20) so it cannot grow without bound across long uptime."""
        backend = NvidiaBackend()
        # Feed 50 distinct Xid lines across 50 polls.
        for i in range(50):
            line = (
                f"2026-06-19T14:00:{i:02d},000000+00:00 host kernel: "
                f"NVRM: Xid (PCI:0000:01:00): {40 + i}, pid={i}, err.\n"
            ).encode()
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_proc(line)
            ):
                await backend.query_xid_errors()

        # The dedup memory derives from the bounded deque, so it can never hold
        # more than its maxlen (20), regardless of how many events were seen.
        assert len(backend._recent_xids) <= 20

    @pytest.mark.asyncio
    async def test_xid_count_since_start_accumulates(self):
        """xid_count_since_start counts every distinct rising-edge event, even
        though recent_xids is bounded."""
        backend = NvidiaBackend()
        for i in range(5):
            line = (
                f"2026-06-19T15:00:{i:02d},000000+00:00 host kernel: "
                f"NVRM: Xid (PCI:0000:01:00): {60 + i}, pid={i}, err.\n"
            ).encode()
            with patch(
                "asyncio.create_subprocess_exec", return_value=_mock_proc(line)
            ):
                await backend.query_xid_errors()
        assert backend.xid_count_since_start == 5


# ---------------------------------------------------------------------------
# StubBackend — the correct complete value on non-NVIDIA / no-GPU hosts
# ---------------------------------------------------------------------------

class TestStubBackendSlowPoll:
    @pytest.mark.asyncio
    async def test_throttle_reasons_empty(self):
        assert await StubBackend().query_throttle_reasons() == []

    @pytest.mark.asyncio
    async def test_pcie_throughput_none_pair(self):
        assert await StubBackend().query_pcie_throughput() == (None, None)

    @pytest.mark.asyncio
    async def test_xid_errors_empty(self):
        assert await StubBackend().query_xid_errors() == []


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_nvidia_backend_satisfies_protocol(self):
        assert isinstance(NvidiaBackend(), GPUBackend)

    def test_stub_backend_satisfies_protocol(self):
        assert isinstance(StubBackend(), GPUBackend)
