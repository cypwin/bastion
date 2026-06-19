"""HTTP-contract tests for BASTION admin / health / metrics routes.

Phase 1 of the server.py coverage push: pin the public contract for every
admin endpoint on ``create_app``.

Scope:
  - /broker/status, /broker/queue, /broker/health, /broker/vram
  - /broker/livez, /broker/readyz
  - /broker/preload, /broker/unload (all status branches)
  - /broker/drain, /broker/resume
  - /broker/metrics (Prometheus exposition or 501)
  - /broker/watchdog, /broker/recent, /broker/counters, /broker/thrashing
  - /broker/intent, /broker/intents, /broker/intent/{id}/complete + DELETE

Out of scope (covered elsewhere or in later phases):
  - /a2a/* routes  (Phase 3)
  - Streaming proxy paths  (test_proxy.py)
  - Detailed scheduler / circuit-breaker internals  (test_scheduler.py)

The fixture ``app_with_stub_scheduler`` (in conftest.py) builds a real app,
runs its lifespan, then swaps module-level collaborators on
``bastion.server`` with AsyncMock/MagicMock stubs.  Tests reach those stubs
via ``client.app.state.stubs``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import bastion.server as server_mod
from bastion.models import AuthConfig, BrokerConfig
from bastion.server import create_app
from tests.conftest import make_request

# ---------------------------------------------------------------------------
# /broker/status
# ---------------------------------------------------------------------------


class TestBrokerStatus:
    """Pin the response shape of /broker/status under stub collaborators."""

    def test_returns_200_with_stub_collaborators(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Happy path: route returns 200 even when scheduler is a stub."""
        client = app_with_stub_scheduler
        resp = client.get("/broker/status")
        assert resp.status_code == 200

    def test_response_has_required_top_level_keys(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Schema regression: dashboards rely on these keys existing."""
        client = app_with_stub_scheduler
        data = client.get("/broker/status").json()
        for key in (
            "uptime_seconds",
            "queue_depth",
            "loaded_models",
            "gpu",
            "state",
            "total_dispatched",
            "swap_rate_level",
            "stall_reason",
            "inflight_models",
            "circuit_breaker",
            "max_vram_gb",
            "gpu_is_safe",
            "recent_audit_events",
        ):
            assert key in data, f"missing key: {key}"

    def test_state_is_running_when_not_draining(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """state == 'running' when scheduler.is_draining is False."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.is_draining = False
        data = client.get("/broker/status").json()
        assert data["state"] == "running"

    def test_state_is_draining_when_scheduler_draining(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """state flips to 'draining' when the scheduler is draining."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.is_draining = True
        data = client.get("/broker/status").json()
        assert data["state"] == "draining"


# ---------------------------------------------------------------------------
# /broker/queue
# ---------------------------------------------------------------------------


class TestBrokerQueue:
    """Pin queue diagnostics shape (Phase 1 — empty queue case)."""

    def test_returns_200(self, app_with_stub_scheduler: TestClient) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/queue")
        assert resp.status_code == 200

    def test_returns_zero_total_when_queue_empty(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        data = client.get("/broker/queue").json()
        assert data["total"] == 0
        assert data["models"] == {}
        assert data["inflight"] == {}
        assert data["inflight_total"] == 0

    def test_includes_scheduler_state_and_cooldown(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Cooldown_remaining + scheduler_state are surfaced when scheduler set."""
        client = app_with_stub_scheduler
        data = client.get("/broker/queue").json()
        assert "scheduler_state" in data
        assert "cooldown_remaining" in data
        assert data["cooldown_remaining"] >= 0.0


# ---------------------------------------------------------------------------
# /broker/health, /broker/livez, /broker/readyz
# ---------------------------------------------------------------------------


class TestBrokerHealth:
    """Health probes — pinning HTTP status, content-type, and shape."""

    def test_broker_health_returns_200(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "healthy" in body
        assert "gpu" in body
        assert "scheduler_running" in body

    def test_livez_returns_plain_ok(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/livez")
        assert resp.status_code == 200
        assert resp.text == "ok"
        assert "text/plain" in resp.headers["content-type"]

    def test_readyz_returns_ok_when_scheduler_running(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """readyz == 200 'ok' when scheduler.is_running and proxy is up."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.is_running = True
        resp = client.get("/broker/readyz")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_readyz_returns_503_when_scheduler_not_running(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """readyz == 503 with 'not ready' body when scheduler is stopped."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.is_running = False
        resp = client.get("/broker/readyz")
        assert resp.status_code == 503
        assert "not ready" in resp.text

    def test_readyz_returns_503_when_circuit_open(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """readyz == 503 when proxy circuit breaker is open."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.is_running = True
        cb = MagicMock()
        cb.state = "open"
        client.app.state.stubs.proxy.circuit_breaker = cb
        resp = client.get("/broker/readyz")
        assert resp.status_code == 503
        assert "circuit breaker" in resp.text


# ---------------------------------------------------------------------------
# /broker/vram
# ---------------------------------------------------------------------------


class TestBrokerVram:
    """VRAM ledger endpoint — happy path (manager initialized by lifespan)."""

    def test_returns_200_with_status_dict(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/vram")
        # Lifespan creates the real VRAMManager so this is 200.
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# /broker/preload
# ---------------------------------------------------------------------------


class TestBrokerPreload:
    """Pre-load endpoint — missing field, conflict, and happy path."""

    def test_missing_model_returns_400(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.post("/broker/preload", json={})
        assert resp.status_code == 400
        assert "model" in resp.json()["error"].lower()

    def test_can_not_load_returns_409(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """When VRAMTracker.can_load_model returns False → 409 with reason."""
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.can_load_model = AsyncMock(
            return_value=(False, "insufficient VRAM")
        )
        resp = client.post("/broker/preload", json={"model": "huge:99b"})
        assert resp.status_code == 409
        assert "insufficient" in resp.json()["error"].lower()

    def test_happy_path_returns_loaded_status(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Happy path: preload returns {'status': 'loaded', 'model': ...}.

        Patches the local httpx.AsyncClient.post inside server.broker_preload
        so no real Ollama call is made.
        """
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.can_load_model = AsyncMock(
            return_value=(True, "ok")
        )
        with patch("bastion.server.httpx.AsyncClient") as mock_cls:
            instance = AsyncMock()
            instance.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_cls.return_value.__aenter__.return_value = instance
            resp = client.post(
                "/broker/preload", json={"model": "qwen3:14b"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "loaded"
        assert body["model"] == "qwen3:14b"


# ---------------------------------------------------------------------------
# /broker/unload — full branch coverage (the 2026-05-19 fix landed here)
# ---------------------------------------------------------------------------


class TestBrokerUnload:
    """Pin all four outcome branches: unloaded, reserved, inflight, failed."""

    def test_missing_model_returns_400(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.post("/broker/unload", json={})
        assert resp.status_code == 400

    def test_unloaded_returns_200(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """status='unloaded' from scheduler → 200 with status='unloaded'."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.unload_model_admin = AsyncMock(
            return_value=("unloaded", {"model": "qwen3:14b"})
        )
        resp = client.post("/broker/unload", json={"model": "qwen3:14b"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "unloaded"
        assert body["model"] == "qwen3:14b"

    def test_reserved_returns_409(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """status='reserved' (active A2A lease) → 409 'failed'."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.unload_model_admin = AsyncMock(
            return_value=(
                "reserved",
                {"model": "qwen3:14b", "reason": "active A2A reservation"},
            )
        )
        resp = client.post("/broker/unload", json={"model": "qwen3:14b"})
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == "failed"
        assert "reservation" in body["error"]

    def test_inflight_returns_409(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """status='inflight' (active request) → 409 'failed'."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.unload_model_admin = AsyncMock(
            return_value=(
                "inflight",
                {"model": "qwen3:14b", "reason": "in-flight inference request"},
            )
        )
        resp = client.post("/broker/unload", json={"model": "qwen3:14b"})
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == "failed"
        assert "in-flight" in body["error"]

    def test_failed_returns_500(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """status='failed' from tracker → 500 with reason."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.unload_model_admin = AsyncMock(
            return_value=(
                "failed",
                {"model": "qwen3:14b", "reason": "ollama did not confirm"},
            )
        )
        resp = client.post("/broker/unload", json={"model": "qwen3:14b"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "failed"

    def test_unknown_status_falls_back_to_500(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Unexpected scheduler status (not in the four expected) → 500."""
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.unload_model_admin = AsyncMock(
            return_value=("???", {"reason": "weird"})
        )
        resp = client.post("/broker/unload", json={"model": "qwen3:14b"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# /broker/drain  /broker/resume
# ---------------------------------------------------------------------------


class TestBrokerDrainResume:
    """Drain toggle endpoints."""

    def test_drain_returns_draining_status(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.post("/broker/drain")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "draining"
        client.app.state.stubs.scheduler.drain.assert_awaited()

    def test_resume_returns_running_status(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.post("/broker/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        client.app.state.stubs.scheduler.resume.assert_awaited()


# ---------------------------------------------------------------------------
# /broker/metrics
# ---------------------------------------------------------------------------


class TestBrokerMetrics:
    """Prometheus metrics — text exposition or 501 depending on install."""

    def test_returns_text_or_501(self, app_with_stub_scheduler: TestClient) -> None:
        """If prometheus_client is installed: 200 + text/plain. Else: 501.

        Both are valid contract outcomes; we just pin them.
        """
        client = app_with_stub_scheduler
        resp = client.get("/broker/metrics")
        assert resp.status_code in (200, 501)
        if resp.status_code == 501:
            body = resp.json()
            assert "error" in body
            assert "prometheus" in body["details"].lower()

    def test_501_when_prometheus_absent(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Pin the 501 branch by patching PROMETHEUS_AVAILABLE → False."""
        client = app_with_stub_scheduler
        with patch("bastion.server.PROMETHEUS_AVAILABLE", False):
            resp = client.get("/broker/metrics")
        assert resp.status_code == 501


# ---------------------------------------------------------------------------
# /broker/watchdog, /broker/recent, /broker/counters
# ---------------------------------------------------------------------------


class TestBrokerWatchdog:
    """Process monitor status endpoint."""

    def test_returns_200_with_stub_monitor(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/watchdog")
        assert resp.status_code == 200
        body = resp.json()
        assert "ollama_healthy" in body
        assert "gpu_responsive" in body


class TestBrokerRecent:
    """Recent-requests ring buffer endpoint."""

    def test_returns_200_empty_list_on_fresh_broker(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/recent")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestBrokerCounters:
    """Cumulative-counter endpoint."""

    def test_shape(self, app_with_stub_scheduler: TestClient) -> None:
        client = app_with_stub_scheduler
        data = client.get("/broker/counters").json()
        assert "reset_epoch" in data
        assert "total_requests_served" in data
        assert "total_dispatched" in data
        assert "model_swap_total" in data
        assert "thrashing_halt_total" in data

    def test_reset_epoch_stable_within_process(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        d1 = client.get("/broker/counters").json()
        d2 = client.get("/broker/counters").json()
        assert d1["reset_epoch"] == d2["reset_epoch"]

    def test_counter_values_propagate_from_stubs(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Counters mirror stub state (proxy._requests_served, scheduler.total_*)."""
        client = app_with_stub_scheduler
        client.app.state.stubs.proxy._requests_served = 42
        client.app.state.stubs.scheduler.total_dispatched = 17
        client.app.state.stubs.scheduler.total_swaps = 5
        data = client.get("/broker/counters").json()
        assert data["total_requests_served"] == 42
        assert data["total_dispatched"] == 17
        assert data["model_swap_total"] == 5


# ---------------------------------------------------------------------------
# /broker/thrashing
# ---------------------------------------------------------------------------


class TestBrokerThrashing:
    """Thrashing detector verdict endpoint."""

    def test_fresh_broker_returns_ok(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        data = client.get("/broker/thrashing").json()
        assert data["detector_state"] == "OK"
        assert data["agents"] == []

    def test_keys_present(self, app_with_stub_scheduler: TestClient) -> None:
        client = app_with_stub_scheduler
        data = client.get("/broker/thrashing").json()
        assert "detector_state" in data
        assert "agents" in data


# ---------------------------------------------------------------------------
# /broker/intent  /broker/intents  /broker/intent/{id}/complete  DELETE
# ---------------------------------------------------------------------------


class TestBrokerIntent:
    """Intent lifecycle endpoints."""

    def test_post_without_profile_or_sequence_returns_400(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Neither 'profile' nor 'model_sequence' → 400."""
        client = app_with_stub_scheduler
        resp = client.post("/broker/intent", json={"client_id": "test"})
        assert resp.status_code == 400
        assert "profile" in resp.json()["error"].lower()

    def test_post_unknown_profile_returns_404(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """An unknown profile name → 404 with available_profiles list."""
        client = app_with_stub_scheduler
        resp = client.post(
            "/broker/intent",
            json={"profile": "no-such-profile", "client_id": "test"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert "available_profiles" in body

    def test_post_ad_hoc_sequence_returns_registered(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Ad-hoc model_sequence → 200 with intent_id + resolved priority."""
        client = app_with_stub_scheduler
        resp = client.post(
            "/broker/intent",
            json={
                "intent_id": "test-intent-001",
                "model_sequence": ["qwen3:14b", "llama3.1:8b"],
                "client_id": "test",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent_id"] == "test-intent-001"
        assert body["model_sequence"] == ["qwen3:14b", "llama3.1:8b"]
        assert body["status"] == "registered"

    def test_list_intents_returns_total(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """GET /broker/intents returns intents dict + total count."""
        client = app_with_stub_scheduler
        # Register an intent first.
        client.post(
            "/broker/intent",
            json={
                "intent_id": "list-test",
                "model_sequence": ["qwen3:14b"],
                "client_id": "test",
            },
        )
        resp = client.get("/broker/intents")
        assert resp.status_code == 200
        body = resp.json()
        assert "intents" in body
        assert "total" in body
        assert body["total"] >= 1

    def test_complete_unknown_intent_returns_404(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.post("/broker/intent/does-not-exist/complete")
        assert resp.status_code == 404

    def test_complete_known_intent_returns_completed(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """POST /broker/intent/{id}/complete returns completed for known id."""
        client = app_with_stub_scheduler
        client.post(
            "/broker/intent",
            json={
                "intent_id": "complete-test",
                "model_sequence": ["qwen3:14b"],
                "client_id": "test",
            },
        )
        resp = client.post("/broker/intent/complete-test/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_delete_unknown_intent_returns_404(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.delete("/broker/intent/does-not-exist")
        assert resp.status_code == 404

    def test_delete_known_intent_returns_deleted(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        client.post(
            "/broker/intent",
            json={
                "intent_id": "delete-test",
                "model_sequence": ["qwen3:14b"],
                "client_id": "test",
            },
        )
        resp = client.delete("/broker/intent/delete-test")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"


# ---------------------------------------------------------------------------
# Auth enforcement on /broker/*
# ---------------------------------------------------------------------------


class TestBrokerAuth:
    """When auth is enabled, /broker/* requires a valid Authorization key."""

    @pytest.fixture
    def auth_client(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """Build a TestClient with auth enabled (no module-state stubbing).

        Auth is enforced at the dependency layer before any handler runs, so
        we don't need stub collaborators here — the request never reaches them.
        """
        monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
        cfg = BrokerConfig()
        cfg.auth = AuthConfig(enabled=True, api_keys=["admin-key-1"])
        app = create_app(cfg)
        return TestClient(app)

    def test_broker_status_without_key_returns_401(
        self, auth_client: TestClient
    ) -> None:
        with auth_client as c:
            resp = c.get("/broker/status")
        assert resp.status_code == 401

    def test_broker_status_with_bad_key_returns_401(
        self, auth_client: TestClient
    ) -> None:
        with auth_client as c:
            resp = c.get(
                "/broker/status",
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 401

    def test_broker_status_with_valid_key_returns_200(
        self, auth_client: TestClient
    ) -> None:
        with auth_client as c:
            resp = c.get(
                "/broker/status",
                headers={"Authorization": "Bearer admin-key-1"},
            )
        assert resp.status_code == 200

    def test_broker_health_open_route_pattern(
        self, auth_client: TestClient
    ) -> None:
        """Even /broker/health is behind admin auth (it's a /broker/* route)."""
        with auth_client as c:
            resp = c.get("/broker/health")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Root + agent card (open, no auth)
# ---------------------------------------------------------------------------


class TestRootAndAgentCard:
    """/ and /.well-known/agent-card.json are public."""

    def test_root_returns_ollama_running(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/")
        assert resp.status_code == 200
        # JSON-encoded string per FastAPI default.
        assert "Ollama" in resp.text

    def test_root_head_returns_200(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        client = app_with_stub_scheduler
        resp = client.head("/")
        # FastAPI treats HEAD on a GET route as 200 with no body.
        assert resp.status_code == 200

    def test_agent_card_returns_public_shape(
        self, app_with_stub_scheduler: TestClient
    ) -> None:
        """Public agent card: no auth, returns name + version + skills.

        With A2A disabled (test_config default), the static fallback path runs.
        """
        client = app_with_stub_scheduler
        resp = client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        body = resp.json()
        # Either path (live handler vs. static fallback) exposes these keys.
        assert "name" in body
        assert "version" in body
        assert "skills" in body


# ─────────────────────────────────────────────────────────────────────────────
# /broker/status — VRAM state-unknown coercion + indicator (S130)
# ─────────────────────────────────────────────────────────────────────────────


class TestBrokerStatusVramState:
    def test_vram_state_ok_on_live_read(self, app_with_stub_scheduler) -> None:
        data = app_with_stub_scheduler.get("/broker/status").json()
        assert data["vram_state"] == "ok"

    def test_none_sentinel_coerces_to_empty_list_with_unknown_state(
        self, app_with_stub_scheduler
    ) -> None:
        """During an Ollama outage the status contract (loaded_models: list)
        holds, and vram_state='unknown' marks the list as a placeholder."""
        from unittest.mock import AsyncMock

        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.get_loaded_models = AsyncMock(
            return_value=None
        )
        resp = client.get("/broker/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["loaded_models"] == []
        assert data["vram_state"] == "unknown"


# ---------------------------------------------------------------------------
# _release_swept_request (queue sweep marks grant as rejection)
# ---------------------------------------------------------------------------


class TestReleaseSweptRequest:
    """The sweep loop must mark grant events ``swept`` before setting them.

    An event set without the marker is indistinguishable from a real
    scheduler grant, so the waiting proxy handler would forward the swept
    request to Ollama (proxy side pinned by TestSweptRequests in
    test_proxy.py).
    """

    def test_marks_grant_event_swept_and_sets_both_events(self) -> None:
        req = make_request()
        grant_evt = asyncio.Event()
        completion_evt = asyncio.Event()
        server_mod._pending_grants[req.id] = grant_evt
        server_mod._pending_completions[req.id] = completion_evt
        try:
            server_mod._release_swept_request(req)

            assert grant_evt.is_set()
            assert getattr(grant_evt, "swept", False) is True
            assert completion_evt.is_set()
            assert req.id not in server_mod._pending_grants
            assert req.id not in server_mod._pending_completions
        finally:
            server_mod._pending_grants.pop(req.id, None)
            server_mod._pending_completions.pop(req.id, None)
