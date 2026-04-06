"""Tests for audit.emit_tiered: tiered audit events with dual-identity tracking.

Covers D1:
  - Tiered audit event construction (tier 1, 2, 3)
  - Identity hashing (auth token, A2A identity, source IP)
  - Content hashing at tier 2, raw content at tier 3
  - emit_tiered convenience wrapper
  - Disk-full resilience
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bastion.audit
from bastion.audit import (
    AuditLogger,
    build_audit_event,
    emit_tiered,
    hash_content,
    hash_identity,
    init_audit_logger,
)

# ---------------------------------------------------------------------------
# Identity and content hashing
# ---------------------------------------------------------------------------


class TestHashHelpers:
    def test_hash_identity_returns_sha256(self) -> None:
        token = "test-bearer-token"
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert hash_identity(token) == expected

    def test_hash_identity_deterministic(self) -> None:
        assert hash_identity("token") == hash_identity("token")

    def test_hash_identity_different_for_different_tokens(self) -> None:
        assert hash_identity("token-a") != hash_identity("token-b")

    def test_hash_content_returns_sha256(self) -> None:
        text = "Hello world"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert hash_content(text) == expected


# ---------------------------------------------------------------------------
# build_audit_event tiers
# ---------------------------------------------------------------------------


class TestBuildAuditEvent:
    def test_tier1_basic_fields(self) -> None:
        event = build_audit_event("request_complete", {"model": "qwen3:14b"}, tier=1)
        assert event["event"] == "request_complete"
        assert event["details"]["model"] == "qwen3:14b"
        assert "timestamp" in event
        # Tier 1: no content hashes
        assert "prompt_hash" not in event
        assert "response_hash" not in event

    def test_tier2_includes_content_hashes(self) -> None:
        event = build_audit_event(
            "request_complete",
            {"model": "qwen3:14b"},
            tier=2,
            prompt="What is AI?",
            response="AI is...",
        )
        assert "prompt_hash" in event
        assert "response_hash" in event
        assert event["prompt_hash"] == hash_content("What is AI?")
        # Tier 2: no raw content
        assert "prompt_text" not in event
        assert "response_text" not in event

    def test_tier3_includes_raw_content(self) -> None:
        event = build_audit_event(
            "request_complete",
            {"model": "qwen3:14b"},
            tier=3,
            prompt="What is AI?",
            response="AI is...",
        )
        # Tier 3: both hashes and raw text
        assert event["prompt_hash"] == hash_content("What is AI?")
        assert event["prompt_text"] == "What is AI?"
        assert event["response_text"] == "AI is..."

    def test_auth_identity_hashed(self) -> None:
        event = build_audit_event(
            "request_complete",
            {},
            auth_token="secret-bearer-token",
        )
        assert "auth_identity_hash" in event
        assert event["auth_identity_hash"] == hash_identity("secret-bearer-token")

    def test_a2a_identity_included(self) -> None:
        identity = {"agent_name": "council-ai", "task_id": "t-001"}
        event = build_audit_event("request_complete", {}, a2a_identity=identity)
        assert event["a2a_identity"] == identity

    def test_source_ip_included(self) -> None:
        event = build_audit_event("request_complete", {}, source_ip="192.168.1.100")
        assert event["source_ip"] == "192.168.1.100"

    def test_no_optional_fields_when_not_provided(self) -> None:
        event = build_audit_event("swap", {"from": "a", "to": "b"})
        assert "auth_identity_hash" not in event
        assert "a2a_identity" not in event
        assert "source_ip" not in event
        assert "prompt_hash" not in event

    def test_details_not_mutated(self) -> None:
        original = {"model": "qwen3:14b"}
        build_audit_event("test", original, source_ip="1.2.3.4")
        # The original dict should not have been modified
        assert "source_ip" not in original


# ---------------------------------------------------------------------------
# AuditLogger.emit_tiered
# ---------------------------------------------------------------------------


class TestEmitTiered:
    def test_emit_tiered_writes_to_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "tiered-audit.jsonl"
        audit = AuditLogger(log_path=str(log_file), tier=2)
        audit.emit_tiered(
            "request_complete",
            {"model": "qwen3:14b"},
            prompt="Hello",
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "request_complete"
        assert "prompt_hash" in entry

    def test_emit_tiered_tier_override(self, tmp_path: Path) -> None:
        log_file = tmp_path / "tiered-audit.jsonl"
        audit = AuditLogger(log_path=str(log_file), tier=1)
        # Override tier to 3 for this specific event
        audit.emit_tiered(
            "request_complete",
            {"model": "qwen3:14b"},
            tier_override=3,
            prompt="Hello",
        )
        lines = log_file.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert "prompt_text" in entry  # Tier 3 includes raw text

    def test_emit_tiered_with_full_identity(self, tmp_path: Path) -> None:
        log_file = tmp_path / "tiered-audit.jsonl"
        audit = AuditLogger(log_path=str(log_file), tier=2)
        audit.emit_tiered(
            "request_complete",
            {"model": "qwen3:14b", "status": 200},
            auth_token="bearer-123",
            a2a_identity={"agent": "test"},
            source_ip="10.0.0.1",
            prompt="test prompt",
            response="test response",
        )
        lines = log_file.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert "auth_identity_hash" in entry
        assert "a2a_identity" in entry
        assert "source_ip" in entry
        assert "prompt_hash" in entry
        assert "response_hash" in entry


# ---------------------------------------------------------------------------
# Global emit_tiered convenience wrapper
# ---------------------------------------------------------------------------


class TestGlobalEmitTiered:
    def test_emit_tiered_noop_when_not_initialized(self) -> None:
        bastion.audit._audit_logger = None
        # Should not raise
        emit_tiered("test", {"key": "value"})

    def test_emit_tiered_works_after_init(self, tmp_path: Path) -> None:
        log_file = tmp_path / "global-tiered.jsonl"
        init_audit_logger(log_path=str(log_file), tier=2)
        emit_tiered(
            "request_complete",
            {"model": "qwen3:14b"},
            prompt="Hello",
        )
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert entry["event"] == "request_complete"


# ---------------------------------------------------------------------------
# Disk-full resilience (D3)
# ---------------------------------------------------------------------------


class TestDiskFullResilience:
    def test_emit_does_not_crash_on_write_error(self, tmp_path: Path) -> None:
        """If the log file becomes unwritable, emit should not crash BASTION."""
        log_file = tmp_path / "readonly-audit.jsonl"
        audit = AuditLogger(log_path=str(log_file))
        # First write to create the file
        audit.emit("test", {"data": "value"})
        assert log_file.exists()

        # Make the file read-only
        log_file.chmod(0o444)
        try:
            # This should log a warning but not raise
            audit.emit("test", {"data": "second"})
        finally:
            # Restore permissions for cleanup
            log_file.chmod(0o644)
