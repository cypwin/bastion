"""Regression: proxy enqueue handler must distinguish queue-full (RuntimeError)
from unexpected programming errors.

Per KNOWN_ISSUES.md (Important, resolved in v0.4.1):

    "Any unexpected exception from ``_enqueue_fn`` (programming error,
    attribute error, ...) is silently reported to the client as
    'Broker queue full.' The log message says 'Queue full' regardless
    of cause, with no traceback."

The fix narrows the bare ``except Exception`` to ``except RuntimeError``
(the queue-full / drain signal) and logs other exceptions at ERROR with
``exc_info=True`` so the actual type and traceback land in the broker log
instead of being papered over as "queue full."

For the client, the response distinguishes:

  * RuntimeError ("Queue full" / "Draining")  → 503 with the relevant body
  * Any other exception                       → 500 "Internal broker error"
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.models import BrokerConfig, QueuedRequest
from bastion.proxy import OllamaProxy


def _make_request(body: bytes = b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}'):
    req = MagicMock()
    req.url.path = "/api/generate"
    req.method = "POST"
    req.body = AsyncMock(return_value=body)
    req.headers = {"user-agent": "test-client/1.0"}
    return req


class TestProxyEnqueueNarrowExcept:
    @pytest.mark.asyncio
    async def test_runtime_error_queue_full_still_returns_503(self) -> None:
        """RuntimeError with 'Queue full' / non-draining message → 503."""

        async def failing_enqueue(queued: QueuedRequest):
            raise RuntimeError("Queue full")

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=failing_enqueue)
        resp = await proxy.handle_request(_make_request())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_runtime_error_draining_returns_503_with_drain_body(self) -> None:
        """RuntimeError containing 'Draining' → 503 with the drain-mode body."""
        import json

        async def draining_enqueue(queued: QueuedRequest):
            raise RuntimeError("Draining")

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=draining_enqueue)
        resp = await proxy.handle_request(_make_request())
        assert resp.status_code == 503
        body = json.loads(bytes(resp.body))
        assert "draining" in body.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_500_not_503(self) -> None:
        """A non-RuntimeError (e.g., AttributeError from a programming bug)
        must NOT masquerade as "queue full." Clients distinguish 503 (try
        again later — broker is healthy but busy) from 500 (broker bug;
        retrying won't help). Misrouting programming errors as 503 burns
        client backoff budgets and hides the real failure from operators.
        """
        async def buggy_enqueue(queued: QueuedRequest):
            raise AttributeError("'NoneType' object has no attribute 'submit'")

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=buggy_enqueue)
        resp = await proxy.handle_request(_make_request())
        assert resp.status_code == 500, (
            f"unexpected exception got {resp.status_code} — must be 500, "
            "not the misleading 503 'queue full'"
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_logged_with_exc_info(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The actual exception type + traceback must land in the broker log
        so operators can see what really failed instead of a "queue full" lie.
        """
        async def buggy_enqueue(queued: QueuedRequest):
            raise AttributeError("simulated programming bug")

        proxy = OllamaProxy(BrokerConfig(), enqueue_fn=buggy_enqueue)

        with caplog.at_level(logging.ERROR, logger="bastion.proxy"):
            await proxy.handle_request(_make_request())

        # Look for a log record with traceback info (exc_info) attached.
        matched = [
            r for r in caplog.records
            if r.exc_info is not None
            and r.exc_info[0] is AttributeError
        ]
        assert matched, (
            "expected an ERROR log record with AttributeError exc_info; "
            f"got {[(r.levelname, r.message, r.exc_info) for r in caplog.records]}"
        )
