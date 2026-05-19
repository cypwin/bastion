"""Async HTTP client for BASTION's admin API."""
from __future__ import annotations

from typing import Any

import httpx


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

    async def get_recent(self) -> list[dict]:
        """Fetch /broker/recent and return parsed JSON."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/recent")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return []

    async def get_queue(self) -> dict:
        """Fetch /broker/queue for stall diagnostics."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/queue")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_health(self) -> dict:
        """Fetch /broker/health for circuit breaker state."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/health")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_vram_ledger(self) -> dict:
        """Fetch /broker/vram for VRAM ledger status."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/vram")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_watchdog(self) -> dict:
        """Fetch /broker/watchdog for process monitor status."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/watchdog")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_counters(self) -> dict:
        """Fetch /broker/counters for cumulative counters + reset_epoch."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/counters")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_thrashing(self) -> dict:
        """Fetch /broker/thrashing for per-agent verdicts."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/thrashing")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_latency(self, window_s: float = 300.0) -> dict:
        """Fetch /broker/latency for per-model latency percentiles.

        Parameters
        ----------
        window_s
            Rolling window in seconds. Server clamps to [10, 3600].
        """
        try:
            resp = await self._client.get(
                f"{self.base_url}/broker/latency",
                params={"window_s": window_s},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_catalog(self) -> dict:
        """Fetch /broker/catalog for the registered-models + residency view."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/catalog")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

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
