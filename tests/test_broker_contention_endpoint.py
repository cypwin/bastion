"""HTTP-contract tests for ``GET /broker/contention`` and
``GET /broker/gpu/extended`` (observability T6).

Verifies the dual-factory registration (spec 4.10) and the response shapes
(``ContentionSnapshot`` 4.4 / ``GPUExtendedStatus`` 4.3) of the two new
slow-tick endpoints:

  - each route returns 200 with the right-shaped JSON body;
  - ``None`` legs are acceptable on this host (graceful degradation — a
    partial snapshot is valid and still emitted);
  - each route is registered in **both** ``create_app`` and
    ``create_admin_app`` (or it 404s in the admin-only two-port deployment).

The ``app_with_stub_scheduler`` fixture (conftest.py) builds the real
``create_app`` and runs its lifespan, which starts the broker-side
``_machine_snapshot_loop`` and populates the module-level globals the admin
app shares.
"""

from __future__ import annotations

import bastion.server as server_mod
from bastion.models import (
    BrokerConfig,
    ContentionSnapshot,
    GPUExtendedStatus,
)
from bastion.server import create_admin_app, create_app


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


# ─────────────────────────────────────────────────────────────────────────────
# Dual-factory route registration (spec 4.10) — the load-bearing assertion
# ─────────────────────────────────────────────────────────────────────────────


class TestContentionRoutesPresentInBothFactories:
    """Both new routes MUST be registered in both apps (spec 4.10)."""

    def test_contention_route_in_create_app(self, test_config: BrokerConfig) -> None:
        app = create_app(test_config)
        assert "/broker/contention" in _route_paths(app)

    def test_contention_route_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_admin_app(test_config)
        assert "/broker/contention" in _route_paths(app)

    def test_gpu_extended_route_in_create_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_app(test_config)
        assert "/broker/gpu/extended" in _route_paths(app)

    def test_gpu_extended_route_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_admin_app(test_config)
        assert "/broker/gpu/extended" in _route_paths(app)


# ─────────────────────────────────────────────────────────────────────────────
# /broker/contention — 200 + ContentionSnapshot shape
# ─────────────────────────────────────────────────────────────────────────────


class TestContentionEndpointCreateApp:
    def test_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/contention")
        assert resp.status_code == 200

    def test_payload_validates_as_contention_snapshot(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/contention").json()
        snap = ContentionSnapshot.model_validate(body)
        # block_devices is always a list (possibly empty on this host).
        assert isinstance(snap.block_devices, list)
        # sampled_at is stamped server-side.
        assert isinstance(snap.sampled_at, float)

    def test_none_psi_legs_acceptable(self, app_with_stub_scheduler) -> None:
        # PSI/RAPL/OOM legs may legitimately be None on this host (no PSI in a
        # container, no powercap). The keys still exist and the body is valid.
        body = app_with_stub_scheduler.get("/broker/contention").json()
        for key in (
            "psi_cpu_some_avg10",
            "swap_in_rate_mb_s",
            "cpu_package_watts",
            "oom_kill_total",
            "block_devices",
        ):
            assert key in body


# ─────────────────────────────────────────────────────────────────────────────
# /broker/gpu/extended — 200 + GPUExtendedStatus shape
# ─────────────────────────────────────────────────────────────────────────────


class TestGpuExtendedEndpointCreateApp:
    def test_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/gpu/extended")
        assert resp.status_code == 200

    def test_payload_validates_as_gpu_extended_status(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/gpu/extended").json()
        ext = GPUExtendedStatus.model_validate(body)
        # On a StubBackend / no-GPU host (the CI host), throttle_reasons and
        # recent_xids are the correct *complete* empty lists, not an error.
        assert isinstance(ext.throttle_reasons, list)
        assert isinstance(ext.recent_xids, list)

    def test_none_pcie_legs_acceptable(self, app_with_stub_scheduler) -> None:
        body = app_with_stub_scheduler.get("/broker/gpu/extended").json()
        for key in (
            "throttle_reasons",
            "pcie_tx_kb_s",
            "pcie_rx_kb_s",
            "recent_xids",
            "xid_count_since_start",
            "last_polled_at",
        ):
            assert key in body


# ─────────────────────────────────────────────────────────────────────────────
# create_admin_app — both routes serve the same handler over shared globals
# ─────────────────────────────────────────────────────────────────────────────


class TestEndpointsAdminApp:
    def test_contention_200_admin(self, app_with_stub_scheduler) -> None:
        from fastapi.testclient import TestClient

        admin_app = create_admin_app(server_mod._config)
        with TestClient(admin_app) as admin_client:
            resp = admin_client.get("/broker/contention")
            assert resp.status_code == 200
            ContentionSnapshot.model_validate(resp.json())

    def test_gpu_extended_200_admin(self, app_with_stub_scheduler) -> None:
        from fastapi.testclient import TestClient

        admin_app = create_admin_app(server_mod._config)
        with TestClient(admin_app) as admin_client:
            resp = admin_client.get("/broker/gpu/extended")
            assert resp.status_code == 200
            GPUExtendedStatus.model_validate(resp.json())
