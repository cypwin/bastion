"""Structured audit logging for BASTION events.

Writes JSON-lines format to a rotating log file for model swaps, VRAM alerts,
queue changes, and request completions. Each log line is a single JSON object
for easy parsing by jq, log aggregators, or post-incident analysis.

Phase D3 additions:
  - Tiered audit logging (tiers 1/2/3 controlled by config)
  - Dual-identity tracking (auth SHA-256 hash + A2A agent identity)
  - Content hashing helpers (SHA-256 of prompt/response, never raw tokens)
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Audit event types
EVENT_SWAP = "swap"
EVENT_VRAM_ALERT = "vram_alert"
EVENT_QUEUE_CHANGE = "queue_change"
EVENT_REQUEST_COMPLETE = "request_complete"


# ---------------------------------------------------------------------------
# Identity and content hashing helpers
# ---------------------------------------------------------------------------

def hash_identity(token: str) -> str:
    """Return SHA-256 hex digest of a bearer token.

    Never log raw tokens -- always hash them first.

    Parameters
    ----------
    token : str
        Raw bearer token string.

    Returns
    -------
    str
        SHA-256 hex digest (64 hex chars).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_content(text: str) -> str:
    """Return SHA-256 hex digest of content text.

    Used for audit Tier 2 to record a fingerprint of prompt/response
    without storing the actual content.

    Parameters
    ----------
    text : str
        Raw content string (prompt or response).

    Returns
    -------
    str
        SHA-256 hex digest (64 hex chars).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_audit_event(
    event_type: str,
    data: Dict[str, Any],
    tier: int = 2,
    auth_token: Optional[str] = None,
    a2a_identity: Optional[Dict[str, Any]] = None,
    source_ip: Optional[str] = None,
    prompt: Optional[str] = None,
    response: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a tiered audit event with dual-identity tracking.

    Parameters
    ----------
    event_type : str
        Event type (swap, vram_alert, queue_change, request_complete, etc.).
    data : dict
        Event-specific base fields (request_id, model_name, operation,
        token counts, latency, status, etc.).
    tier : int
        Audit tier (1, 2, or 3). Controls what fields are included.
    auth_token : str, optional
        Raw bearer token -- hashed to SHA-256 before inclusion (never stored raw).
    a2a_identity : dict, optional
        A2A agent identity: ``{"agent_name": ..., "skill_id": ...,
        "task_id": ..., "context_id": ...}``.
    source_ip : str, optional
        Client source IP (from X-Forwarded-For or direct connection).
    prompt : str, optional
        Raw prompt text. Included as hash at tier 2, raw at tier 3.
    response : str, optional
        Raw response text. Included as hash at tier 2, raw at tier 3.

    Returns
    -------
    dict
        Fully constructed audit event ready for JSON serialization.
    """
    entry: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "details": dict(data),  # shallow copy to avoid mutating caller's dict
    }

    # --- Identity layer (all tiers) ---
    if auth_token:
        entry["auth_identity_hash"] = hash_identity(auth_token)

    if a2a_identity:
        entry["a2a_identity"] = a2a_identity

    if source_ip:
        entry["source_ip"] = source_ip

    # --- Tier 2: content hashes ---
    if tier >= 2:
        if prompt is not None:
            entry["prompt_hash"] = hash_content(prompt)
        if response is not None:
            entry["response_hash"] = hash_content(response)

    # --- Tier 3: raw content (opt-in, debugging only) ---
    if tier >= 3:
        if prompt is not None:
            entry["prompt_text"] = prompt
        if response is not None:
            entry["response_text"] = response

    return entry


class AuditLogger:
    """Structured JSON-lines audit logger with rotation.

    Parameters
    ----------
    log_path : str
        Path to the audit log file (default: /tmp/bastion-audit.jsonl).
    max_bytes : int
        Maximum file size before rotation (default: 10MB).
    backup_count : int
        Number of backup files to keep (default: 5).
    tier : int
        Default audit tier (1, 2, or 3).
    """

    def __init__(
        self,
        log_path: str = "/tmp/bastion-audit.jsonl",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        tier: int = 2,
    ) -> None:
        self.logger = logging.getLogger("bastion.audit")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # Don't propagate to root logger
        self.tier = tier

        # Remove existing handlers to avoid duplicates
        self.logger.handlers.clear()

        # Ensure parent directory exists (survives /tmp cleanup on reboot)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        # Rotating file handler
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))  # Raw JSON only
        self.logger.addHandler(handler)

    def emit(self, event: str, details: Dict[str, Any]) -> None:
        """Emit an audit event as a JSON line.

        Parameters
        ----------
        event : str
            Event type (swap, vram_alert, queue_change, request_complete).
        details : dict
            Event-specific data to include in the log entry.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details,
        }
        self.logger.info(json.dumps(entry))

    def emit_tiered(
        self,
        event_type: str,
        data: Dict[str, Any],
        tier_override: Optional[int] = None,
        auth_token: Optional[str] = None,
        a2a_identity: Optional[Dict[str, Any]] = None,
        source_ip: Optional[str] = None,
        prompt: Optional[str] = None,
        response: Optional[str] = None,
    ) -> None:
        """Emit a tiered audit event with dual-identity tracking.

        Uses ``build_audit_event`` to construct the event based on the
        effective tier (``tier_override`` or the logger's configured tier).

        Parameters
        ----------
        event_type : str
            Event type string.
        data : dict
            Base event details (request_id, model, operation, etc.).
        tier_override : int, optional
            Override the configured tier for this event.
        auth_token : str, optional
            Raw bearer token (hashed before logging, never stored raw).
        a2a_identity : dict, optional
            A2A agent identity fields.
        source_ip : str, optional
            Client source IP.
        prompt : str, optional
            Prompt text (hashed at tier 2, raw at tier 3).
        response : str, optional
            Response text (hashed at tier 2, raw at tier 3).
        """
        effective_tier = tier_override if tier_override is not None else self.tier
        entry = build_audit_event(
            event_type=event_type,
            data=data,
            tier=effective_tier,
            auth_token=auth_token,
            a2a_identity=a2a_identity,
            source_ip=source_ip,
            prompt=prompt,
            response=response,
        )
        self.logger.info(json.dumps(entry))


# Global audit logger instance
_audit_logger: AuditLogger | None = None


def init_audit_logger(
    log_path: str = "/tmp/bastion-audit.jsonl",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    tier: int = 2,
) -> None:
    """Initialize the global audit logger.

    Call this once at application startup (e.g., in server.py).

    Parameters
    ----------
    log_path : str
        Path to the audit log file.
    max_bytes : int
        Maximum file size before rotation (default: 10MB).
    backup_count : int
        Number of backup files to keep (default: 5).
    tier : int
        Default audit tier (1, 2, or 3).
    """
    global _audit_logger
    _audit_logger = AuditLogger(log_path, max_bytes, backup_count, tier=tier)


def emit(event: str, details: Dict[str, Any]) -> None:
    """Emit an audit event (convenience wrapper).

    Backward compatible -- delegates to AuditLogger.emit() which
    writes the simple timestamp+event+details format.

    Parameters
    ----------
    event : str
        Event type (swap, vram_alert, queue_change, request_complete).
    details : dict
        Event-specific data.
    """
    if _audit_logger is not None:
        _audit_logger.emit(event, details)


def emit_tiered(
    event_type: str,
    data: Dict[str, Any],
    tier_override: Optional[int] = None,
    auth_token: Optional[str] = None,
    a2a_identity: Optional[Dict[str, Any]] = None,
    source_ip: Optional[str] = None,
    prompt: Optional[str] = None,
    response: Optional[str] = None,
) -> None:
    """Emit a tiered audit event (convenience wrapper).

    No-op if the global audit logger has not been initialized.

    Parameters
    ----------
    event_type : str
        Event type string.
    data : dict
        Base event details.
    tier_override : int, optional
        Override the configured tier for this event.
    auth_token : str, optional
        Raw bearer token (hashed, never stored raw).
    a2a_identity : dict, optional
        A2A agent identity fields.
    source_ip : str, optional
        Client source IP.
    prompt : str, optional
        Prompt text (hashed at tier 2, raw at tier 3).
    response : str, optional
        Response text (hashed at tier 2, raw at tier 3).
    """
    if _audit_logger is not None:
        _audit_logger.emit_tiered(
            event_type=event_type,
            data=data,
            tier_override=tier_override,
            auth_token=auth_token,
            a2a_identity=a2a_identity,
            source_ip=source_ip,
            prompt=prompt,
            response=response,
        )
