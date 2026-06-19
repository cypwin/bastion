"""BASTION correlation engine — the moat (design spec 2026-06-19, Section 6).

An in-memory, bounded, **purely passive** engine that joins the signals every
other subsystem already collects onto one monotonic clock and derives net-new
intelligence (a unified event ring, live stall-reason enrichment, and — in later
slices — contention events / RiskIndex / thermal coupling).

Integration is strictly unidirectional. Existing subsystems **never import this
module**; the engine PULLS from them:

* **(A) audit** — via the public ``audit.get_events_since(cursor)`` cursor API
  (6.1 emitter A). The engine stores ``last_ingested_seq`` (a *monotonic
  sequence number*, not a deque index, so it survives ring wraps) and advances
  it each tick. No reach-in to ``audit._recent_events``.
* **(B) system + GPU** — derived from the ``MachineSnapshot`` the
  ``_machine_snapshot_loop`` already assembled (6.1 emitter B). GPU fields are
  ``None`` on non-NVIDIA / no-GPU hosts; every field is guarded before emitting,
  so the engine has **zero** NVIDIA assumptions and degrades automatically.
* **(C) inference** — via a ``_recent_requests`` cursor ingest (6.1 emitter C).
  The record site does **not** import the engine; the engine reads the deque
  contents through a provider callable and tracks its own high-water cursor, so
  ``server.py``'s done-path stays free of a correlation import. Pull, never push.
* **(D) GPU throttle** — from ``GPUExtendedStatus.throttle_reasons`` (already
  collected by the slow tick via the ``GPUBackend`` seam, 6.1 emitter D). Empty
  list on non-NVIDIA → no throttle events.

The engine is instantiated once in ``lifespan()`` and its :meth:`tick` is called
at the end of each ``_machine_snapshot_loop`` iteration, so it adds **zero** new
background tasks and **zero** new I/O — it consumes the snapshot already built
and **never issues a GPU subprocess itself**.

Portability / ADR-005 (hard constraint): this module reuses the definitive fan
curve from :mod:`bastion.constants` and **must never import**
``bastion.dashboard.app`` — the dependency must never point engine → app. All
GPU access is pre-collected via the ``GPUBackend`` seam in the snapshot; this
module parses no nvidia-smi field names and hard-codes no vendor/device value.
All thresholds come from :class:`~bastion.models.CorrelationConfig`.

Everything here is in-memory and bounded: the ring is a
``collections.deque(maxlen=512)``; the cursors are two integers/floats. No DB.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

from bastion.models import (
    CorrelationConfig,
    CorrelationEvent,
    GPUExtendedStatus,
    MachineSnapshot,
)

logger = logging.getLogger("bastion.correlation")

# Bounded ring capacity (spec 6.1 / Constraint #1; ~200 KB ceiling at 512).
RING_MAXLEN = 512

# Default last-N ring tail embedded in CorrelationState.recent_ring_events (4.7).
DEFAULT_RING_TAIL = 32

# Enriched stall-reason suffix cap (spec 6.2 — TUI truncation guard).
STALL_REASON_MAX_CHARS = 150


class CorrelationRing:
    """A bounded monotonic timeline of :class:`CorrelationEvent` (spec 6.1).

    Thin wrapper over ``collections.deque(maxlen=512)``: appends are O(1) and the
    oldest event is discarded automatically once the ring is full, so the
    structure can never grow unbounded across long uptime (Constraint #1). The
    newest event is at the right end; :meth:`tail` returns the most-recent slice
    in chronological (oldest-first) order for the snapshot surface.
    """

    def __init__(self, maxlen: int = RING_MAXLEN) -> None:
        self._events: deque[CorrelationEvent] = deque(maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        """The bound (``512`` by default) — exposed so tests/callers can assert it."""
        ml = self._events.maxlen
        # deque created with a positive maxlen always reports it; default to the
        # constant rather than None so callers never see an unbounded ring.
        return ml if ml is not None else RING_MAXLEN

    def ingest(self, event: CorrelationEvent) -> None:
        """Append one event; the deque discards the oldest if at capacity."""
        self._events.append(event)

    def tail(self, n: int = DEFAULT_RING_TAIL) -> list[CorrelationEvent]:
        """Return the newest ``n`` events, oldest-first.

        Asking for more than the ring holds returns everything it has (never an
        error, never a negative slice).
        """
        if n <= 0:
            return []
        if n >= len(self._events):
            return list(self._events)
        # deque has no slicing; materialize and slice the tail.
        return list(self._events)[-n:]

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)


def enrich_stall_reason(
    base_reason: str | None,
    snapshot: MachineSnapshot | None,
) -> str | None:
    """Append a bracketed live-context suffix to the scheduler stall reason (6.2).

    Pure, **additive** function: the returned string always begins with
    ``base_reason`` — the base is never replaced (existing tests asserting on
    ``stall_reason`` values keep passing). Returns ``base_reason`` unchanged when
    it is ``None`` or empty, and when ``snapshot`` is ``None``. The suffix omits
    any signal that is ``None`` (no NVMe clause on a host with no matching block
    device, no GPU clause on non-NVIDIA), so the enrichment is correct on partial
    snapshots and never fabricates a misleading ``0``. Output is capped at
    :data:`STALL_REASON_MAX_CHARS` characters (TUI truncation guard); when the
    cap bites, the base prefix is preserved.

    Example: ``'swap_cooldown'`` → ``'swap_cooldown [mem-PSI some=18.3, NVMe
    write 94% util]'``.
    """
    if not base_reason:
        # None or "" — passthrough unchanged (additive contract).
        return base_reason
    if snapshot is None:
        return base_reason

    clauses: list[str] = []
    try:
        contention = snapshot.contention
        if contention is not None:
            mem_psi = contention.psi_mem_some_avg10
            if mem_psi is not None:
                clauses.append(f"mem-PSI some={mem_psi:.1f}")
            cpu_psi = contention.psi_cpu_some_avg10
            if cpu_psi is not None:
                clauses.append(f"cpu-PSI some={cpu_psi:.1f}")
            io_psi = contention.psi_io_some_avg10
            if io_psi is not None:
                clauses.append(f"io-PSI some={io_psi:.1f}")
            swap_out = contention.swap_out_rate_mb_s
            if swap_out is not None and swap_out > 0:
                clauses.append(f"swap-out {swap_out:.0f}MB/s")
            # Busiest discovered block device (any of nvme*/sd*/vd*/mmcblk*).
            busiest = _busiest_block_device(contention)
            if busiest is not None:
                dev, util = busiest
                clauses.append(f"{dev} {util:.0f}% util")
        gpu = snapshot.gpu
        if gpu is not None and gpu.temperature_c is not None:
            clauses.append(f"GPU {gpu.temperature_c}C")
    except Exception:
        # Enrichment is best-effort context; never let it break the response.
        logger.debug("stall-reason enrichment failed; returning base", exc_info=True)
        return base_reason

    if not clauses:
        return base_reason

    enriched = f"{base_reason} [{', '.join(clauses)}]"
    if len(enriched) <= STALL_REASON_MAX_CHARS:
        return enriched
    # Cap while preserving the base prefix (truncate the suffix, close bracket).
    return enriched[: STALL_REASON_MAX_CHARS - 1] + "]"


def _busiest_block_device(contention) -> tuple[str, float] | None:
    """Return ``(device, util_pct)`` for the busiest block device, or ``None``.

    Device-generic: keys off whatever base devices ``block_devices`` discovered,
    not NVMe specifically. ``None`` when the list is empty (no matching device).
    """
    devices = getattr(contention, "block_devices", None) or []
    busiest: tuple[str, float] | None = None
    for dev in devices:
        util = getattr(dev, "util_pct", None)
        name = getattr(dev, "device", None)
        if util is None or name is None:
            continue
        if busiest is None or util > busiest[1]:
            busiest = (name, float(util))
    return busiest


class CorrelationEngine:
    """Passive in-memory correlation engine (spec Section 6 core).

    Owns one :class:`CorrelationRing` and the cursors for the pull-based
    emitters. :meth:`tick` is the single entry point: each call ingests all four
    sources (A audit, B system+GPU, C inference, D throttle) and the ring stays
    bounded at 512. The engine performs **no** I/O of its own — emitter B/D read
    the already-assembled snapshot, emitter A reads the audit module's cursor
    API, and emitter C reads ``_recent_requests`` through the injected provider.

    Parameters
    ----------
    recent_requests_provider:
        Zero-arg callable returning the current ``_recent_requests`` contents
        (newest-first, the ``appendleft`` order ``server.py`` uses). ``None``
        disables emitter C (no inference events). Injected rather than imported
        so the record site never depends on this module (pull, not push).
    config:
        :class:`CorrelationConfig` supplying thresholds (PSI / GPU-temp). When
        ``None`` the model defaults are used, so the engine is fully functional
        without explicit config — but every threshold remains operator-tunable.
    audit_module:
        The :mod:`bastion.audit` module (or a test double exposing
        ``get_events_since``). ``None`` lazily imports the real module the first
        time it is needed, keeping construction import-light.
    """

    def __init__(
        self,
        recent_requests_provider: Callable[[], list[dict]] | None = None,
        config: CorrelationConfig | None = None,
        audit_module: object | None = None,
    ) -> None:
        self.config = config if config is not None else CorrelationConfig()
        self.ring = CorrelationRing(maxlen=self.config.ring_maxlen)
        self._recent_requests_provider = recent_requests_provider
        self._audit_module = audit_module

        # Emitter A cursor: a monotonic audit sequence number (NOT a deque
        # index), stable across ring wraps. 0 = ingest whatever the ring holds.
        self._audit_cursor: int = 0

        # Emitter C cursor: a high-water timestamp over _recent_requests plus a
        # count of records already consumed that share exactly that timestamp,
        # so equal-timestamp records are not double-counted on the next tick.
        self._inference_cursor_ts: float = float("-inf")
        self._inference_seen_at_ts: int = 0

        # Emitter B/D edge state: the set of conditions currently "active" so a
        # crossing emits once (rising edge), not every tick above threshold.
        self._active_conditions: set[str] = set()
        self._active_throttle_reasons: set[str] = set()

    # ------------------------------------------------------------------- tick

    def tick(self, snapshot: MachineSnapshot | None) -> None:
        """Ingest all four sources for this collection tick (spec 6.6).

        Called at the end of each ``_machine_snapshot_loop`` iteration with the
        snapshot that loop just built. Every emitter is individually guarded —
        one failing source never blocks the others and never raises into the
        loop (which must never die). The ring stays bounded at its configured
        ``maxlen`` regardless of how many events a single tick produces.
        """
        # (A) audit — cursor pull.
        try:
            self._ingest_audit_events()
        except Exception:
            logger.debug("audit emitter failed", exc_info=True)
        # (C) inference — cursor pull (independent of the snapshot).
        try:
            self._ingest_inference_events()
        except Exception:
            logger.debug("inference emitter failed", exc_info=True)
        # (B) + (D) require the snapshot.
        if snapshot is not None:
            try:
                self._ingest_system_gpu_events(snapshot)
            except Exception:
                logger.debug("system/GPU emitter failed", exc_info=True)
            try:
                self._ingest_throttle_events(snapshot.gpu_extended)
            except Exception:
                logger.debug("throttle emitter failed", exc_info=True)

    # --------------------------------------------------------- emitter A (audit)

    def _ingest_audit_events(self) -> None:
        """Pull new audit events via the public cursor API (no double-ingest)."""
        audit = self._audit_module
        if audit is None:
            from bastion import audit as audit_module  # lazy, import-light ctor

            audit = self._audit_module = audit_module
        get_events_since = getattr(audit, "get_events_since", None)
        if get_events_since is None:
            return
        events, new_cursor = get_events_since(self._audit_cursor)
        self._audit_cursor = new_cursor
        now_mono = time.monotonic()
        for ev in events:
            kind = ev.get("event", "audit") if isinstance(ev, dict) else "audit"
            ts_wall = self._wall_ts(ev)
            self.ring.ingest(
                CorrelationEvent(
                    ts_monotonic=now_mono,
                    ts_wall=ts_wall,
                    domain="scheduler",
                    kind=str(kind),
                    payload=self._audit_payload(ev),
                )
            )

    @staticmethod
    def _audit_payload(ev: object) -> dict:
        if isinstance(ev, dict):
            details = ev.get("details")
            if isinstance(details, dict):
                return dict(details)
            # Keep a compact, bounded projection — never the whole record.
            return {k: ev[k] for k in ("event", "tier") if k in ev}
        return {}

    @staticmethod
    def _wall_ts(ev: object) -> float:
        if isinstance(ev, dict):
            ts = ev.get("ts") or ev.get("timestamp")
            if isinstance(ts, (int, float)):
                return float(ts)
        return time.time()

    # ----------------------------------------------------- emitter C (inference)

    def _ingest_inference_events(self) -> None:
        """Pull new inference records from ``_recent_requests`` via the cursor.

        The provider returns the deque newest-first (``appendleft`` order); we
        reverse to oldest-first and ingest only records strictly newer than the
        high-water timestamp (with an equal-timestamp tie-count so a record
        sharing the high-water instant is not re-ingested). Only records carrying
        a token signal (Section 4.6 keys) become inference events — pure
        non-inference traffic (``/api/tags`` etc.) is skipped, never a 0-rate
        event.
        """
        provider = self._recent_requests_provider
        if provider is None:
            return
        records = provider()
        if not records:
            return
        # newest-first -> oldest-first for chronological ingest.
        ordered = list(reversed(records))

        cursor_ts = self._inference_cursor_ts
        seen_at_ts = self._inference_seen_at_ts
        new_cursor_ts = cursor_ts
        new_seen_at_ts = seen_at_ts
        now_mono = time.monotonic()

        # Count how many records at exactly cursor_ts we are about to skip, so
        # equal-timestamp records added later still ingest exactly once.
        skipped_at_cursor = 0
        for rec in ordered:
            ts = rec.get("timestamp")
            ts_f = float(ts) if isinstance(ts, (int, float)) else None

            if ts_f is not None and ts_f < cursor_ts:
                continue  # already ingested in a prior tick
            if ts_f is not None and ts_f == cursor_ts:
                # Same instant as the high-water mark: skip the ones we already
                # consumed; ingest any surplus that arrived since.
                skipped_at_cursor += 1
                if skipped_at_cursor <= seen_at_ts:
                    continue

            if not self._is_inference_record(rec):
                # Still advance the cursor over a non-inference record so it is
                # not re-examined forever, but emit no event.
                new_cursor_ts, new_seen_at_ts = self._advance_cursor(
                    ts_f, new_cursor_ts, new_seen_at_ts
                )
                continue

            self.ring.ingest(
                CorrelationEvent(
                    ts_monotonic=now_mono,
                    ts_wall=ts_f if ts_f is not None else time.time(),
                    domain="inference",
                    kind="request_complete",
                    payload=self._inference_payload(rec),
                )
            )
            new_cursor_ts, new_seen_at_ts = self._advance_cursor(
                ts_f, new_cursor_ts, new_seen_at_ts
            )

        self._inference_cursor_ts = new_cursor_ts
        self._inference_seen_at_ts = new_seen_at_ts

    @staticmethod
    def _advance_cursor(
        ts_f: float | None,
        cursor_ts: float,
        seen_at_ts: int,
    ) -> tuple[float, int]:
        """Advance the (high-water-ts, tie-count) cursor for one consumed record."""
        if ts_f is None:
            return cursor_ts, seen_at_ts
        if ts_f > cursor_ts:
            return ts_f, 1
        if ts_f == cursor_ts:
            return cursor_ts, seen_at_ts + 1
        return cursor_ts, seen_at_ts

    @staticmethod
    def _is_inference_record(rec: dict) -> bool:
        """True if the record carries any stream-tapped token signal (4.6)."""
        for key in (
            "decode_tps",
            "prefill_tps",
            "ttft_s",
            "ctx_utilization",
            "eval_count",
            "prompt_eval_count",
        ):
            if rec.get(key) is not None:
                return True
        return False

    @staticmethod
    def _inference_payload(rec: dict) -> dict:
        """Compact, bounded projection of an inference record (no per-id labels)."""
        payload: dict = {}
        for key in (
            "model",
            "endpoint",
            "decode_tps",
            "prefill_tps",
            "ttft_s",
            "ctx_utilization",
            "queue_wait_s",
            "duration_s",
        ):
            if key in rec and rec[key] is not None:
                payload[key] = rec[key]
        return payload

    # ------------------------------------------------ emitter B (system + GPU)

    def _ingest_system_gpu_events(self, snapshot: MachineSnapshot) -> None:
        """Emit system/GPU events on rising-edge threshold crossings.

        Edge-detected (emit at crossing, not every tick above threshold) so a
        sustained-pressure tick stream does not flood the ring. Every field is
        guarded for ``None`` first — on a no-GPU / no-PSI host nothing is
        emitted (no misleading 0). Thresholds come from
        :class:`CorrelationConfig`, never a hard-coded constant.
        """
        cfg = self.config
        now_mono = time.monotonic()
        now_wall = time.time()

        def emit(domain: str, kind: str, condition_key: str, payload: dict) -> None:
            self.ring.ingest(
                CorrelationEvent(
                    ts_monotonic=now_mono,
                    ts_wall=now_wall,
                    domain=domain,  # type: ignore[arg-type]
                    kind=kind,
                    payload=payload,
                )
            )

        # --- System pressure (PSI), guarded for None ------------------------
        contention = snapshot.contention
        if contention is not None:
            mem_psi = contention.psi_mem_some_avg10
            self._edge(
                "mem_psi_high",
                mem_psi is not None and mem_psi >= cfg.contention_psi_threshold,
                lambda v=mem_psi: emit(
                    "system", "mem_pressure", "mem_psi_high", {"psi_mem_some_avg10": v}
                ),
            )
            cpu_psi = contention.psi_cpu_some_avg10
            self._edge(
                "cpu_psi_high",
                cpu_psi is not None and cpu_psi >= cfg.contention_cpu_psi_threshold,
                lambda v=cpu_psi: emit(
                    "system", "cpu_contention", "cpu_psi_high", {"psi_cpu_some_avg10": v}
                ),
            )

        # --- GPU thermal headroom collapse, guarded for None ----------------
        gpu = snapshot.gpu
        if gpu is not None and gpu.temperature_c is not None:
            ceiling = self._gpu_ceiling()
            if ceiling is not None and ceiling > 0:
                # "Hot" once within 5C of the configured/auto-detected ceiling.
                hot = gpu.temperature_c >= (ceiling - 5)
                self._edge(
                    "gpu_temp_high",
                    hot,
                    lambda t=gpu.temperature_c, c=ceiling: emit(
                        "gpu", "thermal", "gpu_temp_high",
                        {"temperature_c": t, "ceiling_c": c},
                    ),
                )

    def _edge(self, key: str, condition: bool, emit_fn: Callable[[], None]) -> None:
        """Rising-edge gate: call ``emit_fn`` only when ``condition`` newly holds."""
        if condition:
            if key not in self._active_conditions:
                self._active_conditions.add(key)
                emit_fn()
        else:
            self._active_conditions.discard(key)

    def _gpu_ceiling(self) -> float | None:
        """GPU thermal ceiling for threshold checks (config-driven, 6.5).

        ``CorrelationConfig.gpu_safe_ceiling_c`` if set, else ``None`` (the
        engine core has no access to ``GPUConfig.max_temperature_c`` here; the
        server-wired engine passes the resolved ceiling via config). ``None``
        means "no GPU ceiling known" → no GPU-temp event (no misleading default).
        """
        return self.config.gpu_safe_ceiling_c

    # ---------------------------------------------------- emitter D (throttle)

    def _ingest_throttle_events(self, gpu_extended: GPUExtendedStatus | None) -> None:
        """Emit a GPU throttle event when a reason newly appears (rising edge).

        Reads the decoded ``throttle_reasons`` list the slow tick already
        collected via the ``GPUBackend`` seam — no subprocess here. Empty list on
        non-NVIDIA hosts → no events. Edge-detected per reason so a sustained
        throttle does not re-emit every tick.
        """
        if gpu_extended is None:
            self._active_throttle_reasons.clear()
            return
        reasons = set(gpu_extended.throttle_reasons or [])
        new_reasons = reasons - self._active_throttle_reasons
        now_mono = time.monotonic()
        now_wall = time.time()
        for reason in sorted(new_reasons):
            self.ring.ingest(
                CorrelationEvent(
                    ts_monotonic=now_mono,
                    ts_wall=now_wall,
                    domain="gpu",
                    kind="throttle",
                    payload={"reason": reason},
                )
            )
        # Track the current active set so a reason clearing then recurring emits
        # again, but a sustained reason does not.
        self._active_throttle_reasons = reasons
