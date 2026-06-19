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

import logging
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


async def test_get_latency_returns_dict() -> None:
    payload = {"sample_total": 0, "per_model": [], "overall": None}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ) as get:
        assert await client.get_latency() == payload
        get.assert_awaited_once_with(
            "http://localhost:11434/broker/latency",
            params={"window_s": 300.0},
        )


async def test_get_latency_forwards_custom_window() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, {}))
    ) as get:
        await client.get_latency(window_s=60.0)
        get.assert_awaited_once_with(
            "http://localhost:11434/broker/latency",
            params={"window_s": 60.0},
        )


async def test_get_catalog_returns_dict() -> None:
    payload = {"models": [], "total": 0, "registry_source": "<unknown>"}
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ):
        assert await client.get_catalog() == payload


# ---------------------------------------------------------------------------
# Observability T6 — snapshot + contention fan-out (spec 5.6 BastionClient)
# ---------------------------------------------------------------------------


async def test_get_snapshot_returns_machine_snapshot_dict() -> None:
    """get_snapshot parses a MachineSnapshot-shaped payload (history=1)."""
    payload = {
        "snapshot_ts": 1234567890.0,
        "broker": None,
        "gpu": {"gpu_index": 0},
        "gpu_extended": None,
        "contention": None,
        "process": None,
        "inference": None,
        "correlation": None,
    }
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ) as get:
        assert await client.get_snapshot() == payload
        get.assert_awaited_once_with(
            "http://localhost:11434/broker/snapshot",
            params={"history": 1},
        )


async def test_get_snapshot_forwards_history() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, {}))
    ) as get:
        await client.get_snapshot(history=5)
        get.assert_awaited_once_with(
            "http://localhost:11434/broker/snapshot",
            params={"history": 5},
        )


async def test_get_contention_returns_dict() -> None:
    """get_contention parses a ContentionSnapshot-shaped payload."""
    payload = {
        "psi_cpu_some_avg10": 1.0,
        "swap_in_rate_mb_s": None,
        "block_devices": [
            {
                "device": "nvme0n1",
                "util_pct": 12.0,
                "read_await_ms": None,
                "write_await_ms": None,
                "read_rate_mb_s": 0.0,
                "write_rate_mb_s": 0.0,
            }
        ],
        "cpu_package_watts": 80.0,
        "oom_kill_total": 0,
        "sampled_at": 1.0,
    }
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(200, payload))
    ) as get:
        assert await client.get_contention() == payload
        get.assert_awaited_once_with(
            "http://localhost:11434/broker/contention", params=None
        )


async def test_get_snapshot_returns_default_on_error() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(500))
    ):
        assert await client.get_snapshot() == {}


async def test_get_contention_returns_default_on_error() -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    ):
        assert await client.get_contention() == {}


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
        ("get_latency", {}),
        ("get_catalog", {}),
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
        ("get_latency", {}),
        ("get_catalog", {}),
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


# ---------------------------------------------------------------------------
# GET error handling — failures must be logged at DEBUG, never silently
# dropped. The dashboard renders an empty panel either way; the log is the
# only place auth failures / 404s / network partitions become visible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name,endpoint",
    [
        ("get_recent", "/broker/recent"),
        ("get_queue", "/broker/queue"),
        ("get_health", "/broker/health"),
        ("get_vram_ledger", "/broker/vram"),
        ("get_watchdog", "/broker/watchdog"),
        ("get_counters", "/broker/counters"),
        ("get_thrashing", "/broker/thrashing"),
        ("get_latency", "/broker/latency"),
        ("get_catalog", "/broker/catalog"),
    ],
)
async def test_get_methods_log_network_error_at_debug(
    method_name: str, endpoint: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    ), caplog.at_level(logging.DEBUG, logger="bastion.dashboard.client"):
        await getattr(client, method_name)()

    messages = [r.getMessage() for r in caplog.records]
    assert any(endpoint in m and "ConnectError" in m for m in messages), (
        f"{method_name} swallowed ConnectError without logging "
        f"endpoint + exception type; got: {messages}"
    )


async def test_get_methods_log_http_status_error_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = BastionClient("http://localhost:11434")
    with patch.object(
        client._client, "get", new=AsyncMock(return_value=_resp(500))
    ), caplog.at_level(logging.DEBUG, logger="bastion.dashboard.client"):
        await client.get_health()

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "/broker/health" in m and "HTTPStatusError" in m for m in messages
    )


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
