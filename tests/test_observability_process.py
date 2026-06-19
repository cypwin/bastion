"""Tests for the always-on process-attribution collector (observability T1).

Covers ``SystemDataCollector.collect_process_snapshot()`` (spec 5.3 / 4.5):
  - own-PID registry: ``os.getpid()`` -> 'bastion'; the Ollama process matched
    by name (+ optional port from ``BrokerConfig``) -> 'ollama';
  - top-N by CPU and by memory via psutil, joined into one ``ProcessRow`` set;
  - per-process ``io_counters()`` bytes/s via a delta; ``AccessDenied`` on a
    process keeps the row with ``io_*`` None (never drop, never a misleading 0);
  - a user watchlist (names or ``pid:NNN``) always tagged ``watchlisted=True``;
  - a bounded churn detector (``psutil.pids()`` set-diff, ``deque(maxlen=10)``);
  - an async pmon/compute-apps join mapping GPU VRAM + SM% onto the same PIDs
    through the ``GPUBackend`` seam (empty on ``StubBackend`` — no GPU rows, no
    error).

This data is **TUI + JSON only** — never a Prometheus label (spec 4.5 / 5.3).
Tests feed fake psutil process objects so they run on any host (no real GPU).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from bastion.dashboard.collectors import SystemDataCollector
from bastion.models import (
    ProcessChurnEvent,
    ProcessRow,
    ProcessSnapshot,
)


# ---------------------------------------------------------------------------
# Fake psutil process objects
# ---------------------------------------------------------------------------


class _FakeIO:
    def __init__(self, read_bytes: int, write_bytes: int) -> None:
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes


class _FakeMem:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeAccessDenied(Exception):
    """Stands in for ``psutil.AccessDenied`` in the fakes."""


class _FakeProc:
    """Minimal psutil.Process stand-in driven by an ``info`` dict.

    ``io_counters()`` either returns a ``_FakeIO`` or raises to simulate the
    per-process ``AccessDenied`` path (the row must survive with io fields None).
    """

    def __init__(
        self,
        pid: int,
        name: str,
        cpu: float,
        rss_mb: float,
        io: _FakeIO | None = None,
        io_denied: bool = False,
    ) -> None:
        self._io = io
        self._io_denied = io_denied
        self.info: dict[str, Any] = {
            "pid": pid,
            "name": name,
            "cpu_percent": cpu,
            "memory_info": _FakeMem(int(rss_mb * 1024 * 1024)),
        }

    @property
    def pid(self) -> int:
        return self.info["pid"]

    def io_counters(self) -> _FakeIO:
        if self._io_denied:
            import psutil

            raise psutil.AccessDenied(self.info["pid"])
        assert self._io is not None
        return self._io


def _config_with(watchlist: list[str] | None = None, churn_threshold: int = 5):
    from bastion.models import BrokerConfig

    cfg = BrokerConfig()
    cfg.observability.process_watchlist = watchlist or []
    cfg.observability.churn_threshold = churn_threshold
    return cfg


# ---------------------------------------------------------------------------
# Shape + own-PID tagging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_shape_and_own_pid_tagging() -> None:
    """Own-PID registry tags this process 'bastion' and an ollama match 'ollama'."""
    me = os.getpid()
    procs = [
        _FakeProc(me, "python", cpu=10.0, rss_mb=500.0, io=_FakeIO(0, 0)),
        _FakeProc(4242, "ollama", cpu=30.0, rss_mb=4096.0, io=_FakeIO(0, 0)),
        _FakeProc(99, "bash", cpu=1.0, rss_mb=10.0, io=_FakeIO(0, 0)),
    ]
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids",
        return_value=[me, 4242, 99],
    ):
        snap = await collector.collect_process_snapshot(_config_with())

    assert isinstance(snap, ProcessSnapshot)
    # own_pids maps pid -> role; this process is 'bastion', ollama is 'ollama'.
    assert snap.own_pids.get(me) == "bastion"
    assert snap.own_pids.get(4242) == "ollama"
    # The corresponding rows carry is_inference_owned + role.
    by_pid = {r.pid: r for r in snap.top_processes}
    assert by_pid[me].is_inference_owned and by_pid[me].role == "bastion"
    assert by_pid[4242].is_inference_owned and by_pid[4242].role == "ollama"
    assert not by_pid[99].is_inference_owned


@pytest.mark.asyncio
async def test_top_n_includes_high_memory_low_cpu() -> None:
    """Top-N is the union of top-by-CPU and top-by-memory (4.5 composite)."""
    procs = [
        _FakeProc(i, f"p{i}", cpu=float(i), rss_mb=10.0, io=_FakeIO(0, 0))
        for i in range(1, 12)
    ]
    # A low-CPU, very-high-memory process must still appear via the memory axis.
    procs.append(_FakeProc(999, "bigmem", cpu=0.1, rss_mb=64000.0, io=_FakeIO(0, 0)))
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids",
        return_value=[p.pid for p in procs],
    ):
        snap = await collector.collect_process_snapshot(_config_with())
    pids = {r.pid for r in snap.top_processes}
    assert 999 in pids, "high-memory low-CPU process must survive the top-N cut"


# ---------------------------------------------------------------------------
# Per-process AccessDenied keeps the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_denied_io_keeps_row_with_none_fields() -> None:
    """A process whose io_counters() raises AccessDenied is KEPT (io_* None)."""
    procs = [
        _FakeProc(11, "denied", cpu=5.0, rss_mb=100.0, io_denied=True),
        _FakeProc(12, "ok", cpu=4.0, rss_mb=50.0, io=_FakeIO(1_000_000, 2_000_000)),
    ]
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[11, 12]
    ):
        snap = await collector.collect_process_snapshot(_config_with())
    by_pid = {r.pid: r for r in snap.top_processes}
    assert 11 in by_pid, "AccessDenied row must NOT be dropped"
    assert by_pid[11].io_read_bytes_s is None
    assert by_pid[11].io_write_bytes_s is None


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchlist_by_name_tags_and_pins_regardless_of_rank() -> None:
    """A watchlisted name appears in watchlist_hits regardless of CPU rank."""
    procs = [
        _FakeProc(1, "python3", cpu=0.0, rss_mb=5.0, io=_FakeIO(0, 0)),
        _FakeProc(2, "other", cpu=90.0, rss_mb=5.0, io=_FakeIO(0, 0)),
    ]
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[1, 2]
    ):
        snap = await collector.collect_process_snapshot(
            _config_with(watchlist=["python3"])
        )
    hit_names = {r.name for r in snap.watchlist_hits}
    assert "python3" in hit_names
    assert all(r.watchlisted for r in snap.watchlist_hits)


@pytest.mark.asyncio
async def test_watchlist_by_pid() -> None:
    """A ``pid:NNN`` watchlist entry matches that PID."""
    procs = [_FakeProc(12345, "training", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[12345]
    ):
        snap = await collector.collect_process_snapshot(
            _config_with(watchlist=["pid:12345"])
        )
    assert any(r.pid == 12345 and r.watchlisted for r in snap.watchlist_hits)


@pytest.mark.asyncio
async def test_empty_watchlist_yields_no_hits() -> None:
    procs = [_FakeProc(1, "x", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[1]
    ):
        snap = await collector.collect_process_snapshot(_config_with(watchlist=[]))
    assert snap.watchlist_hits == []


# ---------------------------------------------------------------------------
# Churn detector (bounded deque)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_churn_detector_fires_and_is_bounded() -> None:
    """A burst of new PIDs above the threshold emits a churn event; deque bounded."""
    collector = SystemDataCollector()
    cfg = _config_with(churn_threshold=5)
    procs = [_FakeProc(1, "a", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]

    # First slow-tick call primes the PID baseline (no event yet).
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[1]
    ):
        await collector.collect_process_snapshot(cfg, slow_tick=True)

    # Now spawn 6 new PIDs (> threshold 5) -> a ProcessChurnEvent.
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids",
        return_value=[1, 2, 3, 4, 5, 6, 7],
    ):
        snap = await collector.collect_process_snapshot(cfg, slow_tick=True)

    assert len(snap.recent_churn_events) >= 1
    ev = snap.recent_churn_events[-1]
    assert isinstance(ev, ProcessChurnEvent)
    assert ev.new_count >= 6

    # Drive 15 churn ticks; the deque must stay bounded at maxlen=10.
    for k in range(15):
        base = list(range(1, 2 + k * 10))
        nxt = list(range(1, 2 + (k + 1) * 10))
        with patch(
            "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
        ), patch(
            "bastion.dashboard.collectors.psutil.pids", return_value=nxt
        ):
            snap = await collector.collect_process_snapshot(cfg, slow_tick=True)
    assert len(snap.recent_churn_events) <= 10


@pytest.mark.asyncio
async def test_churn_below_threshold_no_event() -> None:
    collector = SystemDataCollector()
    cfg = _config_with(churn_threshold=5)
    procs = [_FakeProc(1, "a", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[1]
    ):
        await collector.collect_process_snapshot(cfg, slow_tick=True)
    # +2 new PIDs only (< threshold 5).
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[1, 2, 3]
    ):
        snap = await collector.collect_process_snapshot(cfg, slow_tick=True)
    assert snap.recent_churn_events == []


# ---------------------------------------------------------------------------
# GPU join (StubBackend -> no GPU rows, no error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_join_empty_on_stub_backend() -> None:
    """On a StubBackend host the GPU rows are empty — the panel shows CPU/IO only."""
    from bastion.gpu import set_backend
    from bastion.gpu.stub import StubBackend

    set_backend(StubBackend())
    try:
        procs = [_FakeProc(1, "x", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]
        collector = SystemDataCollector()
        with patch(
            "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
        ), patch(
            "bastion.dashboard.collectors.psutil.pids", return_value=[1]
        ):
            snap = await collector.collect_process_snapshot(
                _config_with(), slow_tick=True
            )
        assert snap.gpu_processes == []
    finally:
        set_backend(StubBackend())


@pytest.mark.asyncio
async def test_gpu_join_maps_vram_and_sm_onto_pids() -> None:
    """Compute-apps VRAM + pmon SM% are joined onto the matching ProcessGPURow."""
    me = os.getpid()

    class _Backend:
        async def query_processes(self) -> list[dict]:
            return [{"pid": str(me), "name": "ollama", "vram_mb": "8192"}]

        async def query_process_utilization(self) -> list[dict]:
            return [
                {
                    "pid": me,
                    "name": "ollama",
                    "sm_pct": 80,
                    "mem_pct": 40,
                    "enc_pct": None,
                    "dec_pct": None,
                }
            ]

    from bastion.gpu import set_backend
    from bastion.gpu.stub import StubBackend

    set_backend(_Backend())  # type: ignore[arg-type]
    try:
        procs = [_FakeProc(me, "ollama", cpu=1.0, rss_mb=5.0, io=_FakeIO(0, 0))]
        collector = SystemDataCollector()
        with patch(
            "bastion.dashboard.collectors.psutil.process_iter", return_value=procs
        ), patch(
            "bastion.dashboard.collectors.psutil.pids", return_value=[me]
        ):
            snap = await collector.collect_process_snapshot(
                _config_with(), slow_tick=True
            )
        gpu_by_pid = {g.pid: g for g in snap.gpu_processes}
        assert me in gpu_by_pid
        assert gpu_by_pid[me].vram_mb == 8192
        assert gpu_by_pid[me].sm_pct == 80
        # The own-PID registry tags this GPU row as inference-owned too.
        assert gpu_by_pid[me].is_inference_owned
    finally:
        set_backend(StubBackend())


# ---------------------------------------------------------------------------
# Never an exception on a broken source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collector_degrades_to_empty_snapshot_on_failure() -> None:
    """A wholesale psutil failure yields a valid (empty) snapshot, never raises."""
    collector = SystemDataCollector()
    with patch(
        "bastion.dashboard.collectors.psutil.process_iter",
        side_effect=RuntimeError("boom"),
    ), patch(
        "bastion.dashboard.collectors.psutil.pids", return_value=[]
    ):
        snap = await collector.collect_process_snapshot(_config_with())
    assert isinstance(snap, ProcessSnapshot)
    assert snap.top_processes == []
