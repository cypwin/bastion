"""HTTP-contract tests for ``GET /broker/processes`` (observability T1).

Verifies the dual-factory registration (spec 4.10) and the response shape
(``ProcessSnapshot`` 4.5) of the always-on process-attribution endpoint:

  - the route returns 200 with a ``ProcessSnapshot``-shaped JSON body;
  - empty lists (not a 404) before the first slow tick has populated GPU rows;
  - the route is registered in **both** ``create_app`` and ``create_admin_app``
    (or it 404s in the admin-only two-port deployment).

Process-attribution data is **TUI + JSON only** — never a Prometheus label
(spec 4.5 / 5.3); this endpoint is the JSON surface.
"""

from __future__ import annotations

import bastion.server as server_mod
from bastion.models import BrokerConfig, ProcessSnapshot
from bastion.server import create_admin_app, create_app


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


class TestProcessesRoutePresentInBothFactories:
    def test_processes_route_in_create_app(self, test_config: BrokerConfig) -> None:
        app = create_app(test_config)
        assert "/broker/processes" in _route_paths(app)

    def test_processes_route_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_admin_app(test_config)
        assert "/broker/processes" in _route_paths(app)


class TestProcessesEndpointCreateApp:
    def test_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/processes")
        assert resp.status_code == 200

    def test_payload_validates_as_process_snapshot(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/processes").json()
        snap = ProcessSnapshot.model_validate(body)
        # All collections are lists/dicts (possibly empty on this host).
        assert isinstance(snap.top_processes, list)
        assert isinstance(snap.gpu_processes, list)
        assert isinstance(snap.own_pids, dict)
        assert isinstance(snap.watchlist_hits, list)
        assert isinstance(snap.recent_churn_events, list)

    def test_empty_lists_not_404_before_first_tick(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/processes").json()
        for key in (
            "top_processes",
            "gpu_processes",
            "own_pids",
            "watchlist_hits",
            "recent_churn_events",
            "collected_at",
        ):
            assert key in body


class TestProcessesEndpointAdminApp:
    def test_processes_200_admin(self, app_with_stub_scheduler) -> None:
        from fastapi.testclient import TestClient

        admin_app = create_admin_app(server_mod._config)
        with TestClient(admin_app) as admin_client:
            resp = admin_client.get("/broker/processes")
            assert resp.status_code == 200
            ProcessSnapshot.model_validate(resp.json())
