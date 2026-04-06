"""Token-bucket rate limiter for BASTION.

Applies per-client-IP rate limiting using a token-bucket algorithm.
Each client gets a bucket that refills at ``requests_per_minute / 60``
tokens per second up to a maximum of ``burst`` tokens.

When a client exhausts their tokens, the middleware returns 429 Too Many
Requests with a ``Retry-After`` header indicating how many seconds until
the next token becomes available.

If rate limiting is disabled (``requests_per_minute == 0`` or
``enabled == False``), all requests pass through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = False
    requests_per_minute: int = 60
    burst: int = 10


class _TokenBucket:
    """Single token bucket for one client.

    Parameters
    ----------
    rate : float
        Tokens added per second.
    burst : int
        Maximum tokens the bucket can hold.
    """

    __slots__ = ("rate", "burst", "tokens", "last_refill")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens: float = float(burst)
        self.last_refill: float = time.monotonic()

    def consume(self) -> float:
        """Try to consume one token.

        Returns
        -------
        float
            0.0 if a token was consumed (request allowed), otherwise the
            number of seconds the caller must wait before a token is
            available.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now

        # Refill tokens based on elapsed time
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0

        # Calculate wait time until one token is available
        deficit = 1.0 - self.tokens
        return deficit / self.rate if self.rate > 0 else 1.0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces per-IP token-bucket rate limiting.

    Parameters
    ----------
    app : ASGIApp
        The ASGI application (provided by Starlette when adding middleware).
    config : RateLimitConfig
        Rate limiting settings.
    """

    def __init__(self, app: object, config: RateLimitConfig | None = None) -> None:
        super().__init__(app)
        self._config = config or RateLimitConfig()
        self._rate: float = self._config.requests_per_minute / 60.0
        self._burst: int = self._config.burst
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = asyncio.Lock()

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from the request.

        Parameters
        ----------
        request : Request
            Incoming request.

        Returns
        -------
        str
            Client IP address (falls back to "unknown" if unavailable).
        """
        # Prefer X-Forwarded-For for reverse-proxy setups
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Apply rate limiting per client IP.

        Parameters
        ----------
        request : Request
            Incoming FastAPI request.
        call_next : Callable
            Next middleware or route handler.

        Returns
        -------
        Response
            Either a 429 JSON error or the downstream response.
        """
        # Skip when disabled or rate is zero
        if not self._config.enabled or self._config.requests_per_minute <= 0:
            return await call_next(request)

        client_ip = self._get_client_ip(request)

        async with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                bucket = _TokenBucket(rate=self._rate, burst=self._burst)
                self._buckets[client_ip] = bucket
            wait_seconds = bucket.consume()

        if wait_seconds > 0:
            retry_after = int(wait_seconds) + 1  # Round up to whole seconds
            logger.info(
                "Rate limited client %s, retry after %ds", client_ip, retry_after
            )
            return JSONResponse(
                {"error": "Too many requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
