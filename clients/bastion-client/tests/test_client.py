"""Tests for bastion-client package."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion_client import BastionClient, IntentRequest, IntentResponse, VRAMInfo
from bastion_client.client import TIER_MAP


# ---------------------------------------------------------------------------
# TIER_MAP resolution
# ---------------------------------------------------------------------------


class TestTierMap:
    def test_council_maps_to_interactive(self) -> None:
        assert BastionClient._resolve_tier("council") == "interactive"

    def test_extraction_maps_to_pipeline(self) -> None:
        assert BastionClient._resolve_tier("extraction") == "pipeline"

    def test_embedding_maps_to_background(self) -> None:
        assert BastionClient._resolve_tier("embedding") == "background"

    def test_agent_maps_to_agent(self) -> None:
        assert BastionClient._resolve_tier("agent") == "agent"

    def test_unknown_stage_defaults_to_agent(self) -> None:
        assert BastionClient._resolve_tier("unknown_thing") == "agent"

    def test_case_insensitive(self) -> None:
        assert BastionClient._resolve_tier("COUNCIL") == "interactive"
        assert BastionClient._resolve_tier("Extraction") == "pipeline"

    def test_all_tier_map_entries_resolve(self) -> None:
        for stage, expected_tier in TIER_MAP.items():
            assert BastionClient._resolve_tier(stage) == expected_tier


# ---------------------------------------------------------------------------
# IntentRequest model
# ---------------------------------------------------------------------------


class TestIntentRequest:
    def test_profile_only(self) -> None:
        req = IntentRequest(profile="council_pipeline", client_id="test")
        assert req.profile == "council_pipeline"
        assert req.model_sequence is None

    def test_model_sequence_only(self) -> None:
        req = IntentRequest(model_sequence=["model_a", "model_b"])
        assert req.model_sequence == ["model_a", "model_b"]
        assert req.profile is None

    def test_defaults(self) -> None:
        req = IntentRequest()
        assert req.estimated_requests == 10
        assert req.client_id == "anonymous"

    def test_exclude_none(self) -> None:
        req = IntentRequest(profile="test")
        dumped = req.model_dump(exclude_none=True)
        assert "model_sequence" not in dumped
        assert dumped["profile"] == "test"


# ---------------------------------------------------------------------------
# BastionClient construction
# ---------------------------------------------------------------------------


class TestBastionClientInit:
    def test_default_url(self) -> None:
        client = BastionClient()
        assert client.base_url == "http://localhost:11434"
        assert client.default_tier == "agent"

    def test_custom_url_and_tier(self) -> None:
        client = BastionClient(
            base_url="http://gpu-box:11434/",
            default_tier="interactive",
        )
        assert client.base_url == "http://gpu-box:11434"
        assert client.default_tier == "interactive"

    def test_pipeline_stage_resolved_as_tier(self) -> None:
        """Passing a stage name like 'council' should resolve to 'interactive'."""
        client = BastionClient(default_tier="council")
        assert client.default_tier == "interactive"


# ---------------------------------------------------------------------------
# Priority header injection
# ---------------------------------------------------------------------------


class TestPriorityHeaderInjection:
    @pytest.mark.asyncio
    async def test_infer_injects_default_tier(self) -> None:
        """infer() should inject X-Broker-Priority with the resolved default tier."""
        client = BastionClient(default_tier="pipeline")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "model": "test-model",
            "response": "hello",
            "done": True,
        }

        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.infer("test-model", "hello")

        call_kwargs = client._client.post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers["X-Broker-Priority"] == "pipeline"
        assert result["response"] == "hello"

    @pytest.mark.asyncio
    async def test_infer_tier_override(self) -> None:
        """Explicit tier param should override default."""
        client = BastionClient(default_tier="agent")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "model": "test-model",
            "response": "hi",
            "done": True,
        }

        client._client.post = AsyncMock(return_value=mock_response)

        await client.infer("test-model", "test", tier="interactive")

        call_kwargs = client._client.post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers["X-Broker-Priority"] == "interactive"

    @pytest.mark.asyncio
    async def test_infer_resolves_stage_name_to_tier(self) -> None:
        """Pipeline stage names in tier= should be resolved to priority tiers."""
        client = BastionClient(default_tier="agent")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"model": "m", "response": "", "done": True}

        client._client.post = AsyncMock(return_value=mock_response)

        await client.infer("m", "test", tier="council")

        call_kwargs = client._client.post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers["X-Broker-Priority"] == "interactive"

    @pytest.mark.asyncio
    async def test_infer_passes_model_and_prompt(self) -> None:
        """infer() should include model and prompt in the request body."""
        client = BastionClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "model": "qwen3:14b",
            "response": "output",
            "done": True,
        }

        client._client.post = AsyncMock(return_value=mock_response)

        await client.infer("qwen3:14b", "What is 2+2?")

        call_kwargs = client._client.post.call_args
        body = call_kwargs.kwargs.get("json", {})
        assert body["model"] == "qwen3:14b"
        assert body["prompt"] == "What is 2+2?"
        assert body["stream"] is False


# ---------------------------------------------------------------------------
# declare_intent
# ---------------------------------------------------------------------------


class TestDeclareIntent:
    @pytest.mark.asyncio
    async def test_declare_with_profile(self) -> None:
        client = BastionClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "intent_id": "abc123",
            "model_sequence": ["model_a", "model_b"],
            "resolved_priority": "interactive",
            "estimated_requests": 10,
            "status": "registered",
        }

        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.declare_intent(profile="council_pipeline")

        assert isinstance(result, IntentResponse)
        assert result.intent_id == "abc123"
        assert result.resolved_priority == "interactive"
        assert result.status == "registered"

        call_kwargs = client._client.post.call_args
        assert call_kwargs.args[0] == "/broker/intent"

    @pytest.mark.asyncio
    async def test_declare_with_ad_hoc_sequence(self) -> None:
        client = BastionClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "intent_id": "xyz789",
            "model_sequence": ["model_x"],
            "resolved_priority": "agent",
            "estimated_requests": 5,
            "status": "registered",
        }

        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.declare_intent(
            model_sequence=["model_x"],
            estimated_requests=5,
        )

        assert result.intent_id == "xyz789"
        assert result.estimated_requests == 5


# ---------------------------------------------------------------------------
# check_vram
# ---------------------------------------------------------------------------


class TestCheckVRAM:
    @pytest.mark.asyncio
    async def test_check_vram_parses_status(self) -> None:
        client = BastionClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "gpu": {
                "vram_used_mb": 18432,
                "vram_free_mb": 14336,
                "vram_total_mb": 32768,
                "temperature_c": 65,
            },
            "loaded_models": [
                {"name": "qwen3:14b", "vram_gb": 9.3},
                {"name": "nomic-embed-text", "vram_gb": 0.4},
            ],
        }

        client._client.get = AsyncMock(return_value=mock_response)

        vram = await client.check_vram()

        assert isinstance(vram, VRAMInfo)
        assert vram.total_vram_gb == 32768 / 1024
        assert vram.used_vram_gb == 18432 / 1024
        assert vram.free_vram_gb == 14336 / 1024
        assert "qwen3:14b" in vram.loaded_models
        assert "nomic-embed-text" in vram.loaded_models
        assert vram.utilization_pct > 0

    @pytest.mark.asyncio
    async def test_check_vram_handles_zero_total(self) -> None:
        client = BastionClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "gpu": {},
            "loaded_models": [],
        }

        client._client.get = AsyncMock(return_value=mock_response)

        vram = await client.check_vram()

        assert vram.total_vram_gb == 0.0
        assert vram.utilization_pct == 0.0
        assert vram.loaded_models == []


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self) -> None:
        async with BastionClient() as client:
            assert client._client is not None
        # After exiting, the client should be closed
        assert client._client.is_closed
