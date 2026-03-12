"""FastAPI middleware for request metrics collection.

Wraps all incoming requests to record duration, status code, endpoint,
model (if present), and priority tier. Emits to Prometheus metrics via
the metrics module (gracefully handles absence of prometheus-client).

The middleware extracts:
  - Endpoint: request.url.path
  - Model: parsed from JSON body if present
  - Tier: from X-Broker-Priority header (defaults to "agent")
  - Duration: start to response completion
  - Status: response.status_code
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from bastion.metrics import record_request

logger = logging.getLogger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that records request metrics.

    Extracts model name and tier from request, measures duration,
    and emits to Prometheus counters/histograms via metrics.py.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Intercept request, measure duration, record metrics.

        Parameters
        ----------
        request : Request
            Incoming FastAPI request.
        call_next : Callable
            Next middleware or route handler.

        Returns
        -------
        Response
            Response from the handler.
        """
        start_time = time.time()

        # Extract metadata before forwarding
        endpoint = request.url.path
        model = await self._extract_model(request)
        tier = self._extract_tier(request)

        # Process the request
        response = await call_next(request)

        # Record metrics
        duration = time.time() - start_time
        record_request(
            endpoint=endpoint,
            status_code=response.status_code,
            duration=duration,
            model=model,
            tier=tier,
        )

        return response

    async def _extract_model(self, request: Request) -> str | None:
        """Extract model name from request body if present.

        Ollama requests typically have JSON body with "model" field.
        We need to parse the body, but FastAPI consumes the stream,
        so we cache it for later handlers.

        Parameters
        ----------
        request : Request
            Incoming request.

        Returns
        -------
        str | None
            Model name if found, else None.
        """
        # Only parse body for POST requests to /api/* endpoints
        if request.method != "POST" or not request.url.path.startswith("/api/"):
            return None

        try:
            # Read body (this consumes the stream)
            body_bytes = await request.body()

            # Cache it so downstream handlers can access it
            # FastAPI's Request.body() caches internally after first read
            # so this should be safe

            # Try to parse as JSON
            if body_bytes:
                body = json.loads(body_bytes)
                return body.get("model")
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Not JSON or invalid — skip model extraction
            pass
        except Exception as e:
            logger.warning("Failed to extract model from request body: %s", e)

        return None

    def _extract_tier(self, request: Request) -> str:
        """Extract priority tier from X-Broker-Priority header.

        Parameters
        ----------
        request : Request
            Incoming request.

        Returns
        -------
        str
            Priority tier: interactive, agent, pipeline, or background.
            Defaults to "agent" if header missing or invalid.
        """
        tier = request.headers.get("X-Broker-Priority", "agent").lower()

        # Validate against known tiers
        valid_tiers = {"interactive", "agent", "pipeline", "background"}
        if tier not in valid_tiers:
            logger.debug("Invalid tier '%s', defaulting to 'agent'", tier)
            return "agent"

        return tier
