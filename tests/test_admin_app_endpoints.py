"""Admin-app (two-port mode) copies of /broker/latency, /broker/catalog,
and /broker/version.

create_admin_app() registers its own copies of these handlers; until S130
only the create_app() copies were tested, so a drift or breakage in the
admin-port duplicates would not fail the suite. These tests exercise the
duplicated handler bodies over HTTP.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    ModelInfo,
    OllamaConfig,
    ServerConfig,
)
from bastion.server import create_admin_app


@pytest.fixture
def admin_client() -> TestClient:
    config = BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434, admin_port=9999),
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "nomic-embed-text": ModelInfo(vram_gb=0.4, always_allowed=True),
        },
    )
    return TestClient(create_admin_app(config))


class TestAdminAppVersion:
    def test_returns_200_with_contract_keys(self, admin_client) -> None:
        resp = admin_client.get("/broker/version")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("version", "git_sha", "boot_time_unix", "boot_time_iso"):
            assert key in body, f"missing key '{key}'"


class TestAdminAppCatalog:
    def test_returns_200_with_registry_entries(self, admin_client) -> None:
        resp = admin_client.get("/broker/catalog")
        assert resp.status_code == 200
        body = resp.json()
        assert {e["name"] for e in body["models"]} == {
            "qwen3:14b",
            "nomic-embed-text",
        }
        for key in (
            "total",
            "loaded_count",
            "evictable_count",
            "registry_source",
            "snapshot_age_s",
            "residency_state",
        ):
            assert key in body, f"missing key '{key}'"


class TestAdminAppLatency:
    def test_returns_200_with_contract_keys(self, admin_client) -> None:
        resp = admin_client.get("/broker/latency")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "window_s",
            "requested_window_s",
            "sample_total",
            "per_model",
            "overall",
        ):
            assert key in body, f"missing key '{key}'"

    def test_window_param_is_clamped(self, admin_client) -> None:
        resp = admin_client.get("/broker/latency", params={"window_s": 999999})
        assert resp.status_code == 200
        assert resp.json()["requested_window_s"] <= 3600
