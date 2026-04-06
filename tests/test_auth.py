"""Tests for router-level authentication dependencies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from bastion.auth import make_a2a_token_dependency, make_admin_key_dependency
from bastion.models import A2AConfig, AuthConfig


def _make_app(
    auth_config: AuthConfig,
    a2a_config: A2AConfig | None = None,
) -> TestClient:
    """Create a minimal FastAPI app with auth dependencies and return a TestClient."""
    app = FastAPI()

    verify_admin = make_admin_key_dependency(auth_config)
    broker_router = APIRouter(prefix="/broker", dependencies=[Depends(verify_admin)])

    @broker_router.get("/status")
    async def broker_status() -> dict:
        return {"status": "ok"}

    @broker_router.get("/queue")
    async def broker_queue() -> dict:
        return {"queue": []}

    # Open routes on main app (no auth)
    @app.get("/api/tags")
    async def api_tags() -> dict:
        return {"models": []}

    @app.get("/api/generate")
    async def api_generate() -> dict:
        return {"response": "hello"}

    @app.get("/")
    async def root() -> str:
        return "ok"

    app.include_router(broker_router)

    # A2A routes if config provided
    if a2a_config:
        verify_a2a = make_a2a_token_dependency(a2a_config)
        a2a_router = APIRouter(prefix="/a2a", dependencies=[Depends(verify_a2a)])

        @a2a_router.post("/tasks")
        async def create_task() -> dict:
            return {"task_id": "test"}

        @a2a_router.get("/tasks/{task_id}")
        async def get_task(task_id: str) -> dict:
            return {"task_id": task_id, "status": "completed"}

        app.include_router(a2a_router)

    return TestClient(app)


class TestAuthDisabled:
    """When auth is disabled, all requests should pass through."""

    def test_broker_route_passes(self) -> None:
        client = _make_app(AuthConfig(enabled=False))
        resp = client.get("/broker/status")
        assert resp.status_code == 200

    def test_api_route_passes(self) -> None:
        client = _make_app(AuthConfig(enabled=False))
        resp = client.get("/api/tags")
        assert resp.status_code == 200

    def test_root_passes(self) -> None:
        client = _make_app(AuthConfig(enabled=False))
        resp = client.get("/")
        assert resp.status_code == 200

    def test_enabled_but_no_keys_passes(self) -> None:
        """Auth enabled with empty api_keys list still passes all requests."""
        client = _make_app(AuthConfig(enabled=True, api_keys=[]))
        resp = client.get("/broker/status")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When auth is enabled with valid keys, protected routes require tokens."""

    _config = AuthConfig(enabled=True, api_keys=["secret-key-1", "secret-key-2"])

    def test_broker_no_token_returns_401(self) -> None:
        client = _make_app(self._config)
        resp = client.get("/broker/status")
        assert resp.status_code == 401
        assert "Missing Authorization" in resp.json()["detail"]

    def test_broker_invalid_token_returns_401(self) -> None:
        client = _make_app(self._config)
        resp = client.get(
            "/broker/status",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401
        assert "Invalid API key" in resp.json()["detail"]

    def test_broker_bad_header_format_returns_401(self) -> None:
        client = _make_app(self._config)
        resp = client.get(
            "/broker/status",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401
        assert "Invalid Authorization header format" in resp.json()["detail"]

    def test_broker_valid_token_passes(self) -> None:
        client = _make_app(self._config)
        resp = client.get(
            "/broker/status",
            headers={"Authorization": "Bearer secret-key-1"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_broker_second_valid_key_passes(self) -> None:
        client = _make_app(self._config)
        resp = client.get(
            "/broker/queue",
            headers={"Authorization": "Bearer secret-key-2"},
        )
        assert resp.status_code == 200

    def test_api_route_open_without_token(self) -> None:
        """Proxy routes (/api/*) remain open even when auth is enabled."""
        client = _make_app(self._config)
        resp = client.get("/api/tags")
        assert resp.status_code == 200

    def test_root_open_without_token(self) -> None:
        """Root (/) remains open even when auth is enabled."""
        client = _make_app(self._config)
        resp = client.get("/")
        assert resp.status_code == 200


class TestA2AAuthNoTokens:
    """When no A2A tokens are configured, all A2A requests pass through."""

    _auth = AuthConfig(enabled=False)
    _a2a = A2AConfig(enabled=True, tokens=[])

    def test_a2a_open_access(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.post("/a2a/tasks")
        assert resp.status_code == 200

    def test_a2a_get_open_access(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.get("/a2a/tasks/test-123")
        assert resp.status_code == 200


class TestA2AAuthWithTokens:
    """When A2A tokens are configured, A2A routes require valid bearer tokens."""

    _auth = AuthConfig(enabled=False)
    _a2a = A2AConfig(enabled=True, tokens=["a2a-secret-1", "a2a-secret-2"])

    def test_a2a_no_token_returns_401(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.post("/a2a/tasks")
        assert resp.status_code == 401
        assert "Missing or invalid Authorization" in resp.json()["detail"]

    def test_a2a_invalid_token_returns_401(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.post(
            "/a2a/tasks",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
        assert "Invalid A2A token" in resp.json()["detail"]

    def test_a2a_valid_token_passes(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.post(
            "/a2a/tasks",
            headers={"Authorization": "Bearer a2a-secret-1"},
        )
        assert resp.status_code == 200

    def test_a2a_second_valid_token_passes(self) -> None:
        client = _make_app(self._auth, self._a2a)
        resp = client.get(
            "/a2a/tasks/test-123",
            headers={"Authorization": "Bearer a2a-secret-2"},
        )
        assert resp.status_code == 200

    def test_broker_unaffected_by_a2a_tokens(self) -> None:
        """Broker routes should not be affected by A2A token config."""
        client = _make_app(self._auth, self._a2a)
        resp = client.get("/broker/status")
        assert resp.status_code == 200
