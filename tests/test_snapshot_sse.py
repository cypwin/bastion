"""HTTP-contract tests for ``GET /broker/snapshot/stream`` (observability T2-sse).

The SSE snapshot stream (spec 5.6) supersedes the older 2026-03-13
``/broker/status/stream``. It is a FastAPI ``StreamingResponse`` with
``media_type='text/event-stream'`` that pushes the latest ``MachineSnapshot``
periodically. This file verifies:

  - with SSE **enabled** the stream yields at least one well-formed SSE
    snapshot event (``data: {json}\\n\\n`` whose JSON validates as a
    ``MachineSnapshot``);
  - with SSE **disabled** by config the route returns **501**;
  - the **9th** concurrent client (cap is 8) gets **503**;
  - the route is registered in **both** ``create_app`` and
    ``create_admin_app`` (spec 4.10);
  - exactly **one** ``_sse_wrapper`` definition remains in ``server.py``
    (the duplicated nested helpers were deduped into a shared one).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path

import bastion.server as server_mod
from bastion.models import BrokerConfig, MachineSnapshot
from bastion.server import create_admin_app, create_app


class _FakeRequest:
    """Minimal Request stand-in for driving the SSE handler directly.

    Avoids httpx ASGITransport (which buffers an unbounded StreamingResponse to
    completion → would hang on an infinite generator). ``is_disconnected``
    flips True after ``disconnect_after`` polls so the generator terminates.
    """

    def __init__(self, disconnect_after: int = 1) -> None:
        self.query_params: dict[str, str] = {}
        self._polls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._polls += 1
        return self._polls > self._disconnect_after


async def _drain_first_frame(resp) -> str:
    """Return the first complete SSE frame from a StreamingResponse, then stop."""
    buf = ""
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        buf += chunk
        if "\n\n" in buf:
            return buf.split("\n\n", 1)[0] + "\n\n"
    return buf

# ─────────────────────────────────────────────────────────────────────────────
# Dual-factory route registration (spec 4.10)
# ─────────────────────────────────────────────────────────────────────────────


def _route_paths(app) -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


class TestStreamRoutePresentInBothFactories:
    """/broker/snapshot/stream MUST be registered in both apps (spec 4.10)."""

    def test_route_present_in_create_app(self, test_config: BrokerConfig) -> None:
        app = create_app(test_config)
        assert "/broker/snapshot/stream" in _route_paths(app)

    def test_route_present_in_create_admin_app(
        self, test_config: BrokerConfig
    ) -> None:
        app = create_admin_app(test_config)
        assert "/broker/snapshot/stream" in _route_paths(app)


# ─────────────────────────────────────────────────────────────────────────────
# _sse_wrapper dedupe — exactly one definition remains
# ─────────────────────────────────────────────────────────────────────────────


class TestSseWrapperDeduped:
    """The pre-existing duplicated _sse_wrapper is now a single shared helper."""

    def test_exactly_one_sse_wrapper_definition(self) -> None:
        source = Path(inspect.getfile(server_mod)).read_text()
        tree = ast.parse(source)
        names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_sse_wrapper"
        ]
        assert names == ["_sse_wrapper"], (
            f"expected exactly one _sse_wrapper definition, found {len(names)}"
        )

    def test_no_admin_suffixed_duplicate(self) -> None:
        # The old copy was named _sse_wrapper_admin in create_admin_app.
        source = Path(inspect.getfile(server_mod)).read_text()
        assert "_sse_wrapper_admin" not in source


# ─────────────────────────────────────────────────────────────────────────────
# Enabled → stream yields ≥1 well-formed SSE snapshot event
# ─────────────────────────────────────────────────────────────────────────────


async def _open_and_first_frame(content_type_out: list[str]) -> str:
    """Call the handler with a fake request and return its first SSE frame."""
    req = _FakeRequest(disconnect_after=2)
    resp = await server_mod._handle_snapshot_stream(req)
    content_type_out.append(resp.media_type)
    return await _drain_first_frame(resp)


class TestStreamEnabledYieldsEvent:
    """With SSE enabled the stream emits a well-formed MachineSnapshot event."""

    def test_yields_one_wellformed_snapshot_event(
        self, app_with_stub_scheduler
    ) -> None:
        # app_with_stub_scheduler holds create_app's lifespan open so the
        # snapshot deque + module globals are live and SSE defaults to enabled.
        assert server_mod._config.observability.snapshot_stream_enabled is True
        media: list[str] = []
        frame = asyncio.run(_open_and_first_frame(media))
        assert media == ["text/event-stream"]
        # Well-formed SSE data event.
        assert frame.startswith("data: "), f"not an SSE data frame: {frame!r}"
        assert frame.endswith("\n\n")
        payload = frame[len("data: ") :].rstrip("\n")
        body = json.loads(payload)
        # The payload is a real MachineSnapshot (round-trips through the model).
        snap = MachineSnapshot.model_validate(body)
        assert snap.snapshot_ts > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Disabled → 501
# ─────────────────────────────────────────────────────────────────────────────


class TestStreamDisabledReturns501:
    """When observability.snapshot_stream_enabled is False the route 501s."""

    def test_disabled_returns_501(self, app_with_stub_scheduler) -> None:
        cfg = server_mod._config
        orig = cfg.observability.snapshot_stream_enabled
        cfg.observability.snapshot_stream_enabled = False
        try:
            resp = app_with_stub_scheduler.get("/broker/snapshot/stream")
            assert resp.status_code == 501
        finally:
            cfg.observability.snapshot_stream_enabled = orig


# ─────────────────────────────────────────────────────────────────────────────
# 9th concurrent client → 503 (cap is 8)
# ─────────────────────────────────────────────────────────────────────────────


class TestStreamConcurrencyCap:
    """The cap is 8 concurrent stream clients; the 9th gets 503."""

    def test_ninth_client_returns_503(self, app_with_stub_scheduler) -> None:
        # Saturate the cap deterministically by pinning the live client counter
        # to 8 (rather than opening 8 real long-lived connections).
        assert server_mod._SNAPSHOT_STREAM_MAX_CLIENTS == 8
        orig = server_mod._snapshot_stream_clients
        server_mod._snapshot_stream_clients = 8
        try:
            resp = app_with_stub_scheduler.get("/broker/snapshot/stream")
            assert resp.status_code == 503
        finally:
            server_mod._snapshot_stream_clients = orig

    def test_counter_released_after_stream_closes(
        self, app_with_stub_scheduler
    ) -> None:
        # A normal stream open + full drain must leave the counter at its start:
        # the slot is acquired in the handler and released in the generator's
        # finally when the (fake) client disconnects and the generator ends.
        start = server_mod._snapshot_stream_clients

        async def _open_drain_close() -> None:
            req = _FakeRequest(disconnect_after=1)
            resp = await server_mod._handle_snapshot_stream(req)
            # Acquired: counter is bumped while the stream is live.
            assert server_mod._snapshot_stream_clients == start + 1
            # Fully consume the generator so its finally runs.
            async for _ in resp.body_iterator:
                pass

        asyncio.run(_open_drain_close())
        assert server_mod._snapshot_stream_clients == start
