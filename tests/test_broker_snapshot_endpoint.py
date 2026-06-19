"""HTTP-contract tests for ``GET /broker/snapshot`` (observability T7).

Verifies the dual-factory registration (spec 4.10) and the
``MachineSnapshot``-shaped payload (spec 4.1/4.9) of the new snapshot
endpoint:

  - the route returns 200 with a MachineSnapshot-shaped JSON body;
  - ``None`` sub-model fields are acceptable on this host (graceful
    degradation — a partial snapshot is valid and still emitted);
  - the route is registered in **both** ``create_app`` and
    ``create_admin_app`` (or it 404s in the admin-only two-port deployment).

The ``app_with_stub_scheduler`` fixture (conftest.py) builds the real
``create_app`` and runs its lifespan, which starts the broker-side
``_machine_snapshot_loop`` and populates the module-level globals the
admin app shares.
"""

from __future__ import annotations

import bastion.server as server_mod
from bastion.models import BrokerConfig, MachineSnapshot
from bastion.server import create_admin_app, create_app

# The seven top-level MachineSnapshot keys (spec 4.1).
_SNAPSHOT_KEYS = (
    "snapshot_ts",
    "broker",
    "gpu",
    "gpu_extended",
    "contention",
    "process",
    "inference",
    "correlation",
)


# ─────────────────────────────────────────────────────────────────────────────
# Dual-factory route registration (spec 4.10) — the load-bearing assertion
# ─────────────────────────────────────────────────────────────────────────────


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


class TestSnapshotRoutePresentInBothFactories:
    """/broker/snapshot MUST be registered in both apps (spec 4.10)."""

    def test_route_present_in_create_app(self, test_config: BrokerConfig) -> None:
        app = create_app(test_config)
        assert "/broker/snapshot" in _route_paths(app)

    def test_route_present_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_admin_app(test_config)
        assert "/broker/snapshot" in _route_paths(app)


# ─────────────────────────────────────────────────────────────────────────────
# create_app — 200 + MachineSnapshot shape
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotEndpointCreateApp:
    """Functional contract against the single-port app."""

    def test_returns_200(self, app_with_stub_scheduler) -> None:
        resp = app_with_stub_scheduler.get("/broker/snapshot")
        assert resp.status_code == 200

    def test_payload_has_all_machine_snapshot_keys(
        self, app_with_stub_scheduler
    ) -> None:
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        for key in _SNAPSHOT_KEYS:
            assert key in body, f"missing MachineSnapshot key: {key}"

    def test_payload_validates_as_machine_snapshot(
        self, app_with_stub_scheduler
    ) -> None:
        # The body must round-trip back into the Pydantic model — proves the
        # handler emits a real MachineSnapshot, not an ad-hoc dict.
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        snap = MachineSnapshot.model_validate(body)
        assert isinstance(snap.snapshot_ts, float)
        assert snap.snapshot_ts > 0.0

    def test_gpu_is_always_present(self, app_with_stub_scheduler) -> None:
        # gpu is a non-optional GPUStatus (default_factory) — present even on
        # a StubBackend / no-GPU host (all inner fields may be None).
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        assert body["gpu"] is not None
        assert isinstance(body["gpu"], dict)

    def test_none_subfields_are_acceptable(self, app_with_stub_scheduler) -> None:
        # The optional sub-models (process/inference/correlation) are not wired
        # in Phase 1; None there is valid and must not break the 200 contract.
        body = app_with_stub_scheduler.get("/broker/snapshot").json()
        # These keys exist; their value may legitimately be None on this host.
        for key in ("gpu_extended", "contention", "process", "inference", "correlation"):
            assert key in body


# ─────────────────────────────────────────────────────────────────────────────
# create_admin_app — 200 + shape (shares module state with create_app lifespan)
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotEndpointAdminApp:
    """The admin-port app serves the same handler over the shared globals."""

    def test_returns_200_and_snapshot_shape(
        self, app_with_stub_scheduler
    ) -> None:
        # app_with_stub_scheduler holds create_app's lifespan open, so the
        # module-level globals (and the snapshot deque) are live.  The admin
        # app's lifespan is a no-op and reuses that same module state.
        from fastapi.testclient import TestClient

        admin_app = create_admin_app(server_mod._config)
        with TestClient(admin_app) as admin_client:
            resp = admin_client.get("/broker/snapshot")
            assert resp.status_code == 200
            body = resp.json()
            for key in _SNAPSHOT_KEYS:
                assert key in body, f"missing MachineSnapshot key: {key}"
            MachineSnapshot.model_validate(body)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level collector (spec 4.9) — graceful, on-demand
# ─────────────────────────────────────────────────────────────────────────────


class TestCollectMachineSnapshotHelper:
    """_collect_machine_snapshot assembles a valid MachineSnapshot."""

    def test_collect_returns_machine_snapshot(
        self, app_with_stub_scheduler
    ) -> None:
        import asyncio

        snap = asyncio.run(server_mod._collect_machine_snapshot(0))
        assert isinstance(snap, MachineSnapshot)
        assert snap.snapshot_ts > 0.0
        # gpu always present (StubBackend yields an empty GPUStatus, not None).
        assert snap.gpu is not None
