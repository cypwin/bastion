"""HTTP-contract tests for ``GET /broker/catalog``.

Verifies that the catalog endpoint emits a CatalogEntry per registered
model from broker.yaml and correctly computes ``is_evictable``,
``currently_loaded``, ``actual_vram_gb``, and aggregate counts.

Uses ``app_with_stub_scheduler`` so VRAMTracker is a stub whose
``get_loaded_models()`` return value we can manipulate per test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import bastion.server as server_mod
from bastion.models import LoadedModel

# ─────────────────────────────────────────────────────────────────────────────
# Empty residency (no models loaded)
# ─────────────────────────────────────────────────────────────────────────────


class TestCatalogEndpointEmptyResidency:
    """No models loaded → registry surfaces but loaded/evictable are zero."""

    def test_returns_200(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/catalog")
        assert resp.status_code == 200

    def test_emits_one_entry_per_registered_model(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        body = client.get("/broker/catalog").json()
        names = {entry["name"] for entry in body["models"]}
        # test_config registers exactly these 4 models.
        assert names == {
            "qwen3:14b",
            "mistral-nemo:12b",
            "llama3.1:8b",
            "nomic-embed-text",
        }
        assert body["total"] == 4

    def test_loaded_and_evictable_counts_are_zero(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        body = client.get("/broker/catalog").json()
        assert body["loaded_count"] == 0
        assert body["evictable_count"] == 0
        for entry in body["models"]:
            assert entry["currently_loaded"] is False
            assert entry["is_evictable"] is False
            assert entry["actual_vram_gb"] is None

    def test_snapshot_age_is_non_negative(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        body = client.get("/broker/catalog").json()
        assert body["snapshot_age_s"] >= 0.0

    def test_response_has_required_top_level_keys(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        body = client.get("/broker/catalog").json()
        for key in (
            "models",
            "total",
            "loaded_count",
            "evictable_count",
            "registry_source",
            "snapshot_age_s",
        ):
            assert key in body, f"missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# With residency — currently_loaded + actual_vram_gb + is_evictable
# ─────────────────────────────────────────────────────────────────────────────


class TestCatalogEndpointWithResidency:
    """Stub VRAMTracker to report two loaded models."""

    def _stub_loaded(self, app_with_stub_scheduler, loaded: list[LoadedModel]) -> None:
        app_with_stub_scheduler.app.state.stubs.vram_tracker.get_loaded_models = (
            AsyncMock(return_value=loaded)
        )

    def test_currently_loaded_set_for_residents(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        self._stub_loaded(
            client,
            [
                LoadedModel(name="qwen3:14b", size_bytes=10**10, vram_gb=9.5),
                LoadedModel(name="llama3.1:8b", size_bytes=5 * 10**9, vram_gb=4.4),
            ],
        )
        body = client.get("/broker/catalog").json()
        loaded_by_name = {e["name"]: e for e in body["models"]}
        assert loaded_by_name["qwen3:14b"]["currently_loaded"] is True
        assert loaded_by_name["qwen3:14b"]["actual_vram_gb"] == 9.5
        assert loaded_by_name["llama3.1:8b"]["currently_loaded"] is True
        assert loaded_by_name["mistral-nemo:12b"]["currently_loaded"] is False
        assert loaded_by_name["mistral-nemo:12b"]["actual_vram_gb"] is None
        assert body["loaded_count"] == 2

    def test_always_allowed_model_is_never_evictable(
        self, app_with_stub_scheduler
    ) -> None:
        # nomic-embed-text is always_allowed=True in test_config.
        client = app_with_stub_scheduler
        self._stub_loaded(
            client,
            [LoadedModel(name="nomic-embed-text", size_bytes=10**8, vram_gb=0.4)],
        )
        body = client.get("/broker/catalog").json()
        nomic = next(e for e in body["models"] if e["name"] == "nomic-embed-text")
        assert nomic["currently_loaded"] is True
        assert nomic["is_evictable"] is False, "always_allowed must override evictable"
        assert body["evictable_count"] == 0

    def test_scheduler_current_model_is_never_evictable(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        client.app.state.stubs.scheduler.current_model = "qwen3:14b"
        self._stub_loaded(
            client,
            [
                LoadedModel(name="qwen3:14b", size_bytes=10**10, vram_gb=9.5),
                LoadedModel(name="llama3.1:8b", size_bytes=5 * 10**9, vram_gb=4.4),
            ],
        )
        body = client.get("/broker/catalog").json()
        by_name = {e["name"]: e for e in body["models"]}
        # qwen3 is current_model — not evictable. llama3 IS evictable.
        assert by_name["qwen3:14b"]["is_evictable"] is False
        assert by_name["llama3.1:8b"]["is_evictable"] is True
        assert body["evictable_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# /api/ps unreachable — collapse to "nothing loaded", do not raise
# ─────────────────────────────────────────────────────────────────────────────


class TestCatalogEndpointOllamaUnreachable:
    """VRAMTracker returns None when /api/ps fails. Catalog must stay up."""

    def test_unreachable_ollama_does_not_500(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.get_loaded_models = AsyncMock(
            return_value=None
        )
        resp = client.get("/broker/catalog")
        assert resp.status_code == 200

    def test_unreachable_ollama_reports_zero_loaded(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.get_loaded_models = AsyncMock(
            return_value=None
        )
        body = client.get("/broker/catalog").json()
        assert body["loaded_count"] == 0
        for entry in body["models"]:
            assert entry["currently_loaded"] is False


# ─────────────────────────────────────────────────────────────────────────────
# registry_source surfacing
# ─────────────────────────────────────────────────────────────────────────────


class TestCatalogEndpointRegistrySource:
    """registry_source is best-effort; '<unknown>' for non-load_config configs."""

    def test_registry_source_when_loaded_from_unset(
        self, app_with_stub_scheduler
    ) -> None:
        # test_config is built directly (bypasses load_config) → loaded_from is None
        # → registry_source must serialize as '<unknown>'.
        client = app_with_stub_scheduler
        body = client.get("/broker/catalog").json()
        assert body["registry_source"] == "<unknown>"


# ─────────────────────────────────────────────────────────────────────────────
# S130 review fixes: residency_state, tag-aware lookup, home redaction
# ─────────────────────────────────────────────────────────────────────────────


class TestCatalogResidencyState:
    """State-unknown must be distinguishable from genuinely-nothing-loaded."""

    def test_residency_state_ok_on_live_read(self, app_with_stub_scheduler) -> None:
        body = app_with_stub_scheduler.get("/broker/catalog").json()
        assert body["residency_state"] == "ok"

    def test_residency_state_unknown_when_tracker_returns_none(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.get_loaded_models = AsyncMock(
            return_value=None
        )
        body = client.get("/broker/catalog").json()
        assert body["residency_state"] == "unknown"
        assert body["loaded_count"] == 0  # placeholder, flagged by the state


class TestCatalogTagAwareResidency:
    def test_latest_tagged_resident_matches_untagged_registry_key(
        self, app_with_stub_scheduler
    ) -> None:
        """/api/ps reports 'nomic-embed-text:latest'; the registry key is
        untagged — the entry must still show as loaded."""
        client = app_with_stub_scheduler
        client.app.state.stubs.vram_tracker.get_loaded_models = AsyncMock(
            return_value=[
                LoadedModel(
                    name="nomic-embed-text:latest",
                    size_bytes=4 * 10**8,
                    vram_gb=0.4,
                ),
            ]
        )
        body = client.get("/broker/catalog").json()
        entry = {e["name"]: e for e in body["models"]}["nomic-embed-text"]
        assert entry["currently_loaded"] is True
        assert entry["actual_vram_gb"] == 0.4
        assert body["loaded_count"] == 1


class TestRedactHome:
    def test_home_prefix_replaced_with_tilde(self) -> None:
        import os

        home = os.path.expanduser("~")
        assert server_mod._redact_home(f"{home}/proj/broker.yaml") == (
            "~/proj/broker.yaml"
        )

    def test_non_home_path_unchanged(self) -> None:
        assert server_mod._redact_home("/etc/bastion/broker.yaml") == (
            "/etc/bastion/broker.yaml"
        )
