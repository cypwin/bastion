"""Regression tests for BASTION dashboard secondary panels.

Covers TracePanel, A2ATaskPanel, LeasePanel, and AuditStreamPanel in
``bastion.dashboard.panels_secondary``. Each panel exposes a pure
``render_data`` method that returns a ``rich.table.Table``; the tests drive it
through a tiny Textual ``App`` harness (mirroring
``tests/test_dashboard_modals.py``) so the panel is composed in a real widget
tree before ``render_data`` is invoked.

The tests are intentionally narrow and behavioural:

  * compose / mount with empty payloads must not raise,
  * realistic happy-path payloads return a non-empty Table,
  * payloads carrying ``:``, ``.``, ``/`` and ``-`` characters in identifiers
    do not blow up via Textual's identifier rule (same gotcha that broke the
    [u] unload modal),
  * truncation paths are exercised so large payloads do not raise.
"""
from __future__ import annotations

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_secondary import (
    A2ATaskPanel,
    AuditStreamPanel,
    LeasePanel,
    TracePanel,
)

# ---------------------------------------------------------------------------
# Pilot harness — mounts a single panel and exposes it on ``app.panel``
# ---------------------------------------------------------------------------


class _PanelHarness(App[None]):
    """Mount one BastionPanel subclass so we can call ``render_data`` on it."""

    def __init__(self, panel_cls: type) -> None:
        super().__init__()
        self._panel_cls = panel_cls
        self.panel: object | None = None

    def compose(self) -> ComposeResult:
        self.panel = self._panel_cls(id="panel-under-test")
        yield self.panel  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TracePanel — input is a list[dict] of recent requests
# ---------------------------------------------------------------------------


async def test_trace_panel_composes_on_empty_list() -> None:
    """Empty ``recent`` must render a placeholder row without raising."""
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data([])
        assert isinstance(table, Table)
        assert table.row_count == 1  # the "(no requests)" placeholder


async def test_trace_panel_composes_on_typical_payload() -> None:
    """A well-formed list of recent requests renders one row per request."""
    recent = [
        {
            "timestamp": 1_700_000_000.0,
            "model": "llama3.2:3b",
            "tier": "fast",
            "queue_wait_s": 0.12,
            "duration_s": 1.34,
            "status_code": 200,
        },
        {
            "timestamp": 1_700_000_005.0,
            "model": "qwen2.5-coder:32b",
            "tier": "heavy",
            "queue_wait_s": 2.50,
            "duration_s": 12.50,
            "status_code": 500,
        },
    ]
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data(recent)
        assert isinstance(table, Table)
        assert table.row_count == 2


async def test_trace_panel_handles_partial_rows() -> None:
    """Missing optional fields default safely (no KeyError)."""
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data([{}])  # all fields missing -> defaults
        assert isinstance(table, Table)
        assert table.row_count == 1


async def test_trace_panel_source_column() -> None:
    """Source column shows the declared identity, '-' when absent, and
    truncates long names to the column width."""
    recent = [
        {"timestamp": 1.0, "model": "m", "tier": "agent",
         "queue_wait_s": 0.0, "duration_s": 1.0, "status_code": 200,
         "source": "cortex-digest"},
        {"timestamp": 2.0, "model": "m", "tier": "agent",
         "queue_wait_s": 0.0, "duration_s": 1.0, "status_code": 200,
         "source": "ollama"},
        {"timestamp": 3.0, "model": "m", "tier": "agent",
         "queue_wait_s": 0.0, "duration_s": 1.0, "status_code": 200},
    ]
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data(recent)
        cells = [str(c) for c in table.columns[2].cells]
        assert cells == ["cortex-di…", "ollama", "-"]


async def test_trace_panel_truncates_to_twenty() -> None:
    """At most 20 rows render even when the input contains many more."""
    recent = [
        {
            "timestamp": 1_700_000_000.0 + i,
            "model": f"model-{i}",
            "tier": "fast",
            "queue_wait_s": 0.1,
            "duration_s": 1.0,
            "status_code": 200,
        }
        for i in range(50)
    ]
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data(recent)
        assert table.row_count == 20


async def test_trace_panel_truncates_long_model_names() -> None:
    """Model names longer than 15 chars must be ellipsized (no overflow)."""
    recent = [
        {
            "timestamp": 1_700_000_000.0,
            "model": "very-long-model-name-that-overflows:latest",
            "tier": "fast",
            "queue_wait_s": 0.1,
            "duration_s": 1.0,
            "status_code": 200,
        }
    ]
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data(recent)  # must not raise
        assert table.row_count == 1


async def test_trace_panel_identifiers_with_special_chars() -> None:
    """``:``, ``.``, ``/``, ``-`` in model name must not break render_data."""
    recent = [
        {
            "timestamp": 1_700_000_000.0,
            "model": "vendor/model.v2:8b-instruct",
            "tier": "fast",
            "queue_wait_s": 0.1,
            "duration_s": 1.0,
            "status_code": 200,
        }
    ]
    app = _PanelHarness(TracePanel)
    async with app.run_test():
        panel = app.query_one(TracePanel)
        table = panel.render_data(recent)
        assert table.row_count == 1


# ---------------------------------------------------------------------------
# A2ATaskPanel — input is the status dict with ``a2a_tasks`` / ``a2a_summary``
# ---------------------------------------------------------------------------


async def test_a2a_panel_composes_on_empty_dict() -> None:
    """Empty status dict renders the ``(none)`` placeholder rows."""
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        # Two placeholder rows: "(none)" + "No active A2A tasks"
        assert table.row_count == 2


async def test_a2a_panel_renders_summary_counts() -> None:
    """Summary counts produce one row per non-zero state plus Total."""
    data = {
        "a2a_tasks": [],
        "a2a_summary": {
            "total": 12,
            "working": 3,
            "submitted": 2,
            "completed": 6,
            "failed": 1,
        },
    }
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data(data)
        # Total + working + submitted + completed + failed = 5 rows
        assert table.row_count == 5


async def test_a2a_panel_renders_each_task_state() -> None:
    """Tasks in all known states render without raising."""
    states = ["submitted", "working", "completed", "failed", "canceled"]
    tasks = [
        {"task_id": f"task-{i}", "state": st, "skill_id": f"skill-{i}"}
        for i, st in enumerate(states)
    ]
    data = {"a2a_tasks": tasks, "a2a_summary": {"total": len(tasks)}}
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data(data)
        # 1 Total row + 5 task rows (no working/submitted/etc. summary keys)
        assert table.row_count == 1 + len(tasks)


async def test_a2a_panel_truncates_to_five_tasks() -> None:
    """Only the first five tasks render even when 50+ are supplied."""
    tasks = [
        {"task_id": f"t{i:03d}", "state": "working", "skill_id": "embed"}
        for i in range(50)
    ]
    data = {"a2a_tasks": tasks, "a2a_summary": {"total": 50, "working": 50}}
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data(data)
        # Total + working summary + 5 task rows
        assert table.row_count == 2 + 5


async def test_a2a_panel_handles_missing_optional_fields() -> None:
    """Tasks with no ``task_id``/``skill_id`` still render via defaults."""
    data = {
        "a2a_tasks": [{"state": "working"}, {}],
        "a2a_summary": {"total": 2},
    }
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data(data)
        assert table.row_count == 1 + 2  # Total + 2 task rows


async def test_a2a_panel_identifiers_with_special_chars() -> None:
    """task_id / skill_id with ``:``, ``.``, ``/``, ``-`` do not raise."""
    data = {
        "a2a_tasks": [
            {
                "task_id": "task:abc.def/ghi-123",
                "state": "working",
                "skill_id": "embed.v2:latest",
            }
        ],
        "a2a_summary": {"total": 1, "working": 1},
    }
    app = _PanelHarness(A2ATaskPanel)
    async with app.run_test():
        panel = app.query_one(A2ATaskPanel)
        table = panel.render_data(data)
        assert table.row_count >= 2  # Total + working + task


# ---------------------------------------------------------------------------
# LeasePanel — input is the status dict, keyed by ``active_leases``
# ---------------------------------------------------------------------------


async def test_lease_panel_composes_on_empty_leases() -> None:
    """No ``active_leases`` -> ``(none)`` placeholder rows."""
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        assert table.row_count == 2  # "(none)" + "No active leases"


async def test_lease_panel_renders_multiple_leases() -> None:
    """Multiple active leases each get a row plus the Active header."""
    leases = [
        {
            "lease_id": f"lease-{i}",
            "model": f"llama3.2:{i}b",
            "remaining_requests": 100 - i,
            "state": "active",
            "ttl_remaining": 30.0 + i,
        }
        for i in range(3)
    ]
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({"active_leases": leases})
        # 1 Active row + 3 lease rows
        assert table.row_count == 1 + 3


async def test_lease_panel_truncates_to_five() -> None:
    """At most five lease rows render."""
    leases = [
        {
            "lease_id": f"l-{i:03d}",
            "model": "qwen2.5:7b",
            "remaining_requests": 10,
            "state": "active",
            "ttl_remaining": 60.0,
        }
        for i in range(15)
    ]
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({"active_leases": leases})
        assert table.row_count == 1 + 5  # Active header + 5 leases


async def test_lease_panel_handles_expired_ttl() -> None:
    """``ttl_remaining`` <= 0 must skip the TTL chip and not crash."""
    leases = [
        {
            "lease_id": "expired",
            "model": "llama3.2:3b",
            "remaining_requests": 0,
            "state": "expired",
            "ttl_remaining": 0.0,
        }
    ]
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({"active_leases": leases})
        assert table.row_count == 2


async def test_lease_panel_handles_missing_fields() -> None:
    """All-defaults lease entry must still render."""
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({"active_leases": [{}]})
        assert table.row_count == 2  # Active header + 1 row


async def test_lease_panel_identifiers_with_special_chars() -> None:
    """lease_id and model with awkward chars compose successfully."""
    leases = [
        {
            "lease_id": "uuid:abc.def-ghi/123",
            "model": "vendor/model.v2:8b-instruct",
            "remaining_requests": 5,
            "state": "active",
            "ttl_remaining": 45.0,
        }
    ]
    app = _PanelHarness(LeasePanel)
    async with app.run_test():
        panel = app.query_one(LeasePanel)
        table = panel.render_data({"active_leases": leases})
        assert table.row_count == 2


# ---------------------------------------------------------------------------
# AuditStreamPanel — input is list[dict] of audit events
# ---------------------------------------------------------------------------


async def test_audit_panel_composes_on_empty_list() -> None:
    """Empty events list renders the placeholder row."""
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data([])
        assert isinstance(table, Table)
        assert table.row_count == 1


async def test_audit_panel_renders_typical_events() -> None:
    """A handful of events render one row each."""
    events = [
        {
            "timestamp": "2026-05-19T12:00:00Z",
            "event": "swap",
            "details": {"model": "llama3.2:3b"},
        },
        {
            "timestamp": "2026-05-19T12:00:05Z",
            "event": "vram_alert",
            "details": {"severity": "high", "vram_used_gb": 23.5},
        },
        {
            "timestamp": "2026-05-19T12:00:10Z",
            "event": "request_complete",
            "details": {"status_code": 200, "model": "qwen2.5:7b"},
        },
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 3


async def test_audit_panel_assorted_event_types() -> None:
    """Each known event_type branch (vram_alert/swap/request_complete/other) is reached."""
    events = [
        {"timestamp": "2026-05-19T12:00:00Z", "event": "vram_alert", "details": {}},
        {"timestamp": "2026-05-19T12:00:01Z", "event": "swap", "details": {}},
        {
            "timestamp": "2026-05-19T12:00:02Z",
            "event": "request_complete",
            "details": {},
        },
        {"timestamp": "2026-05-19T12:00:03Z", "event": "preload", "details": {}},
        {"timestamp": "2026-05-19T12:00:04Z", "event": "unload", "details": {}},
        {"timestamp": "2026-05-19T12:00:05Z", "event": "drain", "details": {}},
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == len(events)


async def test_audit_panel_truncates_to_ten() -> None:
    """At most 10 audit rows render even when many more are supplied."""
    events = [
        {
            "timestamp": f"2026-05-19T12:00:{i:02d}Z",
            "event": "swap",
            "details": {"model": f"m{i}"},
        }
        for i in range(50)
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 10


async def test_audit_panel_long_message_is_truncated() -> None:
    """Long ``model`` field in details is truncated to 15 chars (no overflow)."""
    long_name = "x" * 500
    events = [
        {
            "timestamp": "2026-05-19T12:00:00Z",
            "event": "swap",
            "details": {"model": long_name},
        }
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 1


async def test_audit_panel_malformed_timestamp_falls_back() -> None:
    """Non-ISO ``timestamp`` strings and non-string values do not raise.

    Includes an ISO-shaped but semantically invalid string that triggers the
    ``ValueError`` fallback inside the panel's timestamp parser.
    """
    events = [
        {"timestamp": "garbage", "event": "swap", "details": {}},
        {"timestamp": 1_700_000_000, "event": "swap", "details": {}},
        {"timestamp": None, "event": "swap", "details": {}},
        # ISO-shaped but invalid (month 13) -> hits ValueError fallback
        {"timestamp": "2026-13-99T99:99:99Z", "event": "swap", "details": {}},
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 4


async def test_audit_panel_handles_missing_details() -> None:
    """Events whose ``details`` is missing or non-dict still render."""
    events = [
        {"timestamp": "2026-05-19T12:00:00Z", "event": "swap"},
        {"timestamp": "2026-05-19T12:00:01Z", "event": "swap", "details": "string-form"},
        {"timestamp": "2026-05-19T12:00:02Z", "event": "swap", "details": None},
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 3


async def test_audit_panel_identifiers_with_special_chars() -> None:
    """Model identifiers with ``:`` / ``.`` / ``-`` in details do not crash."""
    events = [
        {
            "timestamp": "2026-05-19T12:00:00Z",
            "event": "swap",
            "details": {
                "model": "vendor/model.v2:8b-instruct",
                "severity": "info",
            },
        }
    ]
    app = _PanelHarness(AuditStreamPanel)
    async with app.run_test():
        panel = app.query_one(AuditStreamPanel)
        table = panel.render_data(events)
        assert table.row_count == 1


# ---------------------------------------------------------------------------
# Combined sanity check — every panel instantiates without depending on data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "panel_cls",
    [TracePanel, A2ATaskPanel, LeasePanel, AuditStreamPanel],
)
async def test_panel_mounts_clean(panel_cls: type) -> None:
    """Each secondary panel must mount in a Textual app without raising."""
    app = _PanelHarness(panel_cls)
    async with app.run_test():
        panel = app.query_one(panel_cls)
        assert panel is not None
