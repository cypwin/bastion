"""Tests that /broker/status populates dashboard fields."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from bastion.models import BrokerConfig
from bastion.server import create_app


def test_broker_status_has_a2a_summary_key():
    app = create_app(BrokerConfig())
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = os.path.join(tmpdir, "bastion-audit.jsonl")
        with (
            patch("bastion.paths.audit_log_path", return_value=audit_path),
            TestClient(app) as client,
        ):
            resp = client.get("/broker/status")
    assert resp.status_code == 200
    data = resp.json()
    # Keys must exist even when empty, so panels render rather than break
    assert "a2a_tasks" in data
    assert "active_leases" in data
    assert "recent_audit_events" in data
    assert "a2a_summary" in data


def test_broker_counters_shape_and_reset_epoch_stable():
    """GET /broker/counters returns all five fields; reset_epoch is stable."""
    app = create_app(BrokerConfig())
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_path = os.path.join(tmpdir, "bastion-audit.jsonl")
        with (
            patch("bastion.paths.audit_log_path", return_value=audit_path),
            TestClient(app) as client,
        ):
            resp1 = client.get("/broker/counters")
            resp2 = client.get("/broker/counters")

    assert resp1.status_code == 200
    data = resp1.json()

    # All five fields must be present
    assert "reset_epoch" in data
    assert "total_requests_served" in data
    assert "total_dispatched" in data
    assert "model_swap_total" in data
    assert "thrashing_halt_total" in data

    # Counter fields are non-negative integers
    assert isinstance(data["total_requests_served"], int)
    assert data["total_requests_served"] >= 0
    assert isinstance(data["total_dispatched"], int)
    assert data["total_dispatched"] >= 0
    assert isinstance(data["model_swap_total"], int)
    assert data["model_swap_total"] >= 0
    assert isinstance(data["thrashing_halt_total"], int)
    assert data["thrashing_halt_total"] >= 0

    # reset_epoch is a non-empty string (ISO-8601 UTC timestamp)
    assert isinstance(data["reset_epoch"], str)
    assert data["reset_epoch"] != ""

    # reset_epoch is stable across multiple calls within one process lifetime
    assert resp2.status_code == 200
    assert resp2.json()["reset_epoch"] == data["reset_epoch"]
