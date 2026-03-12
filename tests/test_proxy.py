"""Tests for OllamaProxy — use_mmap injection, priority detection, routing."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.models import BrokerConfig, PriorityTier, ProxyConfig, QueuedRequest
from bastion.proxy import OllamaProxy


def _make_request(
    path: str = "/api/generate",
    method: str = "POST",
    body: bytes = b'{"model": "qwen3:14b", "prompt": "hello"}',
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock FastAPI Request."""
    req = MagicMock()
    req.url.path = path
    req.method = method
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {"user-agent": "test-client/1.0"}
    return req


class TestPriorityDetection:
    def test_explicit_header(self):
        proxy = OllamaProxy(BrokerConfig())
        req = _make_request(headers={
            "x-broker-priority": "pipeline",
            "user-agent": "test",
        })
        assert proxy._detect_priority(req) == PriorityTier.PIPELINE

    def test_ollama_cli_gets_interactive(self):
        proxy = OllamaProxy(BrokerConfig())
        req = _make_request(headers={
            "user-agent": "ollama/0.4.0 (Linux x86_64)",
        })
        assert proxy._detect_priority(req) == PriorityTier.INTERACTIVE

    def test_unknown_client_gets_agent(self):
        proxy = OllamaProxy(BrokerConfig())
        req = _make_request(headers={"user-agent": "python-httpx/0.27"})
        assert proxy._detect_priority(req) == PriorityTier.AGENT

    def test_invalid_header_falls_through(self):
        proxy = OllamaProxy(BrokerConfig())
        req = _make_request(headers={
            "x-broker-priority": "invalid_tier",
            "user-agent": "test",
        })
        assert proxy._detect_priority(req) == PriorityTier.AGENT


class TestEndpointClassification:
    def test_scheduled_endpoints(self):
        proxy = OllamaProxy(BrokerConfig())
        assert "/api/generate" in proxy._scheduled_endpoints
        assert "/api/chat" in proxy._scheduled_endpoints
        assert "/api/embed" in proxy._scheduled_endpoints

    def test_passthrough_endpoints(self):
        proxy = OllamaProxy(BrokerConfig())
        assert "/api/tags" in proxy._passthrough_endpoints
        assert "/api/ps" in proxy._passthrough_endpoints
        assert "/api/pull" in proxy._passthrough_endpoints


class TestUseMmapInjection:
    @pytest.mark.asyncio
    async def test_injects_use_mmap_false(self):
        """Scheduled requests get use_mmap: false injected."""
        config = BrokerConfig()
        captured_body = None

        async def fake_enqueue(queued: QueuedRequest):
            nonlocal captured_body
            captured_body = queued.body
            event = asyncio.Event()
            event.set()  # Grant immediately
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=fake_enqueue)

        # Mock the HTTP forward so it doesn't actually connect
        mock_resp = httpx.Response(200, json={"response": "hi", "done": True},
                                   request=httpx.Request("POST", "http://mock"))
        with patch.object(proxy._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(body=b'{"model": "qwen3:14b", "prompt": "test", "stream": false}')
            await proxy.handle_request(req)

        assert captured_body is not None
        payload = json.loads(captured_body)
        assert payload["options"]["use_mmap"] is False

    @pytest.mark.asyncio
    async def test_does_not_override_explicit_use_mmap(self):
        """If client explicitly sets use_mmap, don't override."""
        config = BrokerConfig()
        captured_body = None

        async def fake_enqueue(queued: QueuedRequest):
            nonlocal captured_body
            captured_body = queued.body
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(config, enqueue_fn=fake_enqueue)

        body = json.dumps({
            "model": "qwen3:14b", "prompt": "test", "stream": False,
            "options": {"use_mmap": True},
        }).encode()

        mock_resp = httpx.Response(200, json={"response": "hi", "done": True},
                                   request=httpx.Request("POST", "http://mock"))
        with patch.object(proxy._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(body=body)
            await proxy.handle_request(req)

        payload = json.loads(captured_body)
        assert payload["options"]["use_mmap"] is True  # Preserved client's choice


class TestSchedulerIntegration:
    @pytest.mark.asyncio
    async def test_enqueue_called_for_scheduled(self):
        """Scheduled endpoints call enqueue_fn."""
        enqueue_called = False

        async def fake_enqueue(queued: QueuedRequest):
            nonlocal enqueue_called
            enqueue_called = True
            assert queued.model == "qwen3:14b"
            assert queued.endpoint == "/api/generate"
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=fake_enqueue)
        mock_resp = httpx.Response(200, json={"response": "ok", "done": True},
                                   request=httpx.Request("POST", "http://mock"))
        with patch.object(proxy._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(body=b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}')
            await proxy.handle_request(req)

        assert enqueue_called is True

    @pytest.mark.asyncio
    async def test_passthrough_bypasses_scheduler(self):
        """Passthrough endpoints do NOT call enqueue_fn."""
        enqueue_called = False

        async def fake_enqueue(queued: QueuedRequest):
            nonlocal enqueue_called
            enqueue_called = True
            event = asyncio.Event()
            event.set()
            return event, lambda: None, lambda: None

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=fake_enqueue)
        mock_resp = httpx.Response(200, json={"models": []},
                                   request=httpx.Request("GET", "http://mock"))
        with patch.object(proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(path="/api/tags", method="GET", body=b"")
            await proxy.handle_request(req)

        assert enqueue_called is False

    @pytest.mark.asyncio
    async def test_queue_full_returns_503(self):
        """When enqueue_fn raises, proxy returns 503."""
        async def failing_enqueue(queued: QueuedRequest) -> asyncio.Event:
            raise RuntimeError("Queue full")

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=failing_enqueue)
        req = _make_request(body=b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}')
        resp = await proxy.handle_request(req)
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_queue_timeout_returns_504(self):
        """When grant event is never set, proxy returns 504 after timeout."""
        async def slow_enqueue(queued: QueuedRequest):
            return asyncio.Event(), lambda: None, lambda: None  # Event never set → triggers timeout

        # Use a very short queue timeout for testing
        config = BrokerConfig(proxy=ProxyConfig(queue_timeout_seconds=0.05))
        proxy = OllamaProxy(config, enqueue_fn=slow_enqueue)
        req = _make_request(body=b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}')
        resp = await proxy.handle_request(req)

        assert resp.status_code == 504

    @pytest.mark.asyncio
    async def test_direct_mode_without_enqueue_fn(self):
        """Without enqueue_fn, proxy forwards directly (no scheduling)."""
        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=None)
        mock_resp = httpx.Response(200, json={"response": "direct", "done": True},
                                   request=httpx.Request("POST", "http://mock"))
        with patch.object(proxy._http, "post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(body=b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}')
            resp = await proxy.handle_request(req)

        assert resp.status_code == 200


class TestInvalidInput:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        proxy = OllamaProxy(BrokerConfig())
        req = _make_request(body=b"not json at all{{{")
        resp = await proxy.handle_request(req)
        assert resp.status_code == 400
