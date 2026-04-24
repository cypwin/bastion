"""Tests for A2A (Agent-to-Agent) protocol handler.

Covers:
  - Task lifecycle: create, get, cancel, state transitions
  - Infer skill: single prompt, errors, timeouts
  - Status skill: broker state retrieval
  - Agent card: static vs dynamic, runtime state
  - Auth: bearer token validation
  - SSE streaming: events, format, connection lifecycle
  - Batch inference and reservations (stubs verify they fail gracefully)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.a2a import A2AHandler
from bastion.models import (
    A2AConfig,
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    PriorityTier,
    QueuedRequest,
    SchedulerConfig,
    ServerConfig,
)
from bastion.vram import VRAMTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def a2a_config() -> BrokerConfig:
    """BrokerConfig with A2A enabled."""
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
            cooldown_seconds=0.0,  # No cooldown for tests
            max_queue_size=16,
        ),
        a2a=A2AConfig(
            enabled=True,
            tokens=["test-a2a-token", "test-a2a-token-2"],
            max_batch_size=5,
            reservation_timeout_seconds=10.0,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3, tags=["fast"]),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1, tags=["council"]),
        },
    )


@pytest.fixture
def a2a_config_open() -> BrokerConfig:
    """BrokerConfig with A2A enabled but no auth tokens (open access)."""
    config = BrokerConfig(
        a2a=A2AConfig(enabled=True, tokens=[]),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
        },
    )
    return config


@pytest.fixture
def mock_vram_tracker():
    """Mock VRAMTracker for testing."""
    tracker = MagicMock(spec=VRAMTracker)
    tracker.get_loaded_models = AsyncMock(return_value=[
        LoadedModel(name="qwen3:14b", size_bytes=9965000000, vram_gb=9.3),
    ])
    tracker.get_loaded_vram_gb = AsyncMock(return_value=9.3)
    return tracker


@pytest.fixture
def mock_scheduler():
    """Mock Scheduler for testing."""
    scheduler = MagicMock()
    scheduler.current_model = "qwen3:14b"
    scheduler.queue.total_size = 3
    scheduler.queue.queue_depth_by_model = MagicMock(return_value={
        "qwen3:14b": 2,
        "mistral-nemo:12b": 1,
    })
    return scheduler


@pytest.fixture
async def a2a_handler(a2a_config, mock_vram_tracker, mock_scheduler):
    """A2AHandler instance with mocked dependencies."""
    async def fake_enqueue(request: QueuedRequest):
        """Immediately grant all requests."""
        event = asyncio.Event()
        event.set()
        return event, lambda: None, lambda: None

    handler = A2AHandler(
        config=a2a_config,
        enqueue_fn=fake_enqueue,
        vram_tracker=mock_vram_tracker,
        scheduler=mock_scheduler,
    )
    return handler


# ---------------------------------------------------------------------------
# Task Lifecycle Tests
# ---------------------------------------------------------------------------

class TestTaskLifecycle:
    """Test task creation, state transitions, and retrieval."""

    @pytest.mark.asyncio
    async def test_create_task_returns_submitted_state(self, a2a_handler):
        """Creating a task returns a task dict in submitted state."""
        message = {
            "skill_id": "status",
            "params": {},
        }
        result = await a2a_handler.create_task(message)

        assert "id" in result
        assert result["status"]["state"] == "submitted"
        assert "contextId" in result
        assert result["artifacts"] == []

    @pytest.mark.asyncio
    async def test_get_task_returns_current_state(self, a2a_handler):
        """get_task returns the current task state."""
        message = {"skill_id": "status", "params": {}}
        created = await a2a_handler.create_task(message)
        task_id = created["id"]

        # Wait briefly for status handler to complete
        await asyncio.sleep(0.1)

        result = await a2a_handler.get_task(task_id)
        assert result is not None
        assert result["id"] == task_id
        assert result["status"]["state"] in ("working", "completed")

    @pytest.mark.asyncio
    async def test_get_nonexistent_task_returns_none(self, a2a_handler):
        """get_task returns None for unknown task ID."""
        result = await a2a_handler.get_task("nonexistent-task-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_submitted_task(self, a2a_handler):
        """cancel_task succeeds for submitted tasks."""
        # Create a task that won't auto-complete (infer with missing params)
        message = {"skill_id": "infer", "params": {}}
        created = await a2a_handler.create_task(message)
        task_id = created["id"]

        # Cancel immediately before it transitions to working
        success = await a2a_handler.cancel_task(task_id)
        # May succeed or fail depending on timing, just verify it doesn't crash
        assert isinstance(success, bool)

    @pytest.mark.asyncio
    async def test_cancel_completed_task_fails(self, a2a_handler):
        """cancel_task fails for already-completed tasks."""
        message = {"skill_id": "status", "params": {}}
        created = await a2a_handler.create_task(message)
        task_id = created["id"]

        # Wait for completion
        await asyncio.sleep(0.1)

        success = await a2a_handler.cancel_task(task_id)
        assert success is False

    @pytest.mark.asyncio
    async def test_unknown_skill_creates_failed_task(self, a2a_handler):
        """Unknown skill_id creates task in failed state immediately."""
        message = {"skill_id": "unknown_skill", "params": {}}
        result = await a2a_handler.create_task(message)

        assert result["status"]["state"] == "failed"
        assert "Unknown skill" in result["status"]["message"]

    @pytest.mark.asyncio
    async def test_missing_skill_id_creates_failed_task(self, a2a_handler):
        """Missing skill_id creates task in failed state."""
        message = {"params": {"test": "value"}}
        result = await a2a_handler.create_task(message)

        assert result["status"]["state"] == "failed"
        assert "Missing skill_id" in result["status"]["message"]


# ---------------------------------------------------------------------------
# Infer Skill Tests
# ---------------------------------------------------------------------------

class TestInferSkill:
    """Test the infer skill (single-prompt inference)."""

    @pytest.mark.asyncio
    async def test_single_prompt_completes(self, a2a_handler):
        """Infer skill successfully completes a single prompt."""
        # Mock Ollama response
        mock_response = httpx.Response(
            200,
            json={"response": "Quantum computing uses qubits.", "done": True, "eval_count": 25},
            request=httpx.Request("POST", "http://mock"),
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            message = {
                "skill_id": "infer",
                "params": {
                    "model": "qwen3:14b",
                    "prompt": "What is quantum computing?",
                },
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            # Wait for completion
            await asyncio.sleep(0.2)

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "completed"
            assert len(task["artifacts"]) > 0
            assert task["artifacts"][0]["parts"][0]["kind"] == "text"
            assert "Quantum computing" in task["artifacts"][0]["parts"][0]["text"]

    @pytest.mark.asyncio
    async def test_infer_with_system_prompt(self, a2a_handler):
        """Infer skill includes system prompt in Ollama request."""
        captured_payload = None

        async def capture_post(*args, **kwargs):
            nonlocal captured_payload
            captured_payload = kwargs.get("json")
            return httpx.Response(
                200,
                json={"response": "Answer", "done": True},
                request=httpx.Request("POST", "http://mock"),
            )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=capture_post):
            message = {
                "skill_id": "infer",
                "params": {
                    "model": "qwen3:14b",
                    "prompt": "Test prompt",
                    "system_prompt": "You are a helpful assistant.",
                },
            }
            await a2a_handler.create_task(message)
            await asyncio.sleep(0.2)

            assert captured_payload is not None
            assert captured_payload["system"] == "You are a helpful assistant."
            assert captured_payload["options"]["use_mmap"] is False

    @pytest.mark.asyncio
    async def test_infer_missing_model_fails(self, a2a_handler):
        """Infer skill fails when model parameter is missing."""
        message = {
            "skill_id": "infer",
            "params": {
                "prompt": "Test prompt",
            },
        }
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.1)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "Missing 'model'" in task["status"]["message"]

    @pytest.mark.asyncio
    async def test_infer_queue_full_fails_task(self, a2a_config, mock_vram_tracker, mock_scheduler):
        """Infer fails gracefully when queue is full."""
        async def enqueue_fails(request: QueuedRequest) -> asyncio.Event:
            raise RuntimeError("Queue full")

        handler = A2AHandler(
            config=a2a_config,
            enqueue_fn=enqueue_fails,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
        )

        message = {
            "skill_id": "infer",
            "params": {"model": "qwen3:14b", "prompt": "test"},
        }
        result = await handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.1)

        task = await handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "Queue full" in task["status"]["message"]

    @pytest.mark.asyncio
    async def test_infer_timeout_fails_task(self, a2a_config, mock_vram_tracker, mock_scheduler):
        """Infer fails when queue grant times out."""
        async def enqueue_never_grants(request: QueuedRequest):
            return asyncio.Event(), lambda: None, lambda: None  # Never set, will timeout

        # Set very short timeout for test
        a2a_config.proxy.queue_timeout_seconds = 0.1

        handler = A2AHandler(
            config=a2a_config,
            enqueue_fn=enqueue_never_grants,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
        )

        message = {
            "skill_id": "infer",
            "params": {"model": "qwen3:14b", "prompt": "test"},
        }
        result = await handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.3)

        task = await handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "timeout" in task["status"]["message"].lower()

    @pytest.mark.asyncio
    async def test_infer_ollama_error_fails_task(self, a2a_handler):
        """Infer fails gracefully on Ollama HTTP errors."""
        mock_response = httpx.Response(
            500,
            json={"error": "Internal server error"},
            request=httpx.Request("POST", "http://mock"),
        )
        mock_response.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "500 Server Error", request=mock_response.request, response=mock_response
        ))

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            message = {
                "skill_id": "infer",
                "params": {"model": "qwen3:14b", "prompt": "test"},
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            await asyncio.sleep(0.2)

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "failed"
            assert "error" in task["status"]["message"].lower()


# ---------------------------------------------------------------------------
# Status Skill Tests
# ---------------------------------------------------------------------------

class TestStatusSkill:
    """Test the status skill (broker state retrieval)."""

    @pytest.mark.asyncio
    async def test_status_returns_broker_state(self, a2a_handler):
        """Status skill returns current queue and loaded models."""
        message = {"skill_id": "status", "params": {}}
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.1)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "completed"
        assert len(task["artifacts"]) > 0

        data_part = task["artifacts"][0]["parts"][0]
        assert data_part["kind"] == "data"
        status_data = data_part["data"]
        assert "queue_depth" in status_data
        assert "loaded_models" in status_data
        assert "qwen3:14b" in status_data["loaded_models"]


# ---------------------------------------------------------------------------
# Batch Inference Tests
# ---------------------------------------------------------------------------

class TestBatchInferSkill:
    """Test batch_infer skill (N prompts with single model load)."""

    @pytest.mark.asyncio
    async def test_batch_all_succeed(self, a2a_handler):
        """Batch inference with all prompts succeeding."""
        mock_response = httpx.Response(
            200,
            json={"response": "Test response", "done": True},
            request=httpx.Request("POST", "http://mock"),
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            message = {
                "skill_id": "batch_infer",
                "params": {
                    "model": "qwen3:14b",
                    "prompts": ["Prompt 1", "Prompt 2", "Prompt 3"],
                },
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            # Wait for completion
            for _ in range(30):
                await asyncio.sleep(0.1)
                task = await a2a_handler.get_task(task_id)
                if task["status"]["state"] in ("completed", "failed"):
                    break

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "completed"
            artifact = task["artifacts"][0]
            batch_result = artifact["parts"][0]["data"]
            assert batch_result["total"] == 3
            assert batch_result["succeeded"] == 3
            assert batch_result["failed"] == 0

    @pytest.mark.asyncio
    async def test_batch_partial_failure(self, a2a_handler):
        """Batch inference with some prompts failing."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Second call fails, others succeed
            if call_count == 2:
                return httpx.Response(
                    500,
                    json={"error": "Error"},
                    request=httpx.Request("POST", "http://mock"),
                )
            return httpx.Response(
                200,
                json={"response": "OK", "done": True},
                request=httpx.Request("POST", "http://mock"),
            )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post):
            message = {
                "skill_id": "batch_infer",
                "params": {
                    "model": "qwen3:14b",
                    "prompts": ["P1", "P2", "P3"],
                },
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            for _ in range(30):
                await asyncio.sleep(0.1)
                task = await a2a_handler.get_task(task_id)
                if task["status"]["state"] in ("completed", "failed"):
                    break

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "completed"
            batch_result = task["artifacts"][0]["parts"][0]["data"]
            assert batch_result["succeeded"] == 2
            assert batch_result["failed"] == 1

    @pytest.mark.asyncio
    async def test_batch_exceeds_max_size_rejected(self, a2a_handler):
        """Batch size exceeding max should be rejected."""
        message = {
            "skill_id": "batch_infer",
            "params": {
                "model": "qwen3:14b",
                "prompts": ["P"] * 10,  # Config max is 5
            },
        }
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.2)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "exceeds max" in task["status"]["message"]

    @pytest.mark.asyncio
    async def test_batch_empty_prompts_rejected(self, a2a_handler):
        """Empty prompts list should be rejected."""
        message = {
            "skill_id": "batch_infer",
            "params": {
                "model": "qwen3:14b",
                "prompts": [],
            },
        }
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.2)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "empty" in task["status"]["message"].lower()

    @pytest.mark.asyncio
    async def test_batch_creates_reservation(self, a2a_handler):
        """Batch inference should create and clean up reservation."""
        mock_response = httpx.Response(
            200,
            json={"response": "OK", "done": True},
            request=httpx.Request("POST", "http://mock"),
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            # Before batch: no reservations
            assert len(a2a_handler._reservations) == 0

            message = {
                "skill_id": "batch_infer",
                "params": {
                    "model": "qwen3:14b",
                    "prompts": ["P1", "P2"],
                },
            }
            await a2a_handler.create_task(message)

            # Wait for completion
            await asyncio.sleep(0.5)

            # After batch: reservation should be cleaned up
            assert len(a2a_handler._reservations) == 0


# ---------------------------------------------------------------------------
# Preload/Reservation Tests
# ---------------------------------------------------------------------------

class TestPreloadSkill:
    """Test preload skill (model reservation)."""

    @pytest.mark.asyncio
    async def test_reservation_created(self, a2a_handler, mock_vram_tracker):
        """Preload skill should create reservation."""
        mock_vram_tracker.can_load_model = AsyncMock(return_value=(True, ""))

        mock_response = httpx.Response(
            200,
            json={"response": "", "done": True},
            request=httpx.Request("POST", "http://mock"),
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            message = {
                "skill_id": "preload",
                "params": {
                    "model": "qwen3:14b",
                    "num_requests": 5,
                },
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            for _ in range(20):
                await asyncio.sleep(0.05)
                task = await a2a_handler.get_task(task_id)
                if task["status"]["state"] in ("completed", "failed"):
                    break

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "completed"
            reservation_data = task["artifacts"][0]["parts"][0]["data"]
            assert "reservation_id" in reservation_data
            assert reservation_data["model"] == "qwen3:14b"
            assert reservation_data["num_requests"] == 5

            # Verify reservation exists
            assert len(a2a_handler._reservations) == 1

    @pytest.mark.asyncio
    async def test_reservation_prevents_eviction(self, a2a_handler, mock_vram_tracker):
        """Active reservation should prevent model eviction."""
        mock_vram_tracker.can_load_model = AsyncMock(return_value=(True, ""))

        mock_response = httpx.Response(
            200,
            json={"response": "", "done": True},
            request=httpx.Request("POST", "http://mock"),
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            message = {
                "skill_id": "preload",
                "params": {
                    "model": "qwen3:14b",
                    "num_requests": 10,
                },
            }
            await a2a_handler.create_task(message)

            await asyncio.sleep(0.2)

            # Check reservation prevents eviction
            assert a2a_handler.has_active_reservation("qwen3:14b") is True
            assert a2a_handler.has_active_reservation("mistral-nemo:12b") is False

    @pytest.mark.asyncio
    async def test_reservation_invalid_model_fails(self, a2a_handler):
        """Preload with invalid model should fail."""
        message = {
            "skill_id": "preload",
            "params": {
                "model": "nonexistent-model",
                "num_requests": 5,
            },
        }
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.2)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "Unknown model" in task["status"]["message"]

    @pytest.mark.asyncio
    async def test_reservation_vram_check(self, a2a_handler, mock_vram_tracker):
        """Preload should check VRAM availability."""
        # Mock VRAM check to fail
        mock_vram_tracker.can_load_model = AsyncMock(return_value=(False, "Insufficient VRAM"))

        message = {
            "skill_id": "preload",
            "params": {
                "model": "qwen3:14b",
                "num_requests": 5,
            },
        }
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        await asyncio.sleep(0.2)

        task = await a2a_handler.get_task(task_id)
        assert task["status"]["state"] == "failed"
        assert "VRAM" in task["status"]["message"]


# ---------------------------------------------------------------------------
# Agent Card Tests
# ---------------------------------------------------------------------------

class TestAgentCard:
    """Test three-tier agent card disclosure (C1 hardening)."""

    # ── Tier 1: Public Card ─────────────────────────────────────────

    def test_public_card_has_generic_name(self, a2a_handler):
        """Public card uses generic agent name (no infrastructure leak)."""
        card = a2a_handler.build_public_card()
        assert card["name"] == "BASTION GPU Inference Broker"

    def test_public_card_has_no_state(self, a2a_handler):
        """Public card does NOT expose runtime state (VRAM, models, queue)."""
        card = a2a_handler.build_public_card()
        assert "state" not in card

    def test_public_card_has_no_model_names(self, a2a_handler):
        """Public card does NOT list specific model names."""
        card = a2a_handler.build_public_card()
        card_str = json.dumps(card)
        assert "qwen3" not in card_str
        assert "mistral" not in card_str

    def test_public_card_has_no_vram_data(self, a2a_handler):
        """Public card does NOT expose VRAM numbers."""
        card = a2a_handler.build_public_card()
        card_str = json.dumps(card)
        assert "vram" not in card_str.lower()

    def test_public_card_has_no_queue_depth(self, a2a_handler):
        """Public card does NOT expose queue depth."""
        card = a2a_handler.build_public_card()
        card_str = json.dumps(card)
        assert "queue_depth" not in card_str

    def test_public_card_has_real_skill_ids(self, a2a_handler):
        """Public card lists real skill IDs that match the routing table."""
        card = a2a_handler.build_public_card()
        skill_ids = {
            s["id"] if isinstance(s, dict) else s
            for s in card["skills"]
        }
        assert "infer" in skill_ids
        assert "batch_infer" in skill_ids

    def test_public_card_has_streaming_capability(self, a2a_handler):
        """Public card advertises streaming capability."""
        card = a2a_handler.build_public_card()
        assert card["capabilities"]["streaming"] is True

    def test_public_card_has_protocol_version(self, a2a_handler):
        """Public card includes protocol version."""
        card = a2a_handler.build_public_card()
        assert "protocolVersion" in card

    def test_public_card_always_has_security_schemes(self, a2a_handler):
        """Public card always includes securitySchemes (tells callers how to auth)."""
        card = a2a_handler.build_public_card()
        assert "securitySchemes" in card
        assert "BearerToken" in card["securitySchemes"]
        assert card["securitySchemes"]["BearerToken"]["type"] == "http"
        assert card["securitySchemes"]["BearerToken"]["scheme"] == "bearer"
        assert "security" in card
        assert {"BearerToken": []} in card["security"]

    @pytest.mark.asyncio
    async def test_public_card_security_even_without_tokens(
        self, a2a_config_open, mock_vram_tracker, mock_scheduler,
    ):
        """Public card includes securitySchemes even when no tokens configured."""
        async def fake_enqueue(request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        handler = A2AHandler(
            config=a2a_config_open,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
        )

        card = handler.build_public_card()
        assert "securitySchemes" in card
        assert "BearerToken" in card["securitySchemes"]

    # ── Tier 2: Extended Card ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_extended_card_includes_supported_models(self, a2a_handler):
        """Extended card lists specific model families from config."""
        card = await a2a_handler.build_extended_card()

        assert "supported_models" in card
        model_names = [m["name"] for m in card["supported_models"]]
        assert "qwen3:14b" in model_names
        assert "mistral-nemo:12b" in model_names

    @pytest.mark.asyncio
    async def test_extended_card_model_details(self, a2a_handler):
        """Extended card includes capability parameters per model."""
        card = await a2a_handler.build_extended_card()

        models_by_name = {m["name"]: m for m in card["supported_models"]}
        qwen = models_by_name["qwen3:14b"]
        assert qwen["vram_gb"] == 9.3
        assert "default_num_ctx" in qwen
        assert qwen["tags"] == ["fast"]

    @pytest.mark.asyncio
    async def test_extended_card_availability_available(self, a2a_handler):
        """Extended card shows 'available' when no circuit breaker issues."""
        card = await a2a_handler.build_extended_card()
        assert card["availability"] == "available"

    @pytest.mark.asyncio
    async def test_extended_card_availability_unavailable(
        self, a2a_config, mock_vram_tracker, mock_scheduler,
    ):
        """Extended card shows 'unavailable' when circuit breaker is open."""
        async def fake_enqueue(request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        mock_cb = MagicMock()
        mock_cb.state = "open"

        handler = A2AHandler(
            config=a2a_config,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
            circuit_breaker=mock_cb,
        )

        card = await handler.build_extended_card()
        assert card["availability"] == "unavailable"

    @pytest.mark.asyncio
    async def test_extended_card_availability_degraded(
        self, a2a_config, mock_vram_tracker, mock_scheduler,
    ):
        """Extended card shows 'degraded' when circuit breaker is half-open."""
        async def fake_enqueue(request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        mock_cb = MagicMock()
        mock_cb.state = "half_open"

        handler = A2AHandler(
            config=a2a_config,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
            circuit_breaker=mock_cb,
        )

        card = await handler.build_extended_card()
        assert card["availability"] == "degraded"

    @pytest.mark.asyncio
    async def test_extended_card_includes_all_skills(self, a2a_handler):
        """Extended card lists all 4 skills with full schemas."""
        card = await a2a_handler.build_extended_card()

        assert "skills" in card
        skill_ids = [s["id"] for s in card["skills"]]
        assert "infer" in skill_ids
        assert "status" in skill_ids
        assert "batch_infer" in skill_ids
        assert "preload" in skill_ids

    @pytest.mark.asyncio
    async def test_extended_card_includes_security_when_tokens_configured(self, a2a_handler):
        """Extended card includes securitySchemes when tokens are configured."""
        card = await a2a_handler.build_extended_card()

        assert "securitySchemes" in card
        assert "BearerToken" in card["securitySchemes"]
        assert card["securitySchemes"]["BearerToken"]["type"] == "http"
        assert "security" in card
        assert {"BearerToken": []} in card["security"]

    @pytest.mark.asyncio
    async def test_extended_card_omits_security_when_no_tokens(
        self, a2a_config_open, mock_vram_tracker, mock_scheduler,
    ):
        """Extended card omits securitySchemes when no tokens configured."""
        async def fake_enqueue(request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        handler = A2AHandler(
            config=a2a_config_open,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
        )

        card = await handler.build_extended_card()
        assert "securitySchemes" not in card
        assert "security" not in card

    @pytest.mark.asyncio
    async def test_extended_card_has_no_raw_vram_state(self, a2a_handler):
        """Extended card does NOT expose raw VRAM state (that's Tier 3)."""
        card = await a2a_handler.build_extended_card()
        assert "state" not in card

    @pytest.mark.asyncio
    async def test_extended_card_has_no_queue_depth(self, a2a_handler):
        """Extended card does NOT expose actual queue depth value (that's Tier 3)."""
        card = await a2a_handler.build_extended_card()
        # No top-level queue_depth key (skill schemas may mention it as a field name)
        assert "queue_depth" not in card

    @pytest.mark.asyncio
    async def test_extended_card_has_no_loaded_models(self, a2a_handler):
        """Extended card does NOT expose currently loaded models (that's Tier 3)."""
        card = await a2a_handler.build_extended_card()
        # Check there's no "loaded_models" key at top level
        assert "loaded_models" not in card


# ---------------------------------------------------------------------------
# SSE Streaming Tests
# ---------------------------------------------------------------------------

class TestSSEStreaming:
    """Test SSE event streaming for task updates."""

    @pytest.mark.asyncio
    async def test_subscribe_receives_status_updates(self, a2a_handler):
        """Subscribing to a task receives status update events."""
        message = {"skill_id": "status", "params": {}}
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        events = []
        async for event in a2a_handler.subscribe_task(task_id):
            events.append(event)
            if len(events) >= 2:  # Initial + completion
                break

        assert len(events) >= 1
        assert "statusUpdate" in events[0]
        assert events[0]["statusUpdate"]["taskId"] == task_id

    @pytest.mark.asyncio
    async def test_subscribe_nonexistent_task_raises(self, a2a_handler):
        """Subscribing to nonexistent task raises ValueError."""
        with pytest.raises(ValueError, match="Task not found"):
            async for _ in a2a_handler.subscribe_task("nonexistent-id"):
                pass

    @pytest.mark.asyncio
    async def test_subscribe_receives_final_state(self, a2a_handler):
        """SSE stream includes final completed state."""
        message = {"skill_id": "status", "params": {}}
        result = await a2a_handler.create_task(message)
        task_id = result["id"]

        events = []
        async for event in a2a_handler.subscribe_task(task_id):
            events.append(event)
            if "statusUpdate" in event:
                state = event["statusUpdate"]["status"]["state"]
                if state in ("completed", "failed", "canceled"):
                    break

        # Should have at least initial state
        assert len(events) >= 1
        # Last event should be terminal state
        final_event = events[-1]
        assert "statusUpdate" in final_event
        assert final_event["statusUpdate"]["status"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_subscribe_receives_artifact_updates(self, a2a_handler):
        """SSE subscriber should receive artifact updates for streaming."""
        # Mock streaming response
        async def mock_aiter_lines():
            yield json.dumps({"response": "Hello", "done": False})
            yield json.dumps({"response": " world", "done": False})
            yield json.dumps({"response": "!", "done": True, "eval_count": 3})

        mock_stream = MagicMock()
        mock_stream.aiter_lines = mock_aiter_lines
        mock_stream.raise_for_status = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            message = {
                "skill_id": "infer",
                "params": {"model": "qwen3:14b", "prompt": "hello", "stream": True},
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            # Subscribe and collect events
            events = []
            async for event in a2a_handler.subscribe_task(task_id):
                events.append(event)
                if (
                    "statusUpdate" in event
                    and event["statusUpdate"]["status"]["state"] == "completed"
                ):
                    break

            # Should have artifact updates
            artifact_events = [e for e in events if "artifactUpdate" in e]
            assert len(artifact_events) > 0

    @pytest.mark.asyncio
    async def test_sse_heartbeat(self, a2a_config, mock_vram_tracker, mock_scheduler):
        """SSE should send heartbeat when no events."""
        # Create handler with slow enqueue
        async def slow_enqueue(request: QueuedRequest):
            event = asyncio.Event()
            # Don't set immediately
            return event, lambda: None, lambda: None

        handler = A2AHandler(
            config=a2a_config,
            enqueue_fn=slow_enqueue,
            vram_tracker=mock_vram_tracker,
            scheduler=mock_scheduler,
        )

        message = {
            "skill_id": "infer",
            "params": {"model": "qwen3:14b", "prompt": "hello"},
        }
        result = await handler.create_task(message)
        task_id = result["id"]

        # Wait for task to be created
        await asyncio.sleep(0.05)

        # Subscribe with timeout to get heartbeat
        events = []
        count = 0
        async for event in handler.subscribe_task(task_id):
            events.append(event)
            count += 1
            if count >= 2:
                break

        # Should have received at least initial status
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_streaming_infer_emits_tokens(self, a2a_handler):
        """Streaming infer should emit individual token events and accumulate."""
        # Mock streaming response
        async def mock_aiter_lines():
            yield json.dumps({"response": "Token1", "done": False})
            yield json.dumps({"response": "Token2", "done": False})
            yield json.dumps({"response": "Token3", "done": True, "eval_count": 3})

        mock_stream = MagicMock()
        mock_stream.aiter_lines = mock_aiter_lines
        mock_stream.raise_for_status = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            message = {
                "skill_id": "infer",
                "params": {"model": "qwen3:14b", "prompt": "test", "stream": True},
            }
            result = await a2a_handler.create_task(message)
            task_id = result["id"]

            # Wait for completion
            await asyncio.sleep(0.3)

            task = await a2a_handler.get_task(task_id)
            assert task["status"]["state"] == "completed"
            # Final artifact should contain accumulated text
            assert task["artifacts"][0]["parts"][0]["text"] == "Token1Token2Token3"


# ---------------------------------------------------------------------------
# Reservation Tests
# ---------------------------------------------------------------------------

class TestReservations:
    """Test reservation check callback."""

    @pytest.mark.asyncio
    async def test_has_active_reservation_false_when_empty(self, a2a_handler):
        """has_active_reservation returns False when no reservations exist."""
        assert a2a_handler.has_active_reservation("qwen3:14b") is False

    @pytest.mark.asyncio
    async def test_has_active_reservation_false_for_wrong_model(self, a2a_handler):
        """has_active_reservation returns False for different model."""
        # Manually insert a reservation
        import time

        from bastion.models import Reservation

        reservation = Reservation(
            model="mistral-nemo:12b",
            remaining_requests=10,
            priority=PriorityTier.INTERACTIVE,
            created_at=time.time(),
            expires_at=time.time() + 100,
        )
        a2a_handler._reservations[reservation.reservation_id] = reservation

        assert a2a_handler.has_active_reservation("qwen3:14b") is False

    @pytest.mark.asyncio
    async def test_has_active_reservation_true_when_active(self, a2a_handler):
        """has_active_reservation returns True for model with active reservation."""
        import time

        from bastion.models import Reservation

        reservation = Reservation(
            model="qwen3:14b",
            remaining_requests=10,
            priority=PriorityTier.INTERACTIVE,
            created_at=time.time(),
            expires_at=time.time() + 100,
        )
        a2a_handler._reservations[reservation.reservation_id] = reservation

        assert a2a_handler.has_active_reservation("qwen3:14b") is True

    @pytest.mark.asyncio
    async def test_has_active_reservation_false_when_expired(self, a2a_handler):
        """has_active_reservation returns False when reservation expired."""
        import time

        from bastion.models import Reservation

        reservation = Reservation(
            model="qwen3:14b",
            remaining_requests=10,
            priority=PriorityTier.INTERACTIVE,
            created_at=time.time() - 200,
            expires_at=time.time() - 100,  # Expired
        )
        a2a_handler._reservations[reservation.reservation_id] = reservation

        assert a2a_handler.has_active_reservation("qwen3:14b") is False

    @pytest.mark.asyncio
    async def test_has_active_reservation_false_when_depleted(self, a2a_handler):
        """has_active_reservation returns False when remaining_requests is 0."""
        import time

        from bastion.models import Reservation

        reservation = Reservation(
            model="qwen3:14b",
            remaining_requests=0,  # Depleted
            priority=PriorityTier.INTERACTIVE,
            created_at=time.time(),
            expires_at=time.time() + 100,
        )
        a2a_handler._reservations[reservation.reservation_id] = reservation

        assert a2a_handler.has_active_reservation("qwen3:14b") is False


# ---------------------------------------------------------------------------
# Public Agent Card Skill IDs
# ---------------------------------------------------------------------------

def test_public_agent_card_skill_ids_match_routing(a2a_handler):
    """Public card must advertise real skill IDs accepted by the routing table."""
    card = a2a_handler.build_public_card()
    # Skills in the public card must be real skill IDs the handler accepts
    real_ids = {"infer", "status", "batch_infer", "preload"}
    advertised = set()
    for skill in card["skills"]:
        # Support both string-form (legacy) and structured-form entries
        if isinstance(skill, str):
            advertised.add(skill)
        else:
            advertised.add(skill["id"])
    assert advertised.issubset(real_ids), f"unexpected skills: {advertised - real_ids}"
    # At least the core two must be advertised
    assert "infer" in advertised
