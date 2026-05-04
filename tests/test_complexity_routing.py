"""Tests for M58 complexity routing config and model override logic."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion.models import (
    BrokerConfig,
    ComplexityRoutingConfig,
    ModelInfo,
    PriorityTier,
    ThrashingDetectionConfig,
)
from bastion.proxy import OllamaProxy


class TestComplexityRoutingConfig:
    def test_defaults(self):
        c = ComplexityRoutingConfig()
        assert c.enabled is True
        assert c.routes == {}
        assert c.complex_action == "reject"

    def test_custom_routes(self):
        c = ComplexityRoutingConfig(
            routes={"simple": "qwen3.5:9b", "moderate": "qwen3.5:35b-a3b"},
        )
        assert c.routes["simple"] == "qwen3.5:9b"
        assert c.routes["moderate"] == "qwen3.5:35b-a3b"

    def test_disabled(self):
        c = ComplexityRoutingConfig(enabled=False)
        assert c.enabled is False


class TestThrashingDetectionConfig:
    def test_defaults(self):
        c = ThrashingDetectionConfig()
        assert c.enabled is True
        assert c.mode == "warn"
        assert c.window_size == 12
        assert c.warn_swap_ratio == 0.5
        assert c.halt_swap_ratio == 0.75
        assert c.cooloff_seconds == 30
        assert c.min_requests_before_eval == 6

    def test_strict_mode(self):
        c = ThrashingDetectionConfig(mode="strict")
        assert c.mode == "strict"


class TestBrokerConfigWithComplexity:
    def test_has_complexity_routing(self):
        c = BrokerConfig()
        assert hasattr(c, "complexity_routing")
        assert isinstance(c.complexity_routing, ComplexityRoutingConfig)

    def test_has_thrashing_detection(self):
        c = BrokerConfig()
        assert hasattr(c, "thrashing_detection")
        assert isinstance(c.thrashing_detection, ThrashingDetectionConfig)


# ---------------------------------------------------------------------------
# Proxy routing tests (Task 4)
# ---------------------------------------------------------------------------


def _make_request(
    path: str = "/api/generate",
    body: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock FastAPI Request."""
    if body is None:
        body = {"model": "qwen3:14b", "prompt": "hello"}
    req = MagicMock()
    req.url.path = path
    req.method = "POST"
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = headers or {"user-agent": "test-client/1.0"}
    return req


class TestComplexityRouting:
    def _make_config(self) -> BrokerConfig:
        return BrokerConfig(
            complexity_routing=ComplexityRoutingConfig(
                enabled=True,
                routes={"simple": "qwen3.5:9b", "moderate": "qwen3.5:35b-a3b"},
            ),
            models={
                "qwen3.5:9b": ModelInfo(vram_gb=8.1),
                "qwen3.5:35b-a3b": ModelInfo(vram_gb=24.8),
                "qwen3:14b": ModelInfo(vram_gb=9.8),
            },
        )

    @pytest.mark.asyncio
    async def test_simple_overrides_model(self):
        """X-Task-Complexity: simple should override model to configured simple model."""
        config = self._make_config()
        captured = {}

        async def mock_enqueue(req):
            captured["model"] = req.model
            captured["body"] = json.loads(req.body)
            event = asyncio.Event()
            event.set()  # Grant immediately
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=mock_enqueue)
        # Mock the forward response to avoid actual HTTP call
        proxy._forward_response = AsyncMock(return_value=MagicMock())
        proxy._stream_response = AsyncMock(return_value=MagicMock())

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "classify this", "stream": False},
            headers={
                "user-agent": "test",
                "x-task-complexity": "simple",
            },
        )
        await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert captured["model"] == "qwen3.5:9b"
        assert captured["body"]["model"] == "qwen3.5:9b"

    @pytest.mark.asyncio
    async def test_moderate_overrides_model(self):
        config = self._make_config()
        captured = {}

        async def mock_enqueue(req):
            captured["model"] = req.model
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=mock_enqueue)
        proxy._forward_response = AsyncMock(return_value=MagicMock())
        proxy._stream_response = AsyncMock(return_value=MagicMock())

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "summarize", "stream": False},
            headers={"user-agent": "test", "x-task-complexity": "moderate"},
        )
        await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert captured["model"] == "qwen3.5:35b-a3b"

    @pytest.mark.asyncio
    async def test_complex_rejected_422(self):
        config = self._make_config()
        proxy = OllamaProxy(config)
        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "reason deeply"},
            headers={"user-agent": "test", "x-task-complexity": "complex"},
        )
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert result.status_code == 422
        body = json.loads(result.body)
        assert body["complexity"] == "complex"

    @pytest.mark.asyncio
    async def test_absent_header_no_override(self):
        """Without X-Task-Complexity header, client model is used as-is."""
        config = self._make_config()
        captured = {}

        async def mock_enqueue(req):
            captured["model"] = req.model
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=mock_enqueue)
        proxy._forward_response = AsyncMock(return_value=MagicMock())
        proxy._stream_response = AsyncMock(return_value=MagicMock())

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "hello", "stream": False},
            headers={"user-agent": "test"},  # no x-task-complexity
        )
        await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert captured["model"] == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_routing_disabled_no_override(self):
        config = BrokerConfig(
            complexity_routing=ComplexityRoutingConfig(enabled=False),
        )
        captured = {}

        async def mock_enqueue(req):
            captured["model"] = req.model
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=mock_enqueue)
        proxy._forward_response = AsyncMock(return_value=MagicMock())
        proxy._stream_response = AsyncMock(return_value=MagicMock())

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "hello", "stream": False},
            headers={"user-agent": "test", "x-task-complexity": "simple"},
        )
        await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert captured["model"] == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_invalid_complexity_value_ignored(self):
        config = self._make_config()
        captured = {}

        async def mock_enqueue(req):
            captured["model"] = req.model
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=mock_enqueue)
        proxy._forward_response = AsyncMock(return_value=MagicMock())
        proxy._stream_response = AsyncMock(return_value=MagicMock())

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "hello", "stream": False},
            headers={"user-agent": "test", "x-task-complexity": "unknown_value"},
        )
        await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert captured["model"] == "qwen3:14b"


class TestStreamingTokenCapture:
    @pytest.mark.asyncio
    async def test_final_chunk_tokens_captured(self):
        """The streaming generator should parse the final done=true chunk for token counts."""
        config = BrokerConfig()
        proxy = OllamaProxy(config)

        # Verify the _extract_streaming_tokens helper works
        final_chunk = b'{"model":"qwen3:14b","done":true,"prompt_eval_count":100,"eval_count":50}\n'
        tokens = proxy._extract_streaming_tokens(final_chunk)
        assert tokens == {"prompt_tokens": 100, "completion_tokens": 50}

    @pytest.mark.asyncio
    async def test_non_final_chunk_returns_none(self):
        config = BrokerConfig()
        proxy = OllamaProxy(config)

        chunk = b'{"model":"qwen3:14b","done":false,"response":"hello"}\n'
        tokens = proxy._extract_streaming_tokens(chunk)
        assert tokens is None


class TestResponseHeaders:
    @pytest.mark.asyncio
    async def test_non_streaming_token_headers(self):
        """Non-streaming responses should include X-Prompt-Tokens and X-Completion-Tokens."""
        config = BrokerConfig(
            complexity_routing=ComplexityRoutingConfig(
                enabled=True,
                routes={"simple": "qwen3.5:9b"},
            ),
            models={"qwen3.5:9b": ModelInfo(vram_gb=8.1)},
        )
        proxy = OllamaProxy(config)

        # Mock the HTTP client response with token counts
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "model": "qwen3.5:9b",
            "response": "classified",
            "done": True,
            "prompt_eval_count": 150,
            "eval_count": 25,
        }
        mock_response.status_code = 200
        proxy._http = MagicMock()
        proxy._http.post = AsyncMock(return_value=mock_response)

        req = _make_request(
            body={"model": "qwen3:14b", "prompt": "classify", "stream": False},
            headers={"user-agent": "test", "x-task-complexity": "simple"},
        )
        result = await proxy._forward_response(
            req, "http://localhost:11435/api/generate",
            json.dumps({"model": "qwen3.5:9b", "prompt": "classify"}).encode(),
            model="qwen3.5:9b", path="/api/generate", tier=PriorityTier.AGENT,
            routing_meta={
                "requested": "qwen3:14b",
                "routed": "qwen3.5:9b",
                "reason": "complexity-simple",
            },
        )
        assert result.headers.get("X-Model-Requested") == "qwen3:14b"
        assert result.headers.get("X-Model-Routed") == "qwen3.5:9b"
        assert result.headers.get("X-Routing-Reason") == "complexity-simple"
        assert result.headers.get("X-Prompt-Tokens") == "150"
        assert result.headers.get("X-Completion-Tokens") == "25"


class TestThrashingIntegration:
    @pytest.mark.asyncio
    async def test_halt_returns_429(self):
        """In strict mode, thrashing agent gets 429."""
        from bastion.thrashing import ThrashingDetector

        config = BrokerConfig(
            thrashing_detection=ThrashingDetectionConfig(
                enabled=True, mode="strict",
                window_size=6, min_requests_before_eval=3,
                warn_swap_ratio=0.3, halt_swap_ratio=0.5,
            ),
        )
        detector = ThrashingDetector(config.thrashing_detection)
        proxy = OllamaProxy(config, thrashing_detector=detector)

        # Pre-fill detector with thrashing pattern
        for i in range(6):
            detector.record_request("thrash_agent", "modelA" if i % 2 == 0 else "modelB")

        req = _make_request(
            body={"model": "modelA", "prompt": "test"},
            headers={
                "user-agent": "test",
                "x-agent-id": "thrash_agent",
            },
        )
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())
        assert result.status_code == 429
        body = json.loads(result.body)
        assert "thrashing" in body["error"].lower()
