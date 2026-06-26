"""SRV2: /broker/status brake snapshot + POST /broker/swap-brake admin override.

Both app factories (create_app, create_admin_app) must expose the swap-brake
snapshot on /broker/status and accept the auto-expiring admin override on
POST /broker/swap-brake. These run the real lifespan so a genuine Scheduler
(hence a real SwapBrake) is wired into the module globals.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import bastion.audit as audit
import bastion.server as srv
from bastion.models import BrokerConfig
from bastion.server import create_admin_app, create_app

_BRAKE_STATUS_KEYS = (
    "brake_state",
    "brake_reason",
    "cooloff_remaining_s",
    "windowed_rate_per_min",
    "backoff_level",
    "pinned_models",
    "pinned_vram_gb",
    "hardware_gate_blind",
)


@contextmanager
def _client(factory):
    app = factory(BrokerConfig())
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = os.path.join(tmpdir, "bastion-audit.jsonl")
        from unittest.mock import patch

        with (
            patch("bastion.paths.audit_log_path", return_value=audit_path),
            TestClient(app) as client,
        ):
            yield client


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_status_exposes_brake_snapshot(factory):
    with _client(factory) as client:
        resp = client.get("/broker/status")

    assert resp.status_code == 200
    data = resp.json()
    for key in _BRAKE_STATUS_KEYS:
        assert key in data, f"missing brake snapshot key '{key}'"
    # A freshly-started broker has a CLOSED brake.
    assert data["brake_state"] == "closed"
    assert isinstance(data["backoff_level"], int)
    assert isinstance(data["pinned_models"], list)
    assert data["cooloff_remaining_s"] == 0.0
    assert isinstance(data["hardware_gate_blind"], bool)


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_swap_brake_force_engage_and_auto_expire(factory):
    with _client(factory) as client:
        brake = srv._scheduler.swap_brake  # real SwapBrake from lifespan
        # Drive the brake on a controllable clock well past its init epoch so
        # min-spacing/tokens are satisfied when the override is NOT active.
        base = brake._clock()
        t = {"now": base + 100_000.0}
        brake._clock = lambda: t["now"]

        resp = client.post("/broker/swap-brake", json={"release": False, "ttl_s": 50.0})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "force_engage"
        assert body["ttl_s"] == 50.0
        assert "snapshot" in body

        # Engaged now: the gate stalls with the force-engaged reason.
        decision = brake.peek("any-model")
        assert decision.action == "stall"
        assert decision.reason == "force-engaged"

        # Auto-expiry: advance the clock past ttl_s — the override lifts itself.
        t["now"] += 100.0
        assert brake.peek("any-model").reason != "force-engaged"


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_swap_brake_force_release_maps_to_force(factory):
    with _client(factory) as client:
        brake = srv._scheduler.swap_brake
        base = brake._clock()
        t = {"now": base + 100_000.0}
        brake._clock = lambda: t["now"]

        resp = client.post("/broker/swap-brake", json={"release": True, "ttl_s": 30.0})
        assert resp.status_code == 200
        assert resp.json()["status"] == "force_release"

        # Force-release is active: acquire proceeds with the force-released reason.
        decision = brake.acquire("any-model")
        assert decision.action == "proceed"
        assert decision.reason == "force-released"


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_swap_brake_emits_audit_event(factory):
    with _client(factory) as client:
        resp = client.post("/broker/swap-brake", json={"release": False, "ttl_s": 10.0})
        assert resp.status_code == 200
        events = audit.recent_events(50)
    assert any(e.get("event") == "swap_brake_override" for e in events)


@pytest.mark.parametrize("factory", [create_app, create_admin_app])
def test_swap_brake_rejects_negative_ttl(factory):
    with _client(factory) as client:
        resp = client.post("/broker/swap-brake", json={"release": False, "ttl_s": -1.0})
    assert resp.status_code == 400
