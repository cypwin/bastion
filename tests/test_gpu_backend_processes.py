"""Tests for async GPU-process queries (T0-async-prereq).

Covers spec ``docs/design/specs/2026-06-19-observability-expansion.md`` Section
5.3 (the async prerequisite for the process-attribution cluster).

Two GPU-process protocol methods must be async (``asyncio.create_subprocess_exec``,
matching ``query_status``) so the 10s slow tick of ``_machine_snapshot_loop``
never blocks the event loop for up to 5s on a synchronous ``subprocess.run``:

* ``query_processes() -> list[dict[str, str]]`` — compute-apps VRAM-per-PID.
  Was synchronous (``subprocess.run``); now async. ``StubBackend`` -> ``[]``.
* ``query_process_utilization() -> list[dict]`` — ``nvidia-smi pmon -s u -c 1``
  sm%/mem%/enc%/dec% per PID. ``StubBackend`` -> ``[]``.

The sync UI bridge ``SystemDataCollector.query_gpu_processes()`` keeps its
synchronous contract (Textual ``compose()`` / screen callbacks cannot await)
by driving the now-async backend method to completion.

nvidia-smi field names live *only* inside ``NvidiaBackend`` (Constraint #7c).
"""

from __future__ import annotations

import asyncio
import inspect
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
# query_processes — now async (the load-bearing prerequisite)
# ---------------------------------------------------------------------------

_COMPUTE_APPS = b"1234, python, 8192\n5678, ollama, 2048\n"


class TestQueryProcessesAsync:
    def test_query_processes_is_a_coroutine_function(self):
        """The Protocol, NvidiaBackend and StubBackend must all be async — no
        synchronous ``subprocess.run`` reachable from the event loop."""
        assert inspect.iscoroutinefunction(NvidiaBackend.query_processes)
        assert inspect.iscoroutinefunction(StubBackend.query_processes)
        assert inspect.iscoroutinefunction(GPUBackend.query_processes)

    @pytest.mark.asyncio
    async def test_parses_pid_name_vram(self):
        """The canonical compute-apps parse: pid/name/vram_mb dicts."""
        proc = _mock_proc(_COMPUTE_APPS)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            procs = await NvidiaBackend().query_processes()

        assert procs == [
            {"pid": "1234", "name": "python", "vram_mb": "8192"},
            {"pid": "5678", "name": "ollama", "vram_mb": "2048"},
        ]

    @pytest.mark.asyncio
    async def test_uses_async_subprocess_not_subprocess_run(self):
        """query_processes must drive ``create_subprocess_exec`` (async), never
        the blocking ``subprocess.run``."""
        captured: dict[str, tuple] = {}

        def _fake_exec(*args, **kwargs):
            captured["args"] = args
            return _mock_proc(_COMPUTE_APPS)

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), patch(
            "subprocess.run", side_effect=AssertionError("must not call subprocess.run")
        ):
            await NvidiaBackend().query_processes()

        assert "nvidia-smi" in captured["args"][0]
        assert any("query-compute-apps" in a for a in captured["args"])

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_empty(self):
        proc = _mock_proc(b"", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await NvidiaBackend().query_processes() == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", return_value=_timeout_proc()):
            assert await NvidiaBackend().query_processes() == []

    @pytest.mark.asyncio
    async def test_missing_smi_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            assert await NvidiaBackend().query_processes() == []

    @pytest.mark.asyncio
    async def test_partial_columns_skipped(self):
        """A malformed line with < 3 columns is skipped, not fatal."""
        proc = _mock_proc(b"1234, python, 8192\nbrokenline\n9999, x, 16\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            procs = await NvidiaBackend().query_processes()
        assert procs == [
            {"pid": "1234", "name": "python", "vram_mb": "8192"},
            {"pid": "9999", "name": "x", "vram_mb": "16"},
        ]

    @pytest.mark.asyncio
    async def test_stub_returns_empty_list(self):
        assert await StubBackend().query_processes() == []


# ---------------------------------------------------------------------------
# query_process_utilization — new pmon method (also async from day one)
# ---------------------------------------------------------------------------

# ``nvidia-smi pmon -s u -c 1`` output: two header lines (prefixed ``#``) then
# one row per process: gpu_idx  pid  type  sm  mem  enc  dec  command.
_PMON = (
    b"# gpu        pid  type    sm    mem    enc    dec    command\n"
    b"# Idx          #   C/G     %      %      %      %    name\n"
    b"    0       1234     C    80     40      0      0    python\n"
    b"    0       5678     C    15     10      5      2    ollama\n"
)


class TestQueryProcessUtilization:
    def test_is_a_coroutine_function(self):
        assert inspect.iscoroutinefunction(NvidiaBackend.query_process_utilization)
        assert inspect.iscoroutinefunction(StubBackend.query_process_utilization)
        assert inspect.iscoroutinefunction(GPUBackend.query_process_utilization)

    @pytest.mark.asyncio
    async def test_parses_pmon_rows(self):
        proc = _mock_proc(_PMON)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            rows = await NvidiaBackend().query_process_utilization()

        assert rows == [
            {"pid": 1234, "name": "python", "sm_pct": 80, "mem_pct": 40,
             "enc_pct": 0, "dec_pct": 0},
            {"pid": 5678, "name": "ollama", "sm_pct": 15, "mem_pct": 10,
             "enc_pct": 5, "dec_pct": 2},
        ]

    @pytest.mark.asyncio
    async def test_runs_pmon_command(self):
        captured: dict[str, tuple] = {}

        def _fake_exec(*args, **kwargs):
            captured["args"] = args
            return _mock_proc(_PMON)

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await NvidiaBackend().query_process_utilization()

        assert "nvidia-smi" in captured["args"][0]
        assert "pmon" in captured["args"]

    @pytest.mark.asyncio
    async def test_missing_enc_dec_degrade_to_none(self):
        """Headless/older drivers omit enc/dec columns -> those fields None,
        the row is still returned with sm/mem populated."""
        pmon = (
            b"# gpu        pid  type    sm    mem    command\n"
            b"# Idx          #   C/G     %      %    name\n"
            b"    0       1234     C    80     40    python\n"
        )
        proc = _mock_proc(pmon)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            rows = await NvidiaBackend().query_process_utilization()
        assert rows == [
            {"pid": 1234, "name": "python", "sm_pct": 80, "mem_pct": 40,
             "enc_pct": None, "dec_pct": None},
        ]

    @pytest.mark.asyncio
    async def test_na_cells_degrade_to_none(self):
        """``-`` / ``[N/A]`` cells (idle GPU) degrade per-field to None."""
        pmon = (
            b"# gpu        pid  type    sm    mem    enc    dec    command\n"
            b"# Idx          #   C/G     %      %      %      %    name\n"
            b"    0       1234     C     -      -      -      -    python\n"
        )
        proc = _mock_proc(pmon)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            rows = await NvidiaBackend().query_process_utilization()
        assert rows == [
            {"pid": 1234, "name": "python", "sm_pct": None, "mem_pct": None,
             "enc_pct": None, "dec_pct": None},
        ]

    @pytest.mark.asyncio
    async def test_header_only_returns_empty(self):
        pmon = (
            b"# gpu        pid  type    sm    mem    enc    dec    command\n"
            b"# Idx          #   C/G     %      %      %      %    name\n"
        )
        proc = _mock_proc(pmon)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await NvidiaBackend().query_process_utilization() == []

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_empty(self):
        proc = _mock_proc(b"", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await NvidiaBackend().query_process_utilization() == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", return_value=_timeout_proc()):
            assert await NvidiaBackend().query_process_utilization() == []

    @pytest.mark.asyncio
    async def test_missing_smi_returns_empty(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            assert await NvidiaBackend().query_process_utilization() == []

    @pytest.mark.asyncio
    async def test_stub_returns_empty_list(self):
        assert await StubBackend().query_process_utilization() == []


# ---------------------------------------------------------------------------
# Protocol conformance — both new/changed async methods are part of the contract
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_nvidia_backend_satisfies_protocol(self):
        assert isinstance(NvidiaBackend(), GPUBackend)

    def test_stub_backend_satisfies_protocol(self):
        assert isinstance(StubBackend(), GPUBackend)


# ---------------------------------------------------------------------------
# Sync UI bridge — SystemDataCollector.query_gpu_processes keeps its sync
# contract while the backend underneath is async (no modal refactor here).
# ---------------------------------------------------------------------------

class TestSyncBridge:
    def test_bridge_returns_backend_processes_no_loop(self):
        """Called with no running loop, the sync bridge drives the async backend
        to completion and returns its parsed list."""
        from bastion.dashboard.collectors import SystemDataCollector

        fake_backend = MagicMock()
        fake_backend.query_processes = AsyncMock(
            return_value=[{"pid": "1", "name": "ollama", "vram_mb": "1024"}]
        )
        with patch("bastion.gpu.get_backend", return_value=fake_backend):
            result = SystemDataCollector.query_gpu_processes()
        assert result == [{"pid": "1", "name": "ollama", "vram_mb": "1024"}]

    @pytest.mark.asyncio
    async def test_bridge_works_from_within_running_loop(self):
        """Textual's compose()/callbacks run inside a live event loop; the sync
        bridge must not deadlock or raise when a loop is already running."""
        from bastion.dashboard.collectors import SystemDataCollector

        fake_backend = MagicMock()
        fake_backend.query_processes = AsyncMock(
            return_value=[{"pid": "9", "name": "x", "vram_mb": "16"}]
        )
        with patch("bastion.gpu.get_backend", return_value=fake_backend):
            # Run the *synchronous* bridge in a worker thread so the current
            # event loop keeps spinning (mirrors Textual's call sites).
            result = await asyncio.to_thread(SystemDataCollector.query_gpu_processes)
        assert result == [{"pid": "9", "name": "x", "vram_mb": "16"}]
