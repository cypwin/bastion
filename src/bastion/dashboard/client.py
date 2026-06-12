"""Async HTTP client for BASTION's admin API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BastionClient:
    """Async HTTP client for BASTION's admin API."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=5.0, headers=headers)

    async def poll(self) -> dict[str, Any]:
        """Fetch /broker/status and return parsed JSON."""
        resp = await self._client.get(f"{self.base_url}/broker/status")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_safe(
        self,
        path: str,
        default: list | dict,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET an admin endpoint, returning ``default`` on any failure.

        The dashboard renders an empty panel either way, so failures are
        logged at DEBUG (endpoint + exception type) — the dashboard log is
        the only place an auth failure, 404, or network partition becomes
        distinguishable from genuinely empty data.
        """
        try:
            resp = await self._client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("GET %s failed: %s: %s", path, type(e).__name__, e)
            return default

    async def get_recent(self) -> list[dict]:
        """Fetch /broker/recent and return parsed JSON."""
        return await self._get_safe("/broker/recent", [])

    async def get_queue(self) -> dict:
        """Fetch /broker/queue for stall diagnostics."""
        return await self._get_safe("/broker/queue", {})

    async def get_health(self) -> dict:
        """Fetch /broker/health for circuit breaker state."""
        return await self._get_safe("/broker/health", {})

    async def get_vram_ledger(self) -> dict:
        """Fetch /broker/vram for VRAM ledger status."""
        return await self._get_safe("/broker/vram", {})

    async def get_watchdog(self) -> dict:
        """Fetch /broker/watchdog for process monitor status."""
        return await self._get_safe("/broker/watchdog", {})

    async def get_counters(self) -> dict:
        """Fetch /broker/counters for cumulative counters + reset_epoch."""
        return await self._get_safe("/broker/counters", {})

    async def get_thrashing(self) -> dict:
        """Fetch /broker/thrashing for per-agent verdicts."""
        return await self._get_safe("/broker/thrashing", {})

    async def get_latency(self, window_s: float = 300.0) -> dict:
        """Fetch /broker/latency for per-model latency percentiles.

        Parameters
        ----------
        window_s
            Rolling window in seconds. Server clamps to [10, 3600].
        """
        return await self._get_safe(
            "/broker/latency", {}, params={"window_s": window_s}
        )

    async def get_catalog(self) -> dict:
        """Fetch /broker/catalog for the registered-models + residency view."""
        return await self._get_safe("/broker/catalog", {})

    async def post_preload(self, model: str) -> dict:
        """Preload a model via /broker/preload."""
        resp = await self._client.post(
            f"{self.base_url}/broker/preload",
            json={"model": model},
        )
        resp.raise_for_status()
        return resp.json()

    async def post_unload(self, model: str) -> dict:
        """Unload a model via /broker/unload."""
        resp = await self._client.post(
            f"{self.base_url}/broker/unload",
            json={"model": model},
        )
        resp.raise_for_status()
        return resp.json()

    async def post_drain(self) -> dict:
        """Toggle drain mode via /broker/drain."""
        resp = await self._client.post(f"{self.base_url}/broker/drain")
        resp.raise_for_status()
        return resp.json()

    async def post_resume(self) -> dict:
        """Resume from drain mode via /broker/resume."""
        resp = await self._client.post(f"{self.base_url}/broker/resume")
        resp.raise_for_status()
        return resp.json()
