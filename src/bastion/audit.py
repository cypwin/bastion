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

import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Audit event types
EVENT_SWAP = "swap"
EVENT_VRAM_ALERT = "vram_alert"
EVENT_QUEUE_CHANGE = "queue_change"
EVENT_REQUEST_COMPLETE = "request_complete"
EVENT_THRASHING = "thrashing"
EVENT_SWAP_BRAKE = "swap_brake"  # swap-velocity brake engage/release (F4 fail-LOUD)


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
    data: dict[str, Any],
    tier: int = 2,
    auth_token: str | None = None,
    a2a_identity: dict[str, Any] | None = None,
    source_ip: str | None = None,
    prompt: str | None = None,
    response: str | None = None,
) -> dict[str, Any]:
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
    entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
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


# Ring buffer of recent audit events (newest last, capped at 50)
_recent_events: deque = deque(maxlen=50)

# Monotonic sequence number incremented on EVERY append to ``_recent_events``.
# It is a stable cursor for consumers (the correlation engine) that need an
# index which survives the bounded ring discarding its left end on overflow —
# a raw deque index drifts once the ring wraps, a sequence number does not.
# See design spec 2026-06-19 Section 6.2 (emitter A — public cursor API).
_event_seq: int = 0


def _append_event(entry: dict) -> None:
    """Append an audit event to the recent-events ring and bump the sequence.

    The single funnel for every ``_recent_events.append`` so ``_event_seq``
    stays strictly monotonic regardless of which emit path produced the event.
    """
    global _event_seq
    _recent_events.append(entry)
    _event_seq += 1


def recent_events(limit: int = 10) -> list[dict]:
    """Return the most recent audit events, newest last."""
    return list(_recent_events)[-limit:]


def get_events_since(cursor: int) -> tuple[list[dict], int]:
    """Return audit events appended since ``cursor`` plus the new cursor.

    ``cursor`` is a monotonic sequence number (see ``_event_seq``), **not** a
    deque index, so it is stable across ring wraps and external mutation of
    ``_recent_events``. The correlation engine stores its ``last_ingested_seq``
    and calls this each tick to pull only-new events (pull, never push).

    Because ``_recent_events`` is bounded, events whose sequence numbers fall
    below the oldest retained event have been discarded; this returns only the
    slice the ring still holds whose sequence number is greater than
    ``cursor`` — never a misleading duplicate and never an exception. The
    returned cursor is always the latest sequence number so the caller advances
    past discarded events rather than re-requesting them forever.

    Parameters
    ----------
    cursor : int
        The last sequence number the caller has already ingested (``0`` to
        start from the beginning of whatever the ring currently retains).

    Returns
    -------
    tuple[list[dict], int]
        ``(new_events, new_cursor)`` where ``new_events`` are the retained
        events with sequence number ``> cursor`` (oldest first) and
        ``new_cursor`` is the current ``_event_seq``.
    """
    latest = _event_seq
    if cursor >= latest:
        return [], latest
    # Sequence number of the oldest event still in the bounded ring: the most
    # recent ``len(_recent_events)`` appends occupy seqs (latest-N, latest].
    retained = list(_recent_events)
    oldest_retained_seq = latest - len(retained)  # seq of retained[0] is this+1
    # Number of events the caller is missing that we can still serve.
    skip = max(0, cursor - oldest_retained_seq)
    if skip >= len(retained):
        return [], latest
    return retained[skip:], latest


def _open_audit_handler(
    primary_path: str,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    """Open a RotatingFileHandler with writable-path fallbacks.

    Audit logs default to the XDG data dir but the root filesystem may be
    mounted read-only (e.g., immutable-base setups, error remounts).  Try the
    configured path first; on OSError fall back to ``$XDG_RUNTIME_DIR`` then
    ``/tmp``.  As a last resort return a NullHandler so emit() never crashes
    the caller, and log a clear error so the operator can see audit is off.
    """
    candidates = [primary_path]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(os.path.join(runtime, "bastion-audit.jsonl"))
    candidates.append("/tmp/bastion-audit.jsonl")

    audit_log = logging.getLogger("bastion.audit")
    for idx, candidate in enumerate(candidates):
        try:
            Path(candidate).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                candidate, maxBytes=max_bytes, backupCount=backup_count,
            )
        except OSError as exc:
            audit_log.warning(
                "Audit path %s unwritable (%s)%s",
                candidate, exc,
                "; trying fallback" if idx < len(candidates) - 1 else "",
            )
            continue
        with contextlib.suppress(OSError):
            os.chmod(candidate, 0o600)
        if idx > 0:
            audit_log.warning(
                "Using fallback audit path %s; audit is not durable across reboots",
                candidate,
            )
        return handler

    audit_log.error(
        "Audit disabled: no writable path among %s", candidates,
    )
    return logging.NullHandler()


class AuditLogger:
    """Structured JSON-lines audit logger with rotation.

    Parameters
    ----------
    log_path : str, optional
        Path to the audit log file.  Defaults to the XDG data directory
        (see :func:`bastion.paths.audit_log_path`).
    max_bytes : int
        Maximum file size before rotation (default: 10MB).
    backup_count : int
        Number of backup files to keep (default: 5).
    tier : int
        Default audit tier (1, 2, or 3).
    """

    def __init__(
        self,
        log_path: str | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        tier: int = 2,
    ) -> None:
        from bastion.paths import audit_log_path as _default_audit_path

        if log_path is None:
            log_path = _default_audit_path()
        self.logger = logging.getLogger("bastion.audit")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # Don't propagate to root logger
        self.tier = tier

        # Remove existing handlers to avoid duplicates
        self.logger.handlers.clear()

        handler = _open_audit_handler(log_path, max_bytes, backup_count)
        handler.setFormatter(logging.Formatter("%(message)s"))  # Raw JSON only
        self.logger.addHandler(handler)

    def emit(self, event: str, details: dict[str, Any]) -> None:
        """Emit an audit event as a JSON line.

        Parameters
        ----------
        event : str
            Event type (swap, vram_alert, queue_change, request_complete).
        details : dict
            Event-specific data to include in the log entry.
        """
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "details": details,
        }
        _append_event(entry)
        self.logger.info(json.dumps(entry))

    def emit_tiered(
        self,
        event_type: str,
        data: dict[str, Any],
        tier_override: int | None = None,
        auth_token: str | None = None,
        a2a_identity: dict[str, Any] | None = None,
        source_ip: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
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
        _append_event(entry)
        self.logger.info(json.dumps(entry))


# Global audit logger instance
_audit_logger: Any = None  # AuditLogger | PersistentAuditLog | None at runtime

# Ring buffer for events emitted before init_audit_logger() — flushed on
# init so startup-ordering bugs leave a trace instead of vanishing.
# Flushed events carry flush-time timestamps (startup window is sub-second).
_PREINIT_BUFFER_MAX = 256
_preinit_events: deque[tuple[str, dict[str, Any]]] = deque(maxlen=_PREINIT_BUFFER_MAX)

# Deliberately NOT logging.getLogger(__name__): "bastion.audit" is the JSONL
# audit logger's name (propagate=False, file handler) — a child logger would
# propagate warnings into the audit file itself.
_module_logger = logging.getLogger("bastion.audit_bootstrap")


def init_audit_logger(
    log_path: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    tier: int = 2,
) -> None:
    """Initialize the global audit logger.

    Call this once at application startup (e.g., in server.py).

    Parameters
    ----------
    log_path : str, optional
        Path to the audit log file.  Defaults to the XDG data directory
        (see :func:`bastion.paths.audit_log_path`).
    max_bytes : int
        Maximum file size before rotation (default: 10MB).
    backup_count : int
        Number of backup files to keep (default: 5).
    tier : int
        Default audit tier (1, 2, or 3).
    """
    global _audit_logger
    _audit_logger = AuditLogger(log_path, max_bytes, backup_count, tier=tier)
    if _preinit_events:
        flushed = len(_preinit_events)
        while _preinit_events:
            event, details = _preinit_events.popleft()
            _audit_logger.emit(event, details)
        _module_logger.info(
            "Flushed %d audit event(s) buffered before init", flushed
        )


def emit(event: str, details: dict[str, Any]) -> None:
    """Emit an audit event (convenience wrapper).

    Backward compatible -- delegates to AuditLogger.emit() which
    writes the simple timestamp+event+details format.

    Parameters
    ----------
    event : str
        Event type (swap, vram_alert, queue_change, request_complete).
    details : dict
        Event-specific data.

    Notes
    -----
    Before :func:`init_audit_logger` runs, events are held in a bounded
    ring buffer (and a WARNING is logged) rather than silently dropped;
    they flush to the audit log on init.
    """
    if _audit_logger is not None:
        _audit_logger.emit(event, details)
        return
    _preinit_events.append((event, details))
    _module_logger.warning(
        "audit.emit('%s') before init_audit_logger — buffered (%d pending, max %d)",
        event, len(_preinit_events), _PREINIT_BUFFER_MAX,
    )


def emit_tiered(
    event_type: str,
    data: dict[str, Any],
    tier_override: int | None = None,
    auth_token: str | None = None,
    a2a_identity: dict[str, Any] | None = None,
    source_ip: str | None = None,
    prompt: str | None = None,
    response: str | None = None,
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
