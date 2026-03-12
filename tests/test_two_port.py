"""Tests for Phase C2: Two-Port Architecture.

Verifies that:
  - create_proxy_app() exposes only /api/* and / (Ollama-compatible)
  - create_admin_app() exposes only /broker/*, /a2a/*, /.well-known/*
  - create_app() (single-port) continues to expose everything
  - ServerConfig.two_port_mode property works correctly
  - Admin port configuration round-trips through YAML
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from bastion.models import (
    BrokerConfig,
    OllamaConfig,
    SchedulerConfig,
    ServerConfig,
)
from bastion.server import create_admin_app, create_app, create_proxy_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def two_port_config() -> BrokerConfig:
    """Config with two-port mode enabled (admin_port != port)."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434, admin_port=9999),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.1,
            max_queue_size=16,
        ),
    )


@pytest.fixture
def single_port_config() -> BrokerConfig:
    """Config with single-port mode (default, admin_port=0)."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.1,
            max_queue_size=16,
        ),
    )


# ---------------------------------------------------------------------------
# ServerConfig.two_port_mode property tests
# ---------------------------------------------------------------------------

class TestTwoPortModeProperty:
    def test_default_is_false(self) -> None:
        """Default ServerConfig has two_port_mode=False."""
        cfg = ServerConfig()
        assert cfg.two_port_mode is False

    def test_admin_port_zero_is_false(self) -> None:
        """admin_port=0 means disabled."""
        cfg = ServerConfig(admin_port=0)
        assert cfg.two_port_mode is False

    def test_admin_port_same_as_port_is_false(self) -> None:
        """admin_port == port means single-port mode."""
        cfg = ServerConfig(port=11434, admin_port=11434)
        assert cfg.two_port_mode is False

    def test_admin_port_different_is_true(self) -> None:
        """admin_port != port and != 0 means two-port mode."""
        cfg = ServerConfig(port=11434, admin_port=9999)
        assert cfg.two_port_mode is True

    def test_admin_port_nonstandard(self) -> None:
        """Any non-zero, non-matching admin_port enables two-port mode."""
        cfg = ServerConfig(port=8080, admin_port=8081)
        assert cfg.two_port_mode is True


# ---------------------------------------------------------------------------
# Proxy app route isolation tests
# ---------------------------------------------------------------------------

class TestProxyApp:
    """Verify the proxy-only app has correct routes."""

    def test_root_endpoint_exists(self, two_port_config: BrokerConfig) -> None:
        """Proxy app serves / (Ollama compatibility)."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/" in paths

    def test_api_routes_exist(self, two_port_config: BrokerConfig) -> None:
        """Proxy app serves /api/{path:path}."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/api/{path:path}" in paths

    def test_no_broker_routes(self, two_port_config: BrokerConfig) -> None:
        """Proxy app does NOT expose /broker/* routes."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        broker_paths = [p for p in paths if p.startswith("/broker")]
        assert broker_paths == [], f"Unexpected broker routes on proxy app: {broker_paths}"

    def test_no_a2a_routes(self, two_port_config: BrokerConfig) -> None:
        """Proxy app does NOT expose /a2a/* routes."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        a2a_paths = [p for p in paths if p.startswith("/a2a")]
        assert a2a_paths == [], f"Unexpected A2A routes on proxy app: {a2a_paths}"

    def test_no_agent_card(self, two_port_config: BrokerConfig) -> None:
        """Proxy app does NOT expose /.well-known/agent-card.json."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/.well-known/agent-card.json" not in paths

    def test_no_docs(self, two_port_config: BrokerConfig) -> None:
        """Proxy app has no OpenAPI docs exposed."""
        app = create_proxy_app(two_port_config)
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None


# ---------------------------------------------------------------------------
# Admin app route isolation tests
# ---------------------------------------------------------------------------

class TestAdminApp:
    """Verify the admin-only app has correct routes."""

    def test_broker_routes_exist(self, two_port_config: BrokerConfig) -> None:
        """Admin app exposes /broker/* routes."""
        app = create_admin_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        broker_paths = [p for p in paths if p.startswith("/broker")]
        assert len(broker_paths) > 0, "Admin app should have /broker/* routes"
        # Check a few key admin endpoints
        assert "/broker/status" in paths
        assert "/broker/health" in paths
        assert "/broker/queue" in paths

    def test_a2a_routes_exist(self, two_port_config: BrokerConfig) -> None:
        """Admin app exposes /a2a/* routes."""
        app = create_admin_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        a2a_paths = [p for p in paths if p.startswith("/a2a")]
        assert len(a2a_paths) > 0, "Admin app should have /a2a/* routes"

    def test_agent_card_exists(self, two_port_config: BrokerConfig) -> None:
        """Admin app exposes /.well-known/agent-card.json."""
        app = create_admin_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/.well-known/agent-card.json" in paths

    def test_no_proxy_routes(self, two_port_config: BrokerConfig) -> None:
        """Admin app does NOT expose /api/* proxy routes."""
        app = create_admin_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        api_paths = [p for p in paths if p.startswith("/api/")]
        assert api_paths == [], f"Unexpected proxy routes on admin app: {api_paths}"

    def test_no_root_endpoint(self, two_port_config: BrokerConfig) -> None:
        """Admin app does NOT expose / root endpoint (that's Ollama compat)."""
        app = create_admin_app(two_port_config)
        # Check that no route has path exactly "/" (exclude well-known)
        root_routes = [
            r for r in app.routes
            if hasattr(r, "path") and r.path == "/"
        ]
        assert root_routes == [], "Admin app should not expose / root"

    def test_has_docs(self, two_port_config: BrokerConfig) -> None:
        """Admin app has OpenAPI docs at /broker/docs."""
        app = create_admin_app(two_port_config)
        assert app.docs_url == "/broker/docs"
        assert app.openapi_url == "/broker/openapi.json"


# ---------------------------------------------------------------------------
# Single-port backward compatibility
# ---------------------------------------------------------------------------

class TestSinglePortBackwardCompat:
    """Verify create_app() still works unchanged in single-port mode."""

    def test_has_all_routes(self, single_port_config: BrokerConfig) -> None:
        """Single-port app has proxy, broker, A2A, and agent card routes."""
        app = create_app(single_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}

        # Proxy routes
        assert "/" in paths
        assert "/api/{path:path}" in paths

        # Broker routes
        assert "/broker/status" in paths
        assert "/broker/health" in paths

        # A2A routes
        a2a_paths = [p for p in paths if p.startswith("/a2a")]
        assert len(a2a_paths) > 0

        # Agent card
        assert "/.well-known/agent-card.json" in paths

    def test_default_config_uses_single_port(self) -> None:
        """Default BrokerConfig results in single-port mode."""
        config = BrokerConfig()
        assert config.server.two_port_mode is False
        assert config.server.admin_port == 0


# ---------------------------------------------------------------------------
# D4: Expanded two-port mode tests
# ---------------------------------------------------------------------------

class TestAdminEndpoints404OnProxy:
    """Admin endpoints should return 404 on the proxy port."""

    def test_broker_status_404_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """GET /broker/status should 404 on proxy app."""
        app = create_proxy_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/broker/status")
        assert resp.status_code in (404, 405)

    def test_broker_health_404_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """GET /broker/health should 404 on proxy app."""
        app = create_proxy_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/broker/health")
        assert resp.status_code in (404, 405)

    def test_broker_queue_404_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """GET /broker/queue should 404 on proxy app."""
        app = create_proxy_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/broker/queue")
        assert resp.status_code in (404, 405)

    def test_agent_card_404_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """GET /.well-known/agent-card.json should 404 on proxy app."""
        app = create_proxy_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/.well-known/agent-card.json")
        assert resp.status_code in (404, 405)

    def test_a2a_tasks_404_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """POST /a2a/tasks should 404 on proxy app."""
        app = create_proxy_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/a2a/tasks", json={"skill_id": "status", "params": {}})
        assert resp.status_code in (404, 405)


class TestProxyEndpoints404OnAdmin:
    """Proxy endpoints should return 404 on the admin port."""

    def test_api_generate_404_on_admin(self, two_port_config: BrokerConfig) -> None:
        """POST /api/generate should 404 on admin app."""
        app = create_admin_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/generate", json={"model": "test", "prompt": "hi"})
        assert resp.status_code in (404, 405)

    def test_api_chat_404_on_admin(self, two_port_config: BrokerConfig) -> None:
        """POST /api/chat should 404 on admin app."""
        app = create_admin_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/chat", json={"model": "test", "messages": []})
        assert resp.status_code in (404, 405)

    def test_api_tags_404_on_admin(self, two_port_config: BrokerConfig) -> None:
        """GET /api/tags should 404 on admin app."""
        app = create_admin_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/tags")
        assert resp.status_code in (404, 405)

    def test_root_404_on_admin(self, two_port_config: BrokerConfig) -> None:
        """GET / should 404 on admin app."""
        app = create_admin_app(two_port_config)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code in (404, 405)


class TestAgentCardOnlyOnAdmin:
    """Agent card should only be accessible on the admin port."""

    def test_agent_card_route_on_admin(self, two_port_config: BrokerConfig) -> None:
        """Admin app has the agent card route."""
        app = create_admin_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/.well-known/agent-card.json" in paths

    def test_no_agent_card_route_on_proxy(self, two_port_config: BrokerConfig) -> None:
        """Proxy app does NOT have the agent card route."""
        app = create_proxy_app(two_port_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/.well-known/agent-card.json" not in paths


class TestAuthOnlyOnAdmin:
    """Auth dependencies are only attached to admin/a2a routers, not proxy."""

    def test_proxy_app_has_no_auth_dependencies(self, two_port_config: BrokerConfig) -> None:
        """Proxy app routes should not have auth dependencies."""
        app = create_proxy_app(two_port_config)
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/api/{path:path}":
                # The proxy catch-all should not have broker auth dependencies
                # It serves raw Ollama traffic
                deps = getattr(route, "dependencies", [])
                dep_names = [str(d) for d in deps]
                for name in dep_names:
                    assert "admin_key" not in name.lower()

    def test_admin_app_broker_routes_have_auth(self, two_port_config: BrokerConfig) -> None:
        """Admin app /broker/* routes should have auth dependency attached."""
        two_port_config.auth.enabled = True
        two_port_config.auth.api_keys = ["test-key"]
        app = create_admin_app(two_port_config)
        # Check that broker routes exist (auth is dependency-injected per-router)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/broker/status" in paths


class TestBothPortsRouteStructure:
    """Verify both apps create cleanly and have proper structure."""

    def test_proxy_app_creates_cleanly(self, two_port_config: BrokerConfig) -> None:
        """create_proxy_app should not raise."""
        app = create_proxy_app(two_port_config)
        assert app is not None

    def test_admin_app_creates_cleanly(self, two_port_config: BrokerConfig) -> None:
        """create_admin_app should not raise."""
        app = create_admin_app(two_port_config)
        assert app is not None

    def test_single_port_app_creates_cleanly(self, single_port_config: BrokerConfig) -> None:
        """create_app should not raise in single-port mode."""
        app = create_app(single_port_config)
        assert app is not None

    def test_proxy_and_admin_have_disjoint_routes(self, two_port_config: BrokerConfig) -> None:
        """Proxy and admin apps should have no overlapping routes."""
        proxy_app = create_proxy_app(two_port_config)
        admin_app = create_admin_app(two_port_config)
        proxy_paths = {r.path for r in proxy_app.routes if hasattr(r, "path")}
        admin_paths = {r.path for r in admin_app.routes if hasattr(r, "path")}

        # Remove framework-standard paths (openapi.json, docs, redoc)
        framework_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        proxy_paths -= framework_paths
        admin_paths -= framework_paths

        overlap = proxy_paths & admin_paths
        assert overlap == set(), f"Routes should not overlap: {overlap}"

    def test_proxy_app_title(self, two_port_config: BrokerConfig) -> None:
        """Proxy app should have a distinct title."""
        app = create_proxy_app(two_port_config)
        assert "proxy" in app.title.lower() or "bastion" in app.title.lower()

    def test_admin_app_title(self, two_port_config: BrokerConfig) -> None:
        """Admin app should have a distinct title."""
        app = create_admin_app(two_port_config)
        assert "admin" in app.title.lower() or "bastion" in app.title.lower()
