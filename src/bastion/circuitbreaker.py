"""Circuit breaker for Ollama backend calls.

Three-state breaker (CLOSED -> OPEN -> HALF_OPEN) that protects BASTION
from cascading failures when the Ollama backend is unhealthy.  When open,
requests fast-fail with 503 instead of piling up on a dead backend.

State machine::

    CLOSED --(N consecutive failures)--> OPEN --(recovery_timeout)--> HALF_OPEN
       ^                                                                  |
       +--------------(probe succeeds)------------------------------------+

    HALF_OPEN --(probe fails)--> OPEN (reset recovery timer)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import httpx

from bastion.models import CircuitBreakerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit breaker is open."""

    def __init__(self, recovery_remaining: float = 0.0) -> None:
        remaining = max(0.0, recovery_remaining)
        super().__init__(
            f"Circuit breaker is OPEN. Retry after {remaining:.1f}s."
        )
        self.recovery_remaining = remaining


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

class _State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Three-state circuit breaker for async callables.

    Parameters
    ----------
    config : CircuitBreakerConfig
        Thresholds and timing knobs.
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self._state = _State.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()
        self._cached_tags: dict | None = None

    # -- public properties ---------------------------------------------------

    @property
    def state(self) -> str:
        """Current state as a plain string."""
        # If nominally OPEN, check whether the recovery timeout has elapsed
        # so callers see the effective state.
        if self._state is _State.OPEN and self._recovery_elapsed():
            return _State.HALF_OPEN.value
        return self._state.value

    # -- cached tags ---------------------------------------------------------

    def set_cached_tags(self, response: dict) -> None:
        """Store the last successful ``/api/tags`` response."""
        self._cached_tags = response

    def get_cached_tags(self) -> dict | None:
        """Return the cached ``/api/tags`` response, or *None*."""
        return self._cached_tags

    # -- recording outcomes --------------------------------------------------

    async def record_success(self) -> None:
        """Record a successful backend call."""
        async with self._lock:
            if self._state is _State.HALF_OPEN:
                logger.info("Circuit breaker probe succeeded -- closing circuit")
            if self._state is not _State.CLOSED:
                logger.info("Circuit breaker transitioning to CLOSED")
            self._state = _State.CLOSED
            self._consecutive_failures = 0

    async def record_failure(self) -> None:
        """Record a failed backend call."""
        async with self._lock:
            self._consecutive_failures += 1

            if self._state is _State.HALF_OPEN:
                # Probe failed -- reopen and reset timer
                logger.warning("Circuit breaker probe failed -- reopening circuit")
                self._state = _State.OPEN
                self._opened_at = time.monotonic()
                return

            if (
                self._state is _State.CLOSED
                and self._consecutive_failures >= self._config.failure_threshold
            ):
                logger.warning(
                    "Circuit breaker tripped after %d consecutive failures",
                    self._consecutive_failures,
                )
                self._state = _State.OPEN
                self._opened_at = time.monotonic()

    # -- call wrapper --------------------------------------------------------

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Invoke *func* through the circuit breaker.

        Parameters
        ----------
        func : Callable
            An async callable to protect.
        *args, **kwargs
            Forwarded to *func*.

        Returns
        -------
        Any
            The return value of *func*.

        Raises
        ------
        CircuitOpenError
            If the circuit is currently open (fast-fail).
        """
        if not self._config.enabled:
            return await func(*args, **kwargs)

        async with self._lock:
            effective = self._effective_state()

            if effective is _State.OPEN:
                remaining = self._recovery_remaining()
                raise CircuitOpenError(remaining)

            if effective is _State.HALF_OPEN:
                # Transition into half-open officially so only one probe runs
                self._state = _State.HALF_OPEN

        # CLOSED or HALF_OPEN -- let the call through
        try:
            result = await func(*args, **kwargs)
        except Exception:
            await self.record_failure()
            raise

        await self.record_success()
        return result

    # -- internals -----------------------------------------------------------

    def _recovery_elapsed(self) -> bool:
        return (time.monotonic() - self._opened_at) >= self._config.recovery_timeout

    def _recovery_remaining(self) -> float:
        return max(
            0.0,
            self._config.recovery_timeout - (time.monotonic() - self._opened_at),
        )

    def _effective_state(self) -> _State:
        """Return the logical state, auto-promoting OPEN -> HALF_OPEN."""
        if self._state is _State.OPEN and self._recovery_elapsed():
            return _State.HALF_OPEN
        return self._state


# ---------------------------------------------------------------------------
# Transport-level circuit breaker (Phase B2)
# ---------------------------------------------------------------------------


class OllamaBackendError(Exception):
    """Raised when Ollama returns a server error (5xx)."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Ollama backend error: HTTP {status_code}")


class CircuitBreakerTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that applies circuit breaker to all requests.

    Wraps an inner transport (default: ``httpx.AsyncHTTPTransport``) with the
    circuit breaker.  All outgoing Ollama requests automatically go through
    the breaker, regardless of which code path initiates them.

    Parameters
    ----------
    breaker : CircuitBreaker
        The circuit breaker instance to use.
    inner : httpx.AsyncBaseTransport, optional
        Inner transport to wrap.  Defaults to ``AsyncHTTPTransport``.
    """

    def __init__(
        self,
        breaker: CircuitBreaker,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._breaker = breaker
        self._transport = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Forward *request* through the circuit breaker.

        * OPEN  -> fast-fail with :class:`CircuitOpenError`.
        * CLOSED / HALF_OPEN -> forward to inner transport.
        * 5xx responses are recorded as failures and raise
          :class:`OllamaBackendError`.
        * ``httpx.ConnectError``, ``httpx.ConnectTimeout`` and
          ``httpx.ReadTimeout`` are recorded as failures and re-raised.
        * Streaming responses record success on connection establishment
          (the caller is responsible for post-stream outcome recording).
        """
        if not self._breaker._config.enabled:
            return await self._transport.handle_async_request(request)

        # Check circuit state before sending
        effective = self._breaker._effective_state()
        if effective is _State.OPEN:
            remaining = self._breaker._recovery_remaining()
            raise CircuitOpenError(remaining)

        try:
            response = await self._transport.handle_async_request(request)

            # For streaming responses we cannot inspect the full body here;
            # record success for the connection itself.  The caller (e.g.
            # A2AHandler) should record final outcome after consuming the
            # stream.
            if response.stream:
                await self._breaker.record_success()
            elif response.status_code >= 500:
                await self._breaker.record_failure()
                raise OllamaBackendError(response.status_code)
            else:
                await self._breaker.record_success()

            return response

        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            await self._breaker.record_failure()
            raise exc

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._transport.aclose()


# ---------------------------------------------------------------------------
# Bulkhead (concurrency limiter)
# ---------------------------------------------------------------------------


class BulkheadSemaphore:
    """Concurrency limiter for Ollama backend calls.

    Prevents overwhelming a recovering backend during half-open state.

    Parameters
    ----------
    max_concurrent : int
        Maximum concurrent Ollama calls.  Default 5.
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._active = 0

    async def __aenter__(self) -> BulkheadSemaphore:
        await self._semaphore.acquire()
        self._active += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._active -= 1
        self._semaphore.release()

    @property
    def active_count(self) -> int:
        """Number of currently in-flight calls."""
        return self._active

    @property
    def max_concurrent(self) -> int:
        """Configured concurrency limit."""
        return self._max
