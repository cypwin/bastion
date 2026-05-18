"""Transparent Ollama HTTP proxy with streaming NDJSON passthrough.

This is the core of BASTION — every request to Ollama passes through here.
The proxy intercepts requests, applies safety overrides (use_mmap:false),
and delegates to the scheduler for queue placement.

Streaming is critical: Ollama returns NDJSON (newline-delimited JSON) for
/api/generate and /api/chat when stream=true. The proxy must pass these
chunks through without buffering, or `ollama run` will appear frozen.

Scheduling integration:
  When an enqueue_fn is provided (from server.py), scheduled requests are
  placed in the AffinityQueue and await a grant signal from the Scheduler.
  When no enqueue_fn is set, requests forward directly (passthrough mode).

Implements use_mmap fix and SOCKS5 proxy detection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from bastion import audit
from bastion.circuitbreaker import CircuitBreaker
from bastion.models import BrokerConfig, PriorityTier, QueuedRequest

logger = logging.getLogger(__name__)

# Headers to NOT forward (hop-by-hop)
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade"}


class OllamaProxy:
    """Transparent reverse proxy to Ollama backend.

    Parameters
    ----------
    config : BrokerConfig
        Broker configuration.
    enqueue_fn : callable, optional
        Async callback to enqueue a scheduled request. Signature:
        ``async def enqueue(request: QueuedRequest) -> (Event, done_fn, cancel_fn)``
        Returns a triple: grant Event, done callback (inference complete),
        and cancel callback (timeout/abort cleanup).
        If None, requests forward directly to Ollama (no scheduling).
    intent_lookup_fn : callable, optional
        Callback to look up an active intent by intent_id. Signature:
        ``def lookup(intent_id: str) -> Optional[tuple[PriorityTier, List[str]]]``
        Returns (resolved_priority, model_sequence) or None.
    """

    def __init__(
        self,
        config: BrokerConfig,
        enqueue_fn: (
            Callable[
                [QueuedRequest],
                Awaitable[tuple[asyncio.Event, Callable[[], None], Callable[[], None]]],
            ]
            | None
        ) = None,
        record_fn: Callable[..., None] | None = None,
        intent_lookup_fn: Callable[[str], tuple[PriorityTier, list] | None] | None = None,
        thrashing_detector: Any | None = None,
    ) -> None:
        self.config = config
        self._backend_url = config.ollama.base_url
        self._enqueue_fn = enqueue_fn
        self._record_fn = record_fn
        self._intent_lookup_fn = intent_lookup_fn
        self._thrashing_detector = thrashing_detector
        # Use configured timeouts
        proxy_cfg = config.proxy
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                proxy_cfg.inference_timeout_seconds,
                connect=proxy_cfg.connect_timeout_seconds,
            )
        )
        self._queue_timeout = proxy_cfg.queue_timeout_seconds
        self._scheduled_endpoints = proxy_cfg.scheduled_endpoints
        self._passthrough_endpoints = proxy_cfg.passthrough_endpoints
        self._max_body_bytes = proxy_cfg.max_request_body_bytes
        self._start_time = time.time()
        self._requests_served = 0
        self._model_swaps = 0

        # Drain-state callable; wired by server.py after the scheduler is built
        # so that _handle_passthrough can reject inference-adjacent requests
        # while management endpoints (/api/tags, /api/show, /api/ps, etc.)
        # continue to serve.  When None, drain has no effect on passthrough.
        self._is_draining_fn: Callable[[], bool] | None = None

        # Circuit breaker for Ollama backend
        cb_config = config.circuit_breaker
        self.circuit_breaker: CircuitBreaker | None = (
            CircuitBreaker(cb_config) if cb_config.enabled else None
        )

    async def handle_request(self, request: Request) -> StreamingResponse | JSONResponse:
        """Route an incoming request to the appropriate handler.

        For scheduled endpoints (/api/generate, /api/chat, /api/embed):
          1. Parse body to extract model name
          2. Inject safety overrides (use_mmap: false)
          3. Detect priority tier from headers / User-Agent
          4. Forward to Ollama (with streaming if requested)

        For passthrough endpoints: forward directly.
        """
        path = request.url.path

        # Read the request body once
        body = await request.body()

        # Request body size validation
        if len(body) > self._max_body_bytes:
            return JSONResponse(
                {"error": f"Request body too large ({len(body)} bytes, "
                 f"max {self._max_body_bytes})"},
                status_code=413,
            )

        # Determine if this is a scheduled or passthrough endpoint
        if path in self._scheduled_endpoints:
            return await self._handle_scheduled(request, path, body)
        else:
            return await self._handle_passthrough(request, path, body)

    async def _handle_scheduled(
        self, request: Request, path: str, body: bytes,
    ) -> StreamingResponse | JSONResponse:
        """Handle a request that may trigger model loading.

        If an enqueue_fn is set, the request is placed in the scheduler queue
        and this coroutine blocks until the scheduler grants it (model is loaded
        and ready). Otherwise, forwards directly to Ollama.
        """
        # Parse body to extract model name and streaming flag
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        model = payload.get("model", "")
        is_streaming = payload.get("stream", True)  # Ollama defaults to stream=true

        # --- M58: Complexity-based model routing ---
        routing_meta: dict[str, str] | None = None
        task_complexity = request.headers.get("x-task-complexity", "").lower().strip()

        if task_complexity and self.config.complexity_routing.enabled:
            if task_complexity == "complex":
                return JSONResponse(
                    {
                        "error": (
                            "Task complexity 'complex' requires Claude, "
                            "not local model. Route to API."
                        ),
                        "complexity": "complex",
                    },
                    status_code=422,
                )

            route_model = self.config.complexity_routing.routes.get(task_complexity)
            if route_model:
                original_model = model
                model = route_model
                payload["model"] = model
                routing_meta = {
                    "requested": original_model,
                    "routed": model,
                    "reason": f"complexity-{task_complexity}",
                }
                logger.info(
                    "M58 routing: %s -> %s (complexity=%s, agent=%s)",
                    original_model, model, task_complexity,
                    request.headers.get("x-agent-id", "unknown"),
                )

        # M58: record request for per-agent thrashing detection
        agent_id = request.headers.get("x-agent-id", "")
        if self._thrashing_detector and agent_id:
            self._thrashing_detector.record_request(agent_id, model)
            verdict = self._thrashing_detector.check(agent_id)
            if verdict.level == "halt":
                return JSONResponse(
                    {
                        "error": "Pipeline suspended — swap thrashing detected",
                        "swap_ratio": round(verdict.swap_ratio, 2),
                        "window_size": verdict.window_size,
                        "estimated_overhead_seconds": round(verdict.estimated_penalty_seconds, 1),
                        "cooloff_seconds": self.config.thrashing_detection.cooloff_seconds,
                        "suggestion": (
                            "Reorganize calls to batch by model. "
                            "Current pattern causes ~14s GPU penalty per swap."
                        ),
                    },
                    status_code=429,
                )
            if verdict.level == "warn":
                routing_meta = routing_meta or {}
                routing_meta["_thrashing_warn"] = (
                    f"swap_ratio={verdict.swap_ratio:.2f}; "
                    f"estimated_overhead_seconds={verdict.estimated_penalty_seconds:.0f}; "
                    'suggestion="batch requests by model to reduce swap penalties"'
                )
                audit.emit(audit.EVENT_THRASHING, {
                    "agent_id": agent_id,
                    "verdict": "warn",
                    "swap_ratio": round(verdict.swap_ratio, 2),
                    "window_size": verdict.window_size,
                    "estimated_penalty_seconds": round(verdict.estimated_penalty_seconds, 1),
                })

        # Inject safety overrides
        options = payload.get("options", {})
        if self.config.request_overrides.use_mmap is False and "use_mmap" not in options:
            options["use_mmap"] = False

        # Inject default num_ctx if client didn't set one
        if "num_ctx" not in options:
            model_info = self.config.models.get(model)
            if model_info and model_info.default_num_ctx:
                options["num_ctx"] = model_info.default_num_ctx
            elif self.config.request_overrides.default_num_ctx is not None:
                options["num_ctx"] = self.config.request_overrides.default_num_ctx

        if options:
            payload["options"] = options

        # Detect priority tier
        tier = self._detect_priority(request)
        base_priority = tier.base_priority(self.config.priorities)

        logger.info(
            "→ %s model=%s stream=%s priority=%s client=%s",
            path, model, is_streaming, tier.value,
            request.headers.get("user-agent", "unknown")[:50],
        )

        # Re-encode the modified payload
        modified_body = json.dumps(payload).encode()

        # If scheduler is active, enqueue and await grant
        done_fn: Callable[[], None] | None = None
        queue_wait_seconds = 0.0

        if self._enqueue_fn is not None:
            queued = QueuedRequest(
                model=model,
                endpoint=path,
                body=modified_body,
                priority=base_priority,
                base_priority=base_priority,
                tier=tier,
                client_info=request.headers.get("user-agent", "unknown")[:80],
            )

            try:
                grant_event, done_fn, cancel_fn = await self._enqueue_fn(queued)
            except RuntimeError as exc:
                if "Draining" in str(exc):
                    logger.warning("Drain mode — rejecting request for model '%s'", model)
                    return JSONResponse(
                        {"error": "Broker is draining — try again later"},
                        status_code=503,
                    )
                logger.error("Queue full — rejecting request for model '%s'", model)
                return JSONResponse(
                    {"error": "Broker queue full — try again later"},
                    status_code=503,
                )
            except Exception:
                logger.error("Queue full — rejecting request for model '%s'", model)
                return JSONResponse(
                    {"error": "Broker queue full — try again later"},
                    status_code=503,
                )

            # Wait for scheduler to grant this request (model loaded and ready)
            try:
                await asyncio.wait_for(grant_event.wait(), timeout=self._queue_timeout)
            except TimeoutError:
                cancel_fn()  # Remove from queue + pending grants + pending completions
                logger.warning(
                    "Request %s timed out in queue after %.0fs",
                    queued.id, self._queue_timeout,
                )
                return JSONResponse(
                    {"error": "Request timed out waiting in scheduler queue"},
                    status_code=504,
                )

            logger.debug("Request %s granted by scheduler", queued.id)
            queue_wait_seconds = queued.age_seconds

        # Check circuit breaker before forwarding
        if self.circuit_breaker and self.circuit_breaker.state == "open":
            if done_fn:
                done_fn()
            return JSONResponse(
                {"error": "Ollama backend unavailable (circuit breaker open)"},
                status_code=503,
            )

        # Forward to Ollama
        target_url = f"{self._backend_url}{path}"
        dispatch_start = time.time()

        try:
            if is_streaming:
                # Pass done_fn into generator so it signals completion after last byte
                result = await self._stream_response(
                    request, target_url, modified_body, model, path, tier,
                    done_fn=done_fn, routing_meta=routing_meta,
                )
                done_fn = None  # Generator owns done_fn now; prevent double-call in finally
            else:
                result = await self._forward_response(
                    request, target_url, modified_body, model, path, tier,
                    routing_meta=routing_meta,
                )
        finally:
            # Non-streaming: call done_fn here (after _forward_response returns).
            # Streaming: done_fn was handed to the generator; only call here if
            # _stream_response itself raised before the generator took ownership.
            if done_fn:
                done_fn()

        # Audit: request complete event (for scheduled endpoints)
        dispatch_duration = time.time() - dispatch_start
        audit_details: dict[str, Any] = {
            "model": model,
            "endpoint": path,
            "tier": tier.value,
            "queue_wait_seconds": round(queue_wait_seconds, 3),
            "dispatch_duration_seconds": round(dispatch_duration, 3),
            "streaming": is_streaming,
        }
        if agent_id:
            audit_details["agent_id"] = agent_id
        if task_complexity:
            audit_details["task_complexity"] = task_complexity
        if routing_meta:
            audit_details["model_requested"] = routing_meta["requested"]
            audit_details["model_routed"] = routing_meta["routed"]
            audit_details["routing_applied"] = True
        else:
            audit_details["routing_applied"] = False
        audit.emit(audit.EVENT_REQUEST_COMPLETE, audit_details)

        # Record for /broker/recent (S5: Dashboard Evolution)
        if self._record_fn is not None:
            self._record_fn(
                model=model,
                endpoint=path,
                tier=tier.value,
                queue_wait_s=queue_wait_seconds,
                duration_s=dispatch_duration,
                status_code=200,
            )

        return result

    # Endpoints that stream NDJSON progress (model pull/push/create).
    # These must be proxied with streaming, not buffered.
    _STREAMING_PASSTHROUGH = {"/api/pull", "/api/push", "/api/create"}

    async def _handle_passthrough(
        self, request: Request, path: str, body: bytes,
    ) -> StreamingResponse | JSONResponse:
        """Forward request to Ollama without scheduling."""
        # Drain mode: reject anything not in the operator-configured management
        # set.  This catches inference-adjacent endpoints that fall through to
        # passthrough by default (e.g., /api/embeddings plural), while keeping
        # /api/tags, /api/ps, /api/show etc. available so operators can still
        # observe state during a drain.
        if (
            self._is_draining_fn is not None
            and self._is_draining_fn()
            and path not in self._passthrough_endpoints
        ):
            return JSONResponse(
                {"error": "Broker is draining - try again later"},
                status_code=503,
            )

        target_url = f"{self._backend_url}{path}"
        method = request.method.upper()

        # Streaming passthrough for long-running NDJSON endpoints
        if path in self._STREAMING_PASSTHROUGH and method == "POST":
            return await self._stream_passthrough(request, target_url, body)

        try:
            headers = self._forward_headers(request)
            resp = await self._http.request(
                method, target_url, content=body, headers=headers,
            )
            self._requests_served += 1

            # Cache /api/tags response for graceful degradation
            if path == "/api/tags" and resp.status_code == 200 and self.circuit_breaker:
                with contextlib.suppress(Exception):
                    self.circuit_breaker.set_cached_tags(resp.json())

            # Record success for circuit breaker
            if self.circuit_breaker:
                await self.circuit_breaker.record_success()

            return JSONResponse(
                content=(
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {"raw": resp.text}
                ),
                status_code=resp.status_code,
            )
        except Exception as e:
            logger.error("Passthrough to %s failed: %s", target_url, e)
            # Record failure for circuit breaker
            if self.circuit_breaker:
                await self.circuit_breaker.record_failure()

            # Graceful degradation: serve cached /api/tags when Ollama is down
            if path == "/api/tags" and self.circuit_breaker:
                cached = self.circuit_breaker.get_cached_tags()
                if cached:
                    logger.info("Serving cached /api/tags response (Ollama unavailable)")
                    return JSONResponse(content=cached, status_code=200)

            return JSONResponse({"error": f"Ollama backend unavailable: {e}"}, status_code=502)

    async def _stream_passthrough(
        self, request: Request, url: str, body: bytes,
    ) -> StreamingResponse | JSONResponse:
        """Stream a passthrough response (for /api/pull, /api/push, /api/create).

        These endpoints return NDJSON progress updates over long-running
        operations (model downloads can be tens of GB). Buffering the full
        response would make the client appear frozen.
        """
        headers = self._forward_headers(request)
        cb = self.circuit_breaker

        async def generate():
            try:
                async with self._http.stream(
                    "POST", url, content=body, headers=headers,
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                if cb:
                    await cb.record_success()
            except Exception as e:
                logger.error("Streaming passthrough error: %s", e)
                if cb:
                    await cb.record_failure()
                error_json = json.dumps({"error": str(e)}).encode() + b"\n"
                yield error_json
            finally:
                self._requests_served += 1

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
        )

    async def _stream_response(
        self, request: Request, url: str, body: bytes,
        model: str = "", path: str = "", tier: PriorityTier = PriorityTier.AGENT,
        done_fn: Callable[[], None] | None = None,
        routing_meta: dict[str, str] | None = None,
    ) -> StreamingResponse:
        """Stream Ollama's NDJSON response back to the client.

        This is the most critical path — `ollama run` depends on streaming
        tokens in real time. Any buffering makes it appear frozen.

        done_fn is called in the generator's finally block so the scheduler
        is unblocked only after the last byte has been sent to the client,
        preventing concurrent Ollama access.
        """
        headers = self._forward_headers(request)

        cb = self.circuit_breaker

        async def generate():
            try:
                async with self._http.stream(
                    "POST", url, content=body, headers=headers,
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                if cb:
                    await cb.record_success()
            except Exception as e:
                logger.error("Streaming proxy error: %s", e)
                if cb:
                    await cb.record_failure()
                error_json = json.dumps({"error": str(e)}).encode() + b"\n"
                yield error_json
            finally:
                self._requests_served += 1
                if done_fn:
                    done_fn()  # Unblock scheduler — this request is done

        response_headers: dict[str, str] = {}
        if routing_meta:
            response_headers["X-Model-Requested"] = routing_meta["requested"]
            response_headers["X-Model-Routed"] = routing_meta["routed"]
            response_headers["X-Routing-Reason"] = routing_meta["reason"]
            if "_thrashing_warn" in routing_meta:
                response_headers["X-Swap-Penalty-Warning"] = routing_meta["_thrashing_warn"]

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers=response_headers,
        )

    async def _forward_response(
        self, request: Request, url: str, body: bytes,
        model: str = "", path: str = "", tier: PriorityTier = PriorityTier.AGENT,
        routing_meta: dict[str, str] | None = None,
    ) -> JSONResponse:
        """Forward a non-streaming request and return the full response."""
        headers = self._forward_headers(request)

        try:
            resp = await self._http.post(url, content=body, headers=headers)
            self._requests_served += 1
            if self.circuit_breaker:
                await self.circuit_breaker.record_success()

            resp_json = resp.json()
            response_headers: dict[str, str] = {}

            if routing_meta:
                response_headers["X-Model-Requested"] = routing_meta["requested"]
                response_headers["X-Model-Routed"] = routing_meta["routed"]
                response_headers["X-Routing-Reason"] = routing_meta["reason"]
                if "_thrashing_warn" in routing_meta:
                    response_headers["X-Swap-Penalty-Warning"] = routing_meta["_thrashing_warn"]

            # Token count headers from Ollama response
            prompt_tokens = resp_json.get("prompt_eval_count")
            completion_tokens = resp_json.get("eval_count")
            if prompt_tokens is not None:
                response_headers["X-Prompt-Tokens"] = str(prompt_tokens)
            if completion_tokens is not None:
                response_headers["X-Completion-Tokens"] = str(completion_tokens)

            return JSONResponse(
                content=resp_json,
                status_code=resp.status_code,
                headers=response_headers,
            )
        except Exception as e:
            logger.error("Forward proxy error to %s: %s: %s", url, type(e).__name__, repr(e))
            if self.circuit_breaker:
                await self.circuit_breaker.record_failure()
            return JSONResponse({"error": f"Ollama backend unavailable: {e}"}, status_code=502)

    def _detect_priority(self, request: Request) -> PriorityTier:
        """Detect request priority from headers, intents, and heuristics.

        Priority sources (highest to lowest precedence):
        1. X-Broker-Priority header (explicit)
        2. X-Broker-Intent header (intent-based scheduling)
        3. User-Agent heuristic (ollama CLI -> INTERACTIVE)
        4. Default (AGENT)
        """
        # Explicit header
        explicit = request.headers.get("x-broker-priority", "").lower()
        if explicit:
            try:
                return PriorityTier(explicit)
            except ValueError:
                pass

        # Intent-based priority: look up active intent by ID
        intent_id = request.headers.get("x-broker-intent", "")
        if intent_id and self._intent_lookup_fn:
            result = self._intent_lookup_fn(intent_id)
            if result is not None:
                tier, _ = result
                return tier

        # User-Agent heuristic: ollama CLI
        user_agent = request.headers.get("user-agent", "").lower()
        if "ollama" in user_agent:
            return PriorityTier.INTERACTIVE

        return PriorityTier.AGENT

    @staticmethod
    def _forward_headers(request: Request) -> dict:
        """Extract headers to forward, excluding hop-by-hop and content-length.

        Content-Length is excluded because the proxy may modify the body
        (e.g., injecting use_mmap:false), changing its size. httpx will
        compute the correct Content-Length from the actual body.
        """
        return {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
            and k.lower() not in ("host", "content-length")
        }

    @staticmethod
    def _extract_streaming_tokens(chunk: bytes) -> dict[str, int] | None:
        """Extract token counts from a streaming NDJSON final chunk.

        Ollama includes prompt_eval_count and eval_count in the last chunk
        where done=true. Returns None for non-final chunks.
        """
        try:
            data = json.loads(chunk)
            if data.get("done"):
                result: dict[str, int] = {}
                if "prompt_eval_count" in data:
                    result["prompt_tokens"] = data["prompt_eval_count"]
                if "eval_count" in data:
                    result["completion_tokens"] = data["eval_count"]
                return result if result else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return None

    async def close(self) -> None:
        await self._http.aclose()
