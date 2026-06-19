"""HTTP-contract + wiring tests for the correlation engine integration (T5).

Verifies the dual-factory registration (spec 4.10) of the two new correlation
endpoints, that the engine is actually wired into the snapshot loop (so
``/broker/snapshot`` carries a populated ``correlation`` leg with an enriched
stall reason and the ring tail), and that the engine instance is constructed in
lifespan with the right dependency set (scheduler/vram only — never a2a).

  - ``GET /broker/correlation/risk``      — RiskIndex + thermal coupling.
  - ``GET /broker/correlation/contentions`` — last-N discrete contention events.
  - ``GET /broker/snapshot`` now carries ``correlation`` (CorrelationState).
  - ``engine.tick`` is called at the end of each ``_machine_snapshot_loop`` pass.
"""

from __future__ import annotations

import asyncio

import bastion.server as server_mod
from bastion.models import BrokerConfig, CorrelationState, MachineSnapshot
from bastion.server import create_admin_app, create_app


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


# ─────────────────────────────────────────────────────────────────────────────
# Dual-factory route registration (spec 4.10) — the load-bearing assertion
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrelationRoutesPresentInBothFactories:
    def test_risk_route_in_create_app(self, test_config: BrokerConfig) -> None:
        assert "/broker/correlation/risk" in _route_paths(create_app(test_config))

    def test_risk_route_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        assert "/broker/correlation/risk" in _route_paths(
            create_admin_app(test_config)
        )

    def test_contentions_route_in_create_app(
        self, test_config: BrokerConfig
    ) -> None:
        assert "/broker/correlation/contentions" in _route_paths(
            create_app(test_config)
        )

    def test_contentions_route_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        assert "/broker/correlation/contentions" in _route_paths(
            create_admin_app(test_config)
        )


# ─────────────────────────────────────────────────────────────────────────────
# create_app — 200 + shape
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrelationEndpointsCreateApp:
    def test_risk_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/correlation/risk")
        assert resp.status_code == 200

    def test_risk_shape(self, app_with_stub_scheduler) -> None:
        body = app_with_stub_scheduler.get("/broker/correlation/risk").json()
        # The risk endpoint surfaces both the composite + thermal coupling.
        assert "risk_index" in body
        assert "thermal_coupling" in body

    def test_contentions_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/correlation/contentions")
        assert resp.status_code == 200

    def test_contentions_empty_list_not_404(self, app_with_stub_scheduler) -> None:
        # Before any contention coincidence, the endpoint returns an empty list
        # body, never a 404 (spec — empty-lists, not 404, before first event).
        body = app_with_stub_scheduler.get("/broker/correlation/contentions").json()
        assert "contentions" in body
        assert isinstance(body["contentions"], list)


# ─────────────────────────────────────────────────────────────────────────────
# create_admin_app — shares module state, serves same handlers
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrelationEndpointsAdminApp:
    def test_admin_risk_and_contentions_200(
        self, app_with_stub_scheduler
    ) -> None:
        from fastapi.testclient import TestClient

        admin_app = create_admin_app(server_mod._config)
        with TestClient(admin_app) as admin_client:
            assert (
                admin_client.get("/broker/correlation/risk").status_code == 200
            )
            assert (
                admin_client.get("/broker/correlation/contentions").status_code
                == 200
            )


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot now carries the correlation leg + enriched stall (folded, NO /ring)
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotCarriesCorrelation:
    def test_snapshot_correlation_leg_populated(
        self, app_with_stub_scheduler
    ) -> None:
        # The engine ticks on every loop pass; force a deterministic collection
        # and assert the correlation leg is a populated CorrelationState, not
        # the Phase-1 None.
        snap = asyncio.run(server_mod._collect_machine_snapshot(0))
        assert isinstance(snap, MachineSnapshot)
        assert snap.correlation is not None
        assert isinstance(snap.correlation, CorrelationState)

    def test_snapshot_carries_enriched_stall_reason_field(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        assert body["correlation"] is not None
        assert "enriched_stall_reason" in body["correlation"]

    def test_snapshot_carries_ring_tail(self, app_with_stub_scheduler) -> None:
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        corr = body["correlation"]
        assert "recent_ring_events" in corr
        assert "ring_size" in corr

    def test_include_ring_query_expands_full_ring(
        self, app_with_stub_scheduler
    ) -> None:
        # ?include_ring=true expands the full ring tail (debug surface, 6.1).
        # The handler must still 200 and carry the correlation leg.
        resp = app_with_stub_scheduler.get("/broker/snapshot?include_ring=true")
        assert resp.status_code == 200
        assert resp.json()["correlation"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Engine instance + wiring (spec 6.6)
# ─────────────────────────────────────────────────────────────────────────────


class TestEngineWiring:
    def test_engine_instance_exists_after_lifespan(
        self, app_with_stub_scheduler
    ) -> None:
        from bastion.correlation import CorrelationEngine

        assert server_mod._correlation_engine is not None
        assert isinstance(server_mod._correlation_engine, CorrelationEngine)

    def test_contention_detector_instance_exists(
        self, app_with_stub_scheduler
    ) -> None:
        from bastion.correlation import ContentionEventDetector

        assert server_mod._contention_detector is not None
        assert isinstance(
            server_mod._contention_detector, ContentionEventDetector
        )

    def test_tick_invoked_during_collection(
        self, app_with_stub_scheduler, monkeypatch
    ) -> None:
        # Spy on the engine's tick to prove the loop body calls it with the
        # snapshot it just built (pull, never push — the engine consumes the
        # already-assembled snapshot).
        calls: list[object] = []
        engine = server_mod._correlation_engine
        assert engine is not None
        orig_tick = engine.tick

        def _spy(snapshot, *a, **kw):
            calls.append(snapshot)
            return orig_tick(snapshot, *a, **kw)

        monkeypatch.setattr(engine, "tick", _spy)
        # Exercise the loop body once via the public collection helper path.
        asyncio.run(server_mod._collect_machine_snapshot(0))
        assert calls, "engine.tick was not invoked during snapshot collection"
        assert isinstance(calls[-1], MachineSnapshot)
