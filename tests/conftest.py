"""Pytest fixtures for BASTION test suite.

Provides:
  - Mock Ollama backend (unittest.mock patches on httpx)
  - Test BrokerConfig with small queue sizes
  - GPU status fixtures (mock nvidia-smi)
  - QueuedRequest factory helper
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    PriorityTier,
    QueuedRequest,
    SchedulerConfig,
    ServerConfig,
)
from bastion.queue import AffinityQueue
from bastion.vram import VRAMTracker


# ---------------------------------------------------------------------------
# Configuration fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_config() -> BrokerConfig:
    """BrokerConfig with small queue sizes for fast tests."""
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
            cooldown_seconds=0.1,  # Fast for tests
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=16,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3, tags=["fast"]),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1, tags=["council"]),
            "llama3.1:8b": ModelInfo(vram_gb=4.4, tags=["council"]),
            "nomic-embed-text": ModelInfo(vram_gb=0.4, always_allowed=True, tags=["embedding"]),
        },
    )


@pytest.fixture
def small_config() -> BrokerConfig:
    """Minimal config with tiny queue for edge-case tests."""
    return BrokerConfig(
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            model_affinity_bonus=5.0,
            aging_rate=1.0,
            max_queue_size=4,
        ),
        models={
            "tiny:1b": ModelInfo(vram_gb=1.0),
        },
    )


# ---------------------------------------------------------------------------
# Queue fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def queue(test_config: BrokerConfig) -> AffinityQueue:
    """Empty AffinityQueue with test config."""
    return AffinityQueue(test_config.scheduler)


# ---------------------------------------------------------------------------
# VRAM tracker fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vram_tracker(test_config: BrokerConfig) -> VRAMTracker:
    """VRAMTracker with test config (no real Ollama connection)."""
    return VRAMTracker(test_config)


# ---------------------------------------------------------------------------
# GPU status fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gpu_status_safe() -> GPUStatus:
    """GPU status within safe limits."""
    return GPUStatus(
        temperature_c=55,
        vram_used_mb=8000,
        vram_free_mb=24000,
        vram_total_mb=32000,
        power_draw_watts=180.0,
    )


@pytest.fixture
def gpu_status_hot() -> GPUStatus:
    """GPU status with temperature exceeding safe limit."""
    return GPUStatus(
        temperature_c=90,
        vram_used_mb=28000,
        vram_free_mb=4000,
        vram_total_mb=32000,
        power_draw_watts=500.0,
    )


@pytest.fixture
def gpu_status_unavailable() -> GPUStatus:
    """GPU status when nvidia-smi is unavailable (all None)."""
    return GPUStatus()


@pytest.fixture
def mock_gpu_safe():
    """Patch query_gpu_status to return safe GPU status (async-compatible)."""
    safe = GPUStatus(
        temperature_c=55,
        vram_used_mb=8000,
        vram_free_mb=24000,
        vram_total_mb=32000,
        power_draw_watts=180.0,
    )
    with patch("bastion.health.query_gpu_status", AsyncMock(return_value=safe)) as m:
        yield m


@pytest.fixture
def mock_gpu_hot():
    """Patch query_gpu_status to return overheated GPU status (async-compatible)."""
    hot = GPUStatus(
        temperature_c=90,
        vram_used_mb=28000,
        vram_free_mb=4000,
        vram_total_mb=32000,
        power_draw_watts=500.0,
    )
    with patch("bastion.health.query_gpu_status", AsyncMock(return_value=hot)) as m:
        yield m


# ---------------------------------------------------------------------------
# Mock Ollama backend
# ---------------------------------------------------------------------------

class MockOllamaResponses:
    """Configurable mock responses for Ollama backend endpoints.

    Usage in tests::

        def test_something(mock_ollama):
            mock_ollama.ps_response = {"models": [{"name": "qwen3:14b", "size": 9965...}]}
            # VRAMTracker.get_loaded_models() will now return that model
    """

    def __init__(self) -> None:
        self.ps_response: Dict[str, Any] = {"models": []}
        self.tags_response: Dict[str, Any] = {"models": []}
        self.generate_response: Dict[str, Any] = {"response": "", "done": True}

    def make_response(self, status_code: int = 200, json_data: Any = None) -> httpx.Response:
        """Create a mock httpx.Response."""
        return httpx.Response(
            status_code=status_code,
            json=json_data,
            request=httpx.Request("GET", "http://mock"),
        )


@pytest.fixture
def mock_ollama():
    """Mock Ollama backend using unittest.mock.

    Patches httpx.AsyncClient methods to return configured responses.
    Configure responses in individual tests by setting attributes on
    the returned MockOllamaResponses object.
    """
    responses = MockOllamaResponses()

    async def mock_get(url, **kwargs):
        if "/api/ps" in str(url):
            return responses.make_response(json_data=responses.ps_response)
        if "/api/tags" in str(url):
            return responses.make_response(json_data=responses.tags_response)
        return responses.make_response(json_data={})

    async def mock_post(url, **kwargs):
        if "/api/generate" in str(url):
            return responses.make_response(json_data=responses.generate_response)
        return responses.make_response(json_data={})

    with patch.object(httpx.AsyncClient, "get", side_effect=mock_get), \
         patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        yield responses


# ---------------------------------------------------------------------------
# Request factory helpers
# ---------------------------------------------------------------------------

def make_request(
    model: str = "qwen3:14b",
    endpoint: str = "/api/generate",
    tier: PriorityTier = PriorityTier.AGENT,
    body: bytes = b'{"model": "qwen3:14b", "prompt": "hello"}',
    client_info: str = "test-client",
    base_priority: float | None = None,
    submitted_at: float | None = None,
) -> QueuedRequest:
    """Create a QueuedRequest for testing.

    Parameters
    ----------
    model : str
        Model name.
    endpoint : str
        API endpoint.
    tier : PriorityTier
        Priority tier.
    body : bytes
        Raw request body.
    client_info : str
        Client identifier.
    base_priority : float, optional
        Override base priority (defaults to tier's standard value).
    submitted_at : float, optional
        Override submission time (defaults to now).
    """
    _priority_defaults = {
        PriorityTier.INTERACTIVE: 100.0,
        PriorityTier.AGENT: 50.0,
        PriorityTier.PIPELINE: 25.0,
        PriorityTier.BACKGROUND: 10.0,
    }
    bp = base_priority if base_priority is not None else _priority_defaults[tier]

    return QueuedRequest(
        model=model,
        endpoint=endpoint,
        body=body,
        priority=bp,
        base_priority=bp,
        tier=tier,
        submitted_at=submitted_at or time.time(),
        client_info=client_info,
    )


@pytest.fixture
def request_factory():
    """Fixture that returns the make_request factory function."""
    return make_request


# ---------------------------------------------------------------------------
# TaskStore fixtures (D5: shared fixtures)
# ---------------------------------------------------------------------------

from bastion.models import A2ATaskRecord, A2ATaskState
from bastion.taskstore import TaskStore


def make_task_record(
    task_id: str = "test-001",
    state: A2ATaskState = A2ATaskState.SUBMITTED,
    skill_id: str = "infer",
) -> A2ATaskRecord:
    """Create a minimal A2ATaskRecord for testing."""
    return A2ATaskRecord(
        task_id=task_id,
        context_id="ctx-001",
        state=state,
        skill_id=skill_id,
        input_params={"model": "qwen3:14b", "prompt": "hello"},
    )


@pytest.fixture
def task_store() -> TaskStore:
    """Fresh TaskStore with default settings."""
    return TaskStore(maxsize=100)


@pytest.fixture
def task_record_factory():
    """Fixture that returns the make_task_record factory function."""
    return make_task_record


# ---------------------------------------------------------------------------
# Audit logger isolation (D5: test isolation)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_audit_logger():
    """Reset global audit logger between tests to prevent cross-contamination."""
    import bastion.audit
    original = bastion.audit._audit_logger
    yield
    bastion.audit._audit_logger = original


@pytest.fixture(autouse=True)
def _isolate_telemetry():
    """Reset telemetry module state between tests."""
    import bastion.telemetry as telem
    orig_tracer = telem._tracer
    orig_enabled = telem._enabled
    yield
    telem._tracer = orig_tracer
    telem._enabled = orig_enabled


# ---------------------------------------------------------------------------
# VRAMManager fixture (D5)
# ---------------------------------------------------------------------------

from bastion.vram import VRAMManager


@pytest.fixture
def vram_manager(vram_tracker: VRAMTracker) -> VRAMManager:
    """VRAMManager with 32GB total, 10% safety margin."""
    total_bytes = 32 * 1024 * 1024 * 1024
    return VRAMManager(vram_tracker, total_bytes, safety_margin_pct=10.0)
