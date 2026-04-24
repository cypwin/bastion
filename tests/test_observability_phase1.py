"""Tests for Phase 1 Observability Enhancement — Agent 3 scope.

Covers:
  - ``GET /a2a/stats`` endpoint (TaskStore statistics)
  - ``BrokerStatus`` new observability fields (swap_rate_level, etc.)
  - Dashboard VRAM budget reading from API instead of hardcode

Test strategy:
  - ``/a2a/stats`` is tested both via direct TaskStore.stats() and via
    route registration verification on both ``create_app`` and
    ``create_admin_app``.
  - BrokerStatus observability fields tested by inspecting the dict
    returned by the ``create_app()`` status handler (which now returns
    extra fields beyond the Pydantic model).
  - Dashboard ``SafetyLimitsBar`` tested for dynamic budget updates.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.models import (
    A2AConfig,
    A2ATaskRecord,
    A2ATaskState,
    BrokerConfig,
    BrokerStatus,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    SchedulerConfig,
    ServerConfig,
)
from bastion.server import create_admin_app, create_app
from bastion.taskstore import TaskStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def obs_config() -> BrokerConfig:
    """BrokerConfig with A2A enabled for observability endpoint tests."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434),
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
            max_temperature_c=82,
            max_power_watts=450.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.1,
            max_queue_size=16,
        ),
        a2a=A2AConfig(
            enabled=True,
            tokens=["test-obs-token"],
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3, tags=["fast"]),
        },
    )


@pytest.fixture
def obs_config_two_port() -> BrokerConfig:
    """BrokerConfig with two-port mode and A2A enabled."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434, admin_port=9999),
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.1,
            max_queue_size=16,
        ),
        a2a=A2AConfig(
            enabled=True,
            tokens=["test-obs-token"],
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
        },
    )


# ---------------------------------------------------------------------------
# TaskStore.stats() direct tests
# ---------------------------------------------------------------------------


class TestTaskStoreStats:
    """Verify TaskStore.stats() returns expected keys and types."""

    def test_stats_empty_store(self) -> None:
        """Empty store returns zero counts and normal pressure."""
        store = TaskStore(maxsize=100)
        stats = store.stats()

        assert stats["active_count"] == 0
        assert stats["completed_count"] == 0
        assert stats["tombstone_count"] == 0
        assert stats["subscriber_count"] == 0
        assert stats["pressure_level"] == "normal"
        assert stats["maxsize"] == 100

    def test_stats_keys_present(self) -> None:
        """All expected keys are present in the stats dict."""
        store = TaskStore(maxsize=50)
        stats = store.stats()

        expected_keys = {
            "active_count",
            "completed_count",
            "tombstone_count",
            "subscriber_count",
            "pressure_level",
            "maxsize",
        }
        assert set(stats.keys()) == expected_keys

    def test_stats_reflects_active_tasks(self) -> None:
        """active_count increases when tasks are created."""
        store = TaskStore(maxsize=100)
        for i in range(5):
            record = A2ATaskRecord(
                task_id=f"task-{i}",
                context_id="ctx-test",
                state=A2ATaskState.SUBMITTED,
                skill_id="infer",
                input_params={"model": "test", "prompt": "hello"},
            )
            store.create(record)

        stats = store.stats()
        assert stats["active_count"] == 5

    def test_stats_reflects_completed_tasks(self) -> None:
        """completed_count increases when tasks reach terminal state."""
        store = TaskStore(maxsize=100)
        record = A2ATaskRecord(
            task_id="task-done",
            context_id="ctx-test",
            state=A2ATaskState.SUBMITTED,
            skill_id="infer",
            input_params={"model": "test", "prompt": "hello"},
        )
        store.create(record)
        store.update_state("task-done", A2ATaskState.WORKING)
        store.update_state("task-done", A2ATaskState.COMPLETED)

        stats = store.stats()
        assert stats["active_count"] == 0
        assert stats["completed_count"] == 1

    def test_stats_maxsize_matches_config(self) -> None:
        """maxsize in stats matches the configured value."""
        store = TaskStore(maxsize=42)
        assert store.stats()["maxsize"] == 42

    def test_stats_pressure_level_type(self) -> None:
        """pressure_level is a string value."""
        store = TaskStore(maxsize=100)
        stats = store.stats()
        assert isinstance(stats["pressure_level"], str)
        assert stats["pressure_level"] in ("normal", "pressure", "overloaded")


# ---------------------------------------------------------------------------
# /a2a/stats route registration tests
# ---------------------------------------------------------------------------


class TestA2AStatsRouteRegistration:
    """Verify /a2a/stats is registered in both app factories."""

    def test_stats_route_on_single_port_app(self, obs_config: BrokerConfig) -> None:
        """create_app() includes /a2a/stats route."""
        app = create_app(obs_config)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/a2a/stats" in paths

    def test_stats_route_on_admin_app(
        self, obs_config_two_port: BrokerConfig,
    ) -> None:
        """create_admin_app() includes /a2a/stats route."""
        app = create_admin_app(obs_config_two_port)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/a2a/stats" in paths

    def test_stats_route_is_get(self, obs_config: BrokerConfig) -> None:
        """/a2a/stats is a GET endpoint."""
        app = create_app(obs_config)
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/a2a/stats":
                methods = getattr(route, "methods", set())
                assert "GET" in methods
                break
        else:
            pytest.fail("/a2a/stats route not found")


# ---------------------------------------------------------------------------
# /a2a/stats endpoint behavior tests (via module-level mock)
# ---------------------------------------------------------------------------


class TestA2AStatsEndpoint:
    """Test /a2a/stats endpoint returns expected data when A2A is wired.

    These tests mock the module-level _a2a_handler to verify the
    endpoint returns the task store stats correctly.
    """

    _A2A_AUTH = {"Authorization": "Bearer test-obs-token"}

    def test_stats_returns_501_when_a2a_disabled(
        self, obs_config: BrokerConfig,
    ) -> None:
        """Returns 501 when A2A handler is not initialized."""
        from fastapi.testclient import TestClient

        app = create_app(obs_config)
        # Module-level _a2a_handler is None (no lifespan), so we get 501
        with patch("bastion.server._a2a_handler", None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/a2a/stats", headers=self._A2A_AUTH)
            assert resp.status_code == 501
            assert "not enabled" in resp.json()["error"]

    def test_stats_returns_store_data_when_a2a_enabled(
        self, obs_config: BrokerConfig,
    ) -> None:
        """Returns task store stats dict when A2A is enabled."""
        from fastapi.testclient import TestClient

        app = create_app(obs_config)

        # Create a mock A2A handler with a real TaskStore
        mock_handler = MagicMock()
        store = TaskStore(maxsize=200)
        # Add some tasks
        for i in range(3):
            record = A2ATaskRecord(
                task_id=f"obs-task-{i}",
                context_id="ctx-obs",
                state=A2ATaskState.SUBMITTED,
                skill_id="infer",
                input_params={"model": "test", "prompt": "hello"},
            )
            store.create(record)
        mock_handler._store = store

        with patch("bastion.server._a2a_handler", mock_handler):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/a2a/stats", headers=self._A2A_AUTH)
            assert resp.status_code == 200
            data = resp.json()
            assert data["active_count"] == 3
            assert data["completed_count"] == 0
            assert data["pressure_level"] == "normal"
            assert data["maxsize"] == 200

    def test_stats_on_admin_app_returns_501_without_handler(
        self, obs_config_two_port: BrokerConfig,
    ) -> None:
        """Admin app /a2a/stats returns 501 when handler is not initialized."""
        from fastapi.testclient import TestClient

        app = create_admin_app(obs_config_two_port)
        with patch("bastion.server._a2a_handler", None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/a2a/stats", headers=self._A2A_AUTH)
            assert resp.status_code == 501

    def test_stats_on_admin_app_returns_data(
        self, obs_config_two_port: BrokerConfig,
    ) -> None:
        """Admin app /a2a/stats returns task store stats when wired."""
        from fastapi.testclient import TestClient

        app = create_admin_app(obs_config_two_port)

        mock_handler = MagicMock()
        store = TaskStore(maxsize=100)
        mock_handler._store = store

        with patch("bastion.server._a2a_handler", mock_handler):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/a2a/stats", headers=self._A2A_AUTH)
            assert resp.status_code == 200
            data = resp.json()
            assert "active_count" in data
            assert "pressure_level" in data


# ---------------------------------------------------------------------------
# BrokerStatus observability fields tests
# ---------------------------------------------------------------------------


class TestBrokerStatusObservabilityFields:
    """Verify BrokerStatus base model fields and new fields from status handler.

    The status handler in create_app() now adds additional fields beyond
    the Pydantic BrokerStatus model.  These tests verify the standard
    model fields plus the extended observability dict.
    """

    def test_broker_status_base_fields(self) -> None:
        """BrokerStatus Pydantic model has all expected base fields."""
        status = BrokerStatus()
        data = status.model_dump()

        # Core fields
        assert "version" in data
        assert "uptime_seconds" in data
        assert "queue_depth" in data
        assert "queue_by_model" in data
        assert "loaded_models" in data
        assert "gpu" in data
        assert "current_model" in data
        assert "total_requests_served" in data
        assert "total_model_swaps" in data
        assert "state" in data
        assert "vram_ledger" in data

    def test_broker_status_types(self) -> None:
        """BrokerStatus fields have correct types."""
        status = BrokerStatus(
            uptime_seconds=100.0,
            queue_depth=5,
            total_requests_served=42,
            total_model_swaps=3,
            state="running",
        )
        data = status.model_dump()

        assert isinstance(data["uptime_seconds"], float)
        assert isinstance(data["queue_depth"], int)
        assert isinstance(data["total_requests_served"], int)
        assert isinstance(data["total_model_swaps"], int)
        assert isinstance(data["state"], str)
        assert isinstance(data["loaded_models"], list)
        assert isinstance(data["queue_by_model"], dict)

    def test_broker_status_with_gpu(self) -> None:
        """BrokerStatus includes GPU status."""
        gpu = GPUStatus(
            temperature_c=55,
            vram_used_mb=8000,
            vram_total_mb=32000,
        )
        status = BrokerStatus(gpu=gpu)
        data = status.model_dump()

        assert data["gpu"]["temperature_c"] == 55
        assert data["gpu"]["vram_used_mb"] == 8000

    def test_broker_status_with_loaded_models(self) -> None:
        """BrokerStatus includes loaded model info."""
        models = [
            LoadedModel(name="qwen3:14b", size_bytes=9965000000, vram_gb=9.3),
        ]
        status = BrokerStatus(loaded_models=models)
        data = status.model_dump()

        assert len(data["loaded_models"]) == 1
        assert data["loaded_models"][0]["name"] == "qwen3:14b"
        assert data["loaded_models"][0]["vram_gb"] == 9.3

    def test_broker_status_default_state(self) -> None:
        """BrokerStatus defaults to 'running' state."""
        status = BrokerStatus()
        assert status.state == "running"

    def test_max_vram_gb_from_gpu_config(self) -> None:
        """GPUConfig.max_vram_gb computes total - headroom correctly.

        This is the value that gets wired into the status response
        as ``max_vram_gb`` (T1-10).
        """
        gpu_cfg = GPUConfig(total_vram_gb=32.0, headroom_gb=6.0)
        assert gpu_cfg.max_vram_gb == 26.0

        gpu_cfg2 = GPUConfig(total_vram_gb=24.0, headroom_gb=8.0)
        assert gpu_cfg2.max_vram_gb == 16.0

    def test_status_handler_extended_fields(
        self, obs_config: BrokerConfig,
    ) -> None:
        """The create_app status handler returns observability fields.

        Verifies that the status endpoint returns the additional fields
        beyond the base BrokerStatus model (total_dispatched,
        swap_rate_level, stall_reason, inflight_models, circuit_breaker,
        max_vram_gb, gpu_is_safe).
        """
        from fastapi.testclient import TestClient

        app = create_app(obs_config)

        # Mock dependencies so the handler doesn't fail
        mock_gpu = GPUStatus(
            temperature_c=55,
            vram_used_mb=8000,
            vram_free_mb=24000,
            vram_total_mb=32000,
            power_draw_watts=180.0,
        )
        mock_models: list[LoadedModel] = [
            LoadedModel(name="qwen3:14b", size_bytes=9965000000, vram_gb=9.3),
        ]

        # Create mock scheduler
        mock_scheduler = MagicMock()
        mock_scheduler.current_model = "qwen3:14b"
        mock_scheduler.total_swaps = 5
        mock_scheduler.is_draining = False
        # Observability attributes added by Agent 1
        mock_scheduler.total_dispatched = 42
        mock_scheduler._swap_rate_level = "normal"
        mock_scheduler.stall_reason = ""
        mock_scheduler.stall_time = 0

        mock_proxy = MagicMock()
        mock_proxy._requests_served = 100
        mock_proxy.circuit_breaker = None

        mock_queue = MagicMock()
        mock_queue.total_size = 3
        mock_queue.queue_depth_by_model = MagicMock(return_value={"qwen3:14b": 3})

        mock_vram_tracker = MagicMock()
        mock_vram_tracker.get_loaded_models = AsyncMock(return_value=mock_models)

        mock_vram_manager = MagicMock()
        mock_vram_manager.status = MagicMock(return_value={"total_bytes": 32000000000})

        with (
            patch("bastion.server._scheduler", mock_scheduler),
            patch("bastion.server._proxy", mock_proxy),
            patch("bastion.server._queue", mock_queue),
            patch("bastion.server._vram_tracker", mock_vram_tracker),
            patch("bastion.server._vram_manager", mock_vram_manager),
            patch("bastion.server._start_time", time.time() - 3600),
            patch("bastion.server._inflight_models", {"qwen3:14b": 1}),
            patch(
                "bastion.server.query_gpu_status",
                AsyncMock(return_value=mock_gpu),
            ),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/broker/status")
            assert resp.status_code == 200
            data = resp.json()

            # Base BrokerStatus fields
            assert "uptime_seconds" in data
            assert "queue_depth" in data
            assert data["queue_depth"] == 3
            assert data["total_requests_served"] == 100
            assert data["total_model_swaps"] == 5
            assert data["state"] == "running"

            # Extended observability fields (T1-02 through T1-10)
            assert "total_dispatched" in data
            assert data["total_dispatched"] == 42

            assert "swap_rate_level" in data
            assert data["swap_rate_level"] == "normal"

            assert "stall_reason" in data
            assert "stall_duration_seconds" in data

            assert "inflight_models" in data
            assert data["inflight_models"] == {"qwen3:14b": 1}

            assert "circuit_breaker" in data

            assert "max_vram_gb" in data
            assert data["max_vram_gb"] == 26.0  # 32 - 6

            assert "gpu_is_safe" in data
            assert data["gpu_is_safe"] is True


# ---------------------------------------------------------------------------
# Dashboard SafetyLimitsBar VRAM budget tests
# ---------------------------------------------------------------------------


class TestSafetyLimitsBarBudget:
    """Verify SafetyLimitsBar threshold management via update_limits()."""

    def test_default_budget(self) -> None:
        """Default VRAM limit is the fallback value (26.0 GB)."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        assert bar._max_vram_gb == 26.0

    def test_update_limits_from_api(self) -> None:
        """update_limits sets the VRAM limit from the API value."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(24.0, None)
        assert bar._max_vram_gb == 24.0

    def test_update_limits_none_keeps_default(self) -> None:
        """update_limits(None, None) does not change limits."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(None, None)
        assert bar._max_vram_gb == 26.0

    def test_update_limits_zero_keeps_default(self) -> None:
        """update_limits(0, 0) does not change limits (zero guard)."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(0.0, 0)
        assert bar._max_vram_gb == 26.0

    def test_update_limits_updates_render(self) -> None:
        """Rendered bar reflects the updated VRAM limit value."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(20.0, None)
        text = bar.render()
        assert "20.0GB" in text.plain

    def test_render_with_default_budget(self) -> None:
        """Rendered bar uses default limit when no update is called."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        text = bar.render()
        assert "26.0GB" in text.plain

    def test_render_shows_temp_threshold(self) -> None:
        """Rendered bar includes temperature threshold."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(None, 80)
        text = bar.render()
        assert "80\u00b0C" in text.plain

    def test_successive_limit_updates(self) -> None:
        """Multiple update_limits calls use the latest value."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(30.0, None)
        assert bar._max_vram_gb == 30.0

        bar.update_limits(16.0, None)
        assert bar._max_vram_gb == 16.0

        text = bar.render()
        assert "16.0GB" in text.plain

    def test_negative_budget_keeps_previous(self) -> None:
        """Negative VRAM values are treated like zero/None."""
        from bastion.dashboard.statusbar import SafetyLimitsBar

        bar = SafetyLimitsBar()
        bar.update_limits(20.0, None)
        bar.update_limits(-5.0, None)
        # Negative is <= 0, so limit should stay at 20.0
        assert bar._max_vram_gb == 20.0
