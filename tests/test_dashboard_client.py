"""Tests for ``bastion.dashboard.client.BastionClient``.

Covers every public method of the async HTTP wrapper around BASTION's admin
API:

* Happy-path JSON return values for each endpoint.
* HTTP error handling (4xx, 5xx, network errors) — most "get_*" methods are
  expected to swallow errors and return an empty dict / list.
* ``post_*`` methods propagate errors via ``raise_for_status()``.
* Authorization header injection when ``api_key`` is supplied.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.dashboard.client import BastionClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status: int = 200, json_body: object | None = None) -> MagicMock:
    """Build a mock httpx.Response.

    ``raise_for_status`` raises an ``HTTPStatusError`` for non-2xx codes
    (mirroring real httpx behaviour) so the client error branches are
    exercised.
    """

    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body)
    if 200 <= status < 300:
        resp.raise_for_status = MagicMock(return_value=None)
    else:
        request = httpx.Request("GET", "http://test.local/")
        response = httpx.Response(status_code=status, request=request)
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"{status}", request=request, response=response
            )
        )
    return resp


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_init_strips_trailing_slash() -> None:
    client = BastionClient("http://localhost:11434/")
    assert client.base_url == "http://localhost:11434"


def test_init_without_api_key_has_no_auth_header() -> None:
    client = BastionClient("http://localhost:11434")
    assert "Authorization" not in client._client.headers


def test_init_with_api_key_injects_bearer_header() -> None:
    client = BastionClient("http://localhost:11434", api_key="s3cret")
    assert client._client.headers["Authorization"] == "Bearer s3cret"


async def test_close_calls_underlying_aclose() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(client._client, "aclose", new=AsyncMock()) as aclose:
        await client.close()
        aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Happy-path GET methods
# ---------------------------------------------------------------------------


async def test_poll_returns_parsed_json() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(return_value=_resp(200, {"state": "running"})),
    ) as get:
        data = await client.poll()
        assert data == {"state": "running"}
        get.assert_awaited_once_with("http://localhost:11434/broker/status")


async def test_get_recent_returns_list() -> None:
    payload = [{"agent_id": "a", "model": "m"}]
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_recent() == payload


async def test_get_queue_returns_dict() -> None:
    payload = {"depth": 3, "items": []}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_queue() == payload


async def test_get_health_returns_dict() -> None:
    payload = {"breaker": "closed"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_health() == payload


async def test_get_vram_ledger_returns_dict() -> None:
    payload = {"reserved_gb": 12.0}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_vram_ledger() == payload


async def test_get_watchdog_returns_dict() -> None:
    payload = {"ok": True}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_watchdog() == payload


async def test_get_counters_returns_dict() -> None:
    payload = {"total_dispatched": 99, "reset_epoch": 1.0}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_counters() == payload


async def test_get_thrashing_returns_dict() -> None:
    payload = {"agents": {}}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_thrashing() == payload


# ---------------------------------------------------------------------------
# GET error handling — methods must swallow errors and return safe defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name,default",
    [
        ("get_recent", []),
        ("get_queue", {}),
        ("get_health", {}),
        ("get_vram_ledger", {}),
        ("get_watchdog", {}),
        ("get_counters", {}),
        ("get_thrashing", {}),
    ],
)
async def test_get_methods_return_default_on_http_error(
    method_name: str, default: object
) -> None:
    """4xx / 5xx must not propagate from the safe GETs."""
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(500))
    ):
        result = await getattr(client, method_name)()
        assert result == default


@pytest.mark.parametrize(
    "method_name,default",
    [
        ("get_recent", []),
        ("get_queue", {}),
        ("get_health", {}),
        ("get_vram_ledger", {}),
        ("get_watchdog", {}),
        ("get_counters", {}),
        ("get_thrashing", {}),
    ],
)
async def test_get_methods_return_default_on_network_error(
    method_name: str, default: object
) -> None:
    """ConnectError / TimeoutException must be swallowed by safe GETs."""
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    ):
        result = await getattr(client, method_name)()
        assert result == default


async def test_get_methods_return_default_on_timeout() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
    ):
        assert await client.get_queue() == {}


# ``poll`` does NOT swallow errors — it propagates via raise_for_status.
async def test_poll_raises_on_http_error() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(500))
    ), pytest.raises(httpx.HTTPStatusError):
        await client.poll()


async def test_poll_propagates_network_error() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.ConnectError("nope")),
    ), pytest.raises(httpx.ConnectError):
        await client.poll()


# ---------------------------------------------------------------------------
# POST methods — happy path + error propagation
# ---------------------------------------------------------------------------


async def test_post_preload_sends_model_and_returns_json() -> None:
    payload = {"status": "loaded", "model": "qwen3:14b"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_resp(200, payload)),
    ) as post:
        result = await client.post_preload("qwen3:14b")
        assert result == payload
        post.assert_awaited_once_with(
            "http://localhost:11434/broker/preload",
            json={"model": "qwen3:14b"},
        )


async def test_post_unload_sends_model_and_returns_json() -> None:
    payload = {"status": "unloaded", "model": "llama3.1:8b"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_resp(200, payload)),
    ) as post:
        result = await client.post_unload("llama3.1:8b")
        assert result == payload
        post.assert_awaited_once_with(
            "http://localhost:11434/broker/unload",
            json={"model": "llama3.1:8b"},
        )


async def test_post_drain_returns_json() -> None:
    payload = {"status": "draining"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_resp(200, payload)),
    ) as post:
        result = await client.post_drain()
        assert result == payload
        post.assert_awaited_once_with("http://localhost:11434/broker/drain")


async def test_post_resume_returns_json() -> None:
    payload = {"status": "running"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_resp(200, payload)),
    ) as post:
        result = await client.post_resume()
        assert result == payload
        post.assert_awaited_once_with("http://localhost:11434/broker/resume")


@pytest.mark.parametrize(
    "method,args",
    [
        ("post_preload", ("qwen3:14b",)),
        ("post_unload", ("llama3.1:8b",)),
        ("post_drain", ()),
        ("post_resume", ()),
    ],
)
async def test_post_methods_raise_on_http_error(
    method: str, args: tuple[str, ...]
) -> None:
    """POSTs must propagate HTTP errors (caller handles in action handlers)."""
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(return_value=_resp(503)),
    ), pytest.raises(httpx.HTTPStatusError):
        await getattr(client, method)(*args)


async def test_post_preload_propagates_network_error() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "post",
        new=AsyncMock(side_effect=httpx.ConnectError("offline")),
    ), pytest.raises(httpx.ConnectError):
        await client.post_preload("x")
