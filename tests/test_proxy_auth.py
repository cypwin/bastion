"""Tests that proxy catch-all routes require auth when auth is enabled."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bastion.models import AuthConfig, BrokerConfig
from bastion.server import create_app


def _config_with_auth() -> BrokerConfig:
    cfg = BrokerConfig()
    cfg.auth = AuthConfig(enabled=True, api_keys=["test-key-1"])
    return cfg


@pytest.fixture(autouse=True)
def _redirect_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect audit log to a writable tmp directory for all tests in this module."""
    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))


def test_proxy_api_returns_401_without_key():
    app = create_app(_config_with_auth())
    with TestClient(app) as client:
        resp = client.get("/api/tags")
    assert resp.status_code == 401


def test_proxy_v1_returns_401_without_key():
    app = create_app(_config_with_auth())
    with TestClient(app) as client:
        resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_proxy_api_returns_401_with_bad_key():
    app = create_app(_config_with_auth())
    with TestClient(app) as client:
        resp = client.get("/api/tags", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_proxy_api_allows_through_with_valid_key_when_auth_on():
    # Without Ollama running we expect 503 from the uninitialized _proxy,
    # not 401. The point: auth accepted, request reached the handler.
    app = create_app(_config_with_auth())
    with TestClient(app) as client:
        resp = client.get("/api/tags", headers={"Authorization": "Bearer test-key-1"})
    assert resp.status_code != 401


def test_proxy_api_open_when_auth_disabled():
    # Backwards compat: auth off → proxy stays open (matches Ollama default).
    cfg = BrokerConfig()
    cfg.auth = AuthConfig(enabled=False)
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.get("/api/tags")
    assert resp.status_code != 401
