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
        with patch("bastion.paths.audit_log_path", return_value=audit_path):
            with TestClient(app) as client:
                resp = client.get("/broker/status")
    assert resp.status_code == 200
    data = resp.json()
    # Keys must exist even when empty, so panels render rather than break
    assert "a2a_tasks" in data
    assert "active_leases" in data
    assert "recent_audit_events" in data
    assert "a2a_summary" in data
