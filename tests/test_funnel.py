"""Load-path funnel tests (SRV1 + REG).

Both ``/broker/preload`` routes are residency-INCREASING load paths. They must
pass through the SAME non-skippable chokepoint as the scheduler swap: the load
serializer, with the swap brake's authoritative ``acquire()`` running inside it,
``record_load()`` after a successful load. A direct ``keep_alive:-1`` POST that
bypassed the serializer would be the unbounded swap-velocity hole the brake
exists to close.

These tests inject lightweight fakes for the scheduler + VRAM tracker globals
(no real lifespan / Ollama), and intercept ``httpx.AsyncClient`` so every
``/api/generate`` POST is classified by residency-delta AT CALL TIME.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import bastion.server as srv
from bastion.models import BrokerConfig, SwapBrakeConfig
from bastion.ratelimit import RateLimitMiddleware
from bastion.server import create_admin_app, create_app
from bastion.swapbrake import BrakeDecision, BrakeState, SwapBrake

# ── lightweight fakes ──────────────────────────────────────────────────


class _FakeBrake:
    def __init__(self, decision: BrakeDecision) -> None:
        self._decision = decision
        self.acquired: list[str] = []
        self.loaded: list[str] = []

    def acquire(self, model: str) -> BrakeDecision:
        self.acquired.append(model)
        return self._decision

    def record_load(self, model: str) -> None:
        self.loaded.append(model)


class _FakeScheduler:
    def __init__(self, brake: _FakeBrake) -> None:
        self.swap_brake = brake
        self.load_serializer = asyncio.Semaphore(1)


class _FakeLoadedModel:
    def __init__(self, name: str, vram_gb: float = 1.0) -> None:
        self.name = name
        self.vram_gb = vram_gb


class _FakeTracker:
    def __init__(self, can_load: bool = True, resident: set[str] | None = None) -> None:
        self._can_load = can_load
        self._pinned: set[str] = set()
        self._resident: set[str] = resident if resident is not None else set()

    async def can_load_model(self, model: str) -> tuple[bool, str]:
        return (self._can_load, "" if self._can_load else "no room")


class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        pass


def _make_recording_client_factory(records: list[dict[str, Any]]):
    """An ``httpx.AsyncClient`` drop-in that records every POST + residency-delta."""

    class _RecordingClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _RecordingClient:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, url: str, json: dict | None = None, **_k: Any) -> _FakeResponse:
            body = json or {}
            model = body.get("model")
            keep_alive = body.get("keep_alive")
            sched = srv._scheduler
            tracker = srv._vram_tracker
            resident_before = set(getattr(tracker, "_resident", set()))
            # Residency-INCREASING iff this is a load (keep_alive != 0) of a
            # model that was NOT resident immediately before this POST.
            increasing = keep_alive != 0 and model not in resident_before
            records.append(
                {
                    "url": url,
                    "model": model,
                    "keep_alive": keep_alive,
                    "increasing": increasing,
                    "serializer_locked": (
                        sched.load_serializer.locked() if sched is not None else None
                    ),
                }
            )
            return _FakeResponse()

    return _RecordingClient


def _unguarded_residency_increases(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """RUNTIME gate: residency-increasing POSTs that did NOT hold the serializer."""
    return [r for r in records if r["increasing"] and not r["serializer_locked"]]


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_globals():
    saved = (srv._scheduler, srv._vram_tracker, srv._vram_manager)
    yield
    srv._scheduler, srv._vram_tracker, srv._vram_manager = saved


@pytest.fixture
def records() -> list[dict[str, Any]]:
    return []


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch, records):
    monkeypatch.setattr(srv.httpx, "AsyncClient", _make_recording_client_factory(records))


def _install(brake_decision: BrakeDecision, *, can_load: bool = True,
             resident: set[str] | None = None) -> _FakeBrake:
    brake = _FakeBrake(brake_decision)
    srv._scheduler = _FakeScheduler(brake)  # type: ignore[assignment]
    srv._vram_tracker = _FakeTracker(can_load=can_load, resident=resident)  # type: ignore[assignment]
    srv._vram_manager = None
    return brake


_PROCEED = BrakeDecision(action="proceed", reason="ok", retry_after_s=0.0)
_SHED = BrakeDecision(action="shed", reason="infeasible set", retry_after_s=4.0)
_STALL = BrakeDecision(action="stall", reason="swap brake OPEN (cooloff)", retry_after_s=7.0)


# ── SRV1: both routes hold serializer + consult brake ──────────────────


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_preload_holds_serializer_and_consults_brake_before_load(factory, records):
    brake = _install(_PROCEED)
    client = TestClient(factory(BrokerConfig()))

    resp = client.post("/broker/preload", json={"model": "qwen3:14b"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "loaded", "model": "qwen3:14b"}
    # Brake consulted, then debited, exactly once.
    assert brake.acquired == ["qwen3:14b"]
    assert brake.loaded == ["qwen3:14b"]
    # Exactly one keep_alive:-1 load POST, held under the serializer.
    loads = [r for r in records if r["model"] == "qwen3:14b"]
    assert len(loads) == 1
    assert loads[0]["keep_alive"] == -1
    assert loads[0]["serializer_locked"] is True


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_none_scheduler_sheds_503_without_bypass(factory, records):
    # Tracker present, scheduler absent: MUST shed, never fall through to a
    # direct ungated keep_alive:-1 load.
    srv._scheduler = None
    srv._vram_tracker = _FakeTracker(can_load=True)  # type: ignore[assignment]
    srv._vram_manager = None
    client = TestClient(factory(BrokerConfig()))

    resp = client.post("/broker/preload", json={"model": "qwen3:14b"})

    assert resp.status_code == 503
    assert resp.json().get("reason_code") == "scheduler_unavailable"
    assert records == []  # NO bypass load happened


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
@pytest.mark.parametrize("decision", [_SHED, _STALL])
def test_braked_preload_sheds_503_with_retry_after_and_throttle(
    factory, decision, records, monkeypatch
):
    brake = _install(decision)
    throttled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        RateLimitMiddleware,
        "throttle",
        lambda self, caller, model: throttled.append((caller, model)),
    )
    client = TestClient(factory(BrokerConfig()))

    resp = client.post("/broker/preload", json={"model": "qwen3:14b"})

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") is not None
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.json()["reason_code"] == f"swap_brake_{decision.action}"
    # Brake consulted but NO load debited and NO keep_alive:-1 POST emitted.
    assert brake.acquired == ["qwen3:14b"]
    assert brake.loaded == []
    assert records == []
    # Admission coupling: the caller is throttled on the public app (which has
    # the rate limiter). The admin app has no RateLimitMiddleware, so the hook
    # is a best-effort no-op there — but the shed (503 + Retry-After) still holds.
    if factory is create_app:
        assert len(throttled) == 1
        assert throttled[0][1] == "qwen3:14b"
    else:
        assert throttled == []


# ── REG: runtime residency-delta funnel regression ─────────────────────


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_funnel_regression_residency_increasing_posts_are_serializer_held(factory, records):
    # Cold model (not resident): the preload POST is residency-increasing and
    # MUST be observed under the held serializer on BOTH preload routes.
    _install(_PROCEED, resident={"already-here"})
    client = TestClient(factory(BrokerConfig()))

    resp = client.post("/broker/preload", json={"model": "cold-model"})
    assert resp.status_code == 200

    increasing = [r for r in records if r["increasing"]]
    assert increasing, "expected a residency-increasing load POST"
    # The runtime gate: every residency-increase held the serializer.
    assert _unguarded_residency_increases(records) == []


def test_regression_gate_is_a_real_runtime_check_not_a_noop():
    # Prove the gate actually FLAGS an unguarded residency-increasing load
    # (the RED case): an increasing POST observed without the serializer held.
    synthetic = [
        {"model": "m", "keep_alive": -1, "increasing": True, "serializer_locked": False},
        {"model": "m", "keep_alive": 0, "increasing": False, "serializer_locked": False},
    ]
    flagged = _unguarded_residency_increases(synthetic)
    assert len(flagged) == 1
    assert flagged[0]["model"] == "m"


def test_coresident_and_unload_posts_are_not_flagged():
    # Co-resident inference (model already resident) needs NO serializer, and
    # keep_alive:0 unloads are excluded — neither is a residency-increase.
    records = [
        {"model": "hot", "keep_alive": -1, "increasing": False, "serializer_locked": False},
        {"model": "hot", "keep_alive": 0, "increasing": False, "serializer_locked": False},
    ]
    assert _unguarded_residency_increases(records) == []


# ── F-1 — the preload funnel is the SECOND acquire() site; it must also abort an
#         orphaned HALF_OPEN probe when a load doesn't record (else the brake wedges)
# ─────────────────────────────────────────────────────────────────────────


class _FlipTracker:
    """Tracker whose can_load_model returns a scripted sequence of verdicts, so the
    cheap pre-check (1st call) can pass while the in-serializer re-check (2nd) fails."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self._pinned: set[str] = set()
        self._resident: set[str] = set()

    async def can_load_model(self, model: str) -> tuple[bool, str]:
        ok = self._results.pop(0) if self._results else True
        return (ok, "" if ok else "no room")


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _primed_halfopen_brake() -> SwapBrake:
    """A real SwapBrake driven to OPEN past its cooloff with a healthy bucket, so the
    NEXT acquire() (the one inside the preload serializer) grants the single probe."""
    clk = _FakeClock()
    cfg = SwapBrakeConfig(
        min_spacing_seconds=0.0, bucket_capacity=3.0, refill_per_minute=0.0,
        cooloff_seconds=30.0, min_state_hold_seconds=5.0, release_rate_per_minute=3.0,
    )
    b = SwapBrake(cfg, clock=clk)
    for _ in range(3):
        b.acquire("m")
        b.record_load("m")
    for _ in range(60):
        b.acquire("m")
        clk.advance(0.1)
    assert b.snapshot()["state"] == BrakeState.OPEN
    clk.advance(31.0)
    clk.advance(60.0)
    b._tokens = float(cfg.bucket_capacity)
    return b


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_preload_no_fit_recheck_aborts_orphan_probe(factory):
    """The in-serializer no-fit re-check returns 409 AFTER acquire() granted the
    HALF_OPEN probe. Without abort_probe the probe is orphaned and every later
    acquire() (scheduler swaps AND preloads) wedges at 'half-open probe in flight'."""
    brake = _primed_halfopen_brake()
    srv._scheduler = _FakeScheduler(brake)  # type: ignore[assignment]
    srv._vram_tracker = _FlipTracker([True, False])  # pre-check ok, re-check no-fit
    srv._vram_manager = None
    client = TestClient(factory(BrokerConfig()))

    resp = client.post("/broker/preload", json={"model": "qwen3:14b"})

    assert resp.status_code == 409
    # The granted probe must be aborted, not orphaned → brake re-OPENed, not wedged.
    assert brake._probe_outstanding is False
    assert brake.snapshot()["state"] == BrakeState.OPEN
    # And a subsequent acquire is not stuck at 'half-open probe in flight'.
    assert brake.acquire("qwen3:14b").reason != "half-open probe in flight"


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_preload_post_failure_aborts_orphan_probe(factory, monkeypatch):
    """The cold-load httpx POST raises (Ollama down/timeout) AFTER acquire() granted
    the probe. The orphaned probe must still be aborted before the error propagates."""
    brake = _primed_halfopen_brake()
    srv._scheduler = _FakeScheduler(brake)  # type: ignore[assignment]
    srv._vram_tracker = _FlipTracker([True, True])  # both fit checks pass
    srv._vram_manager = None

    class _RaisingClient:
        def __init__(self, *_a, **_k) -> None:
            pass

        async def __aenter__(self) -> "_RaisingClient":
            return self

        async def __aexit__(self, *_a) -> bool:
            return False

        async def post(self, *_a, **_k):
            raise srv.httpx.ConnectError("Ollama unreachable")

    monkeypatch.setattr(srv.httpx, "AsyncClient", _RaisingClient)
    client = TestClient(factory(BrokerConfig()), raise_server_exceptions=False)

    client.post("/broker/preload", json={"model": "qwen3:14b"})

    assert brake._probe_outstanding is False
    assert brake.snapshot()["state"] == BrakeState.OPEN
