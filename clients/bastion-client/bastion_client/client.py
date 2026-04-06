"""BASTION client library.

Provides typed methods for interacting with BASTION's broker API:
- declare_intent(): Pre-announce model sequences for scheduler optimization
- infer(): Submit inference requests with automatic priority injection
- check_vram(): Query GPU/VRAM status
"""
from __future__ import annotations

import logging
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


class BastionClient:
    """Async client for BASTION GPU/LLM broker.

    Wraps BASTION's HTTP API with typed methods and automatic
    X-Broker-Priority header injection based on pipeline stage.

    Usage:
        async with BastionClient() as client:
            await client.declare_intent(profile="council_pipeline")
            result = await client.infer("qwen3:8b", "Hello", tier="council")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_tier: str = "agent",
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_tier = self._resolve_tier(default_tier)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    @staticmethod
    def _resolve_tier(tier: str) -> str:
        """Resolve a pipeline stage name to a BASTION priority tier."""
        return TIER_MAP.get(tier.lower(), "agent")

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

        resp = await self._client.post(
            "/api/generate",
            json=body,
            headers=headers,
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
