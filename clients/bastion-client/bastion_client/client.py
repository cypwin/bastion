"""BASTION client library.

Provides typed methods for interacting with BASTION's broker API:
- declare_intent(): Pre-announce model sequences for scheduler optimization
- infer(): Submit inference requests with automatic priority injection
- chat(): Convenience wrapper for Ollama's /api/chat endpoint
- embed(): Convenience wrapper for Ollama's /api/embed endpoint
- check_vram(): Query GPU/VRAM status
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import httpx

from bastion_client.models import IntentRequest, IntentResponse, VRAMInfo

logger = logging.getLogger(__name__)

# Priority tier mapping for pipeline stages
TIER_MAP: dict[str, str] = {
    "interactive": "interactive",
    "council": "interactive",
    "agent": "agent",
    "pipeline": "pipeline",
    "extraction": "pipeline",
    "analysis": "pipeline",
    "background": "background",
    "embedding": "background",
    "indexing": "background",
}

# HTTP status codes that trigger retry
_RETRYABLE_STATUS_CODES: set[int] = {502, 503, 504}


class BastionClient:
    """Async client for BASTION GPU/LLM broker.

    Wraps BASTION's HTTP API with typed methods and automatic
    X-Broker-Priority header injection based on pipeline stage.

    Usage:
        async with BastionClient() as client:
            await client.declare_intent(profile="council_pipeline")
            result = await client.infer("qwen3:8b", "Hello", tier="council")
            reply = await client.chat("qwen3:8b", [{"role": "user", "content": "Hi"}])
            vectors = await client.embed("nomic-embed-text", "Hello world")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_tier: str = "agent",
        timeout: float = 300.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_tier = self._resolve_tier(default_tier)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    @staticmethod
    def _resolve_tier(tier: str) -> str:
        """Resolve a pipeline stage name to a BASTION priority tier."""
        return TIER_MAP.get(tier.lower(), "agent")

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with exponential backoff retry.

        Retries on httpx.ConnectError, httpx.TimeoutException, and
        HTTP 502/503/504 responses. Uses exponential backoff:
        delay * 2^attempt.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.request(
                    method, url, json=json, headers=headers,
                )
                if resp.status_code not in _RETRYABLE_STATUS_CODES:
                    return resp
                # Retryable HTTP status — treat like a transient error
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc

            if attempt < self.max_retries - 1:
                backoff = self.retry_delay * (2 ** attempt)
                logger.warning(
                    "Request to %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    url, attempt + 1, self.max_retries, backoff, last_exc,
                )
                await asyncio.sleep(backoff)

        # Exhausted all retries — raise or re-raise
        if isinstance(last_exc, httpx.HTTPStatusError):
            last_exc.response.raise_for_status()
        raise last_exc  # type: ignore[misc]

    async def declare_intent(
        self,
        *,
        profile: str | None = None,
        model_sequence: list[str] | None = None,
        estimated_requests: int = 10,
        client_id: str = "bastion_client",
    ) -> IntentResponse:
        """Declare an upcoming model sequence for scheduler optimization."""
        req = IntentRequest(
            profile=profile,
            model_sequence=model_sequence,
            estimated_requests=estimated_requests,
            client_id=client_id,
        )
        resp = await self._client.post(
            "/broker/intent",
            json=req.model_dump(exclude_none=True),
        )
        resp.raise_for_status()
        return IntentResponse(**resp.json())

    async def infer(
        self,
        model: str,
        prompt: str,
        *,
        tier: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit an inference request with automatic priority injection."""
        resolved_tier = self._resolve_tier(tier) if tier else self.default_tier
        headers = {"X-Broker-Priority": resolved_tier}

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
            **kwargs,
        }

        resp = await self._request_with_retry(
            "POST", "/api/generate", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        tier: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request via Ollama's /api/chat endpoint.

        Args:
            model: Model name (e.g. "qwen3:8b").
            messages: List of message dicts with "role" and "content" keys.
            tier: Priority tier override (resolved via TIER_MAP).
            stream: Whether to stream the response (default False).
            **kwargs: Additional Ollama chat parameters.

        Returns:
            Parsed JSON response from Ollama.
        """
        resolved_tier = self._resolve_tier(tier) if tier else self.default_tier
        headers = {"X-Broker-Priority": resolved_tier}

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }

        resp = await self._request_with_retry(
            "POST", "/api/chat", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def embed(
        self,
        model: str,
        input: str | list[str],
        *,
        tier: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate embeddings via Ollama's /api/embed endpoint.

        Args:
            model: Embedding model name (e.g. "nomic-embed-text").
            input: Text or list of texts to embed.
            tier: Priority tier override (resolved via TIER_MAP).
            **kwargs: Additional Ollama embed parameters.

        Returns:
            Parsed JSON response containing embedding vectors.
        """
        resolved_tier = self._resolve_tier(tier) if tier else self.default_tier
        headers = {"X-Broker-Priority": resolved_tier}

        body: dict[str, Any] = {
            "model": model,
            "input": input,
            **kwargs,
        }

        resp = await self._request_with_retry(
            "POST", "/api/embed", json=body, headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def check_vram(self) -> VRAMInfo:
        """Query BASTION for current VRAM status."""
        resp = await self._client.get("/broker/status")
        resp.raise_for_status()
        data = resp.json()

        gpu = data.get("gpu", {})
        loaded = data.get("loaded_models", [])

        total_mb = gpu.get("vram_total_mb") or 0
        used_mb = gpu.get("vram_used_mb") or 0
        free_mb = gpu.get("vram_free_mb") or 0

        return VRAMInfo(
            total_vram_gb=total_mb / 1024,
            used_vram_gb=used_mb / 1024,
            free_vram_gb=free_mb / 1024,
            loaded_models=[m.get("name", "") for m in loaded],
            utilization_pct=(used_mb / total_mb * 100) if total_mb > 0 else 0.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> BastionClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class SyncBastionClient:
    """Synchronous wrapper around BastionClient.

    Provides the same API as BastionClient but blocks on each call.
    Uses a dedicated event loop running in a background thread to
    avoid conflicts with any existing event loop in the caller's thread.

    Usage:
        with SyncBastionClient() as client:
            result = client.infer("qwen3:8b", "Hello")
            reply = client.chat("qwen3:8b", [{"role": "user", "content": "Hi"}])
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_tier: str = "agent",
        timeout: float = 300.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._async_client = BastionClient(
            base_url=base_url,
            default_tier=default_tier,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

    def _run(self, coro: Any) -> Any:
        """Submit a coroutine to the background loop and wait for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def declare_intent(
        self,
        *,
        profile: str | None = None,
        model_sequence: list[str] | None = None,
        estimated_requests: int = 10,
        client_id: str = "bastion_client",
    ) -> IntentResponse:
        """Declare an upcoming model sequence for scheduler optimization."""
        return self._run(
            self._async_client.declare_intent(
                profile=profile,
                model_sequence=model_sequence,
                estimated_requests=estimated_requests,
                client_id=client_id,
            )
        )

    def infer(
        self,
        model: str,
        prompt: str,
        *,
        tier: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit an inference request with automatic priority injection."""
        return self._run(
            self._async_client.infer(model, prompt, tier=tier, stream=stream, **kwargs)
        )

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        tier: str | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request via Ollama's /api/chat endpoint."""
        return self._run(
            self._async_client.chat(model, messages, tier=tier, stream=stream, **kwargs)
        )

    def embed(
        self,
        model: str,
        input: str | list[str],
        *,
        tier: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate embeddings via Ollama's /api/embed endpoint."""
        return self._run(
            self._async_client.embed(model, input, tier=tier, **kwargs)
        )

    def check_vram(self) -> VRAMInfo:
        """Query BASTION for current VRAM status."""
        return self._run(self._async_client.check_vram())

    def close(self) -> None:
        """Close the underlying async client and shut down the event loop."""
        self._run(self._async_client.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()

    def __enter__(self) -> SyncBastionClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
