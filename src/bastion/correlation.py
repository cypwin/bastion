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
from typing import Callable, Literal

from bastion.constants import _fan_band
from bastion.models import (
    ContentionEvent,
    ContentionSnapshot,
    CorrelationConfig,
    CorrelationEvent,
    GPUExtendedStatus,
    MachineSnapshot,
    RiskIndexResult,
    ThermalCoupling,
)

logger = logging.getLogger("bastion.correlation")

# Bounded capacity of the dedicated discrete-contention-event deque (spec 6.3 /
# Constraint #1). Separate from the ring because contention events are NOT in
# the snapshot body — they ride GET /broker/correlation/contentions.
CONTENTION_MAXLEN = 50

# RiskIndex levels keyed on the composite score (spec 6.4). Forward-looking:
# "risk approaching, not a crash".
_RISK_LEVEL_NOMINAL = 0.30
_RISK_LEVEL_ELEVATED = 0.55
_RISK_LEVEL_HIGH = 0.80

# The five canonical RiskIndex component names (spec 6.4). dominant_factor is
# always one of these, even when every input is None.
RISK_COMPONENT_NAMES: tuple[str, ...] = (
    "vram_headroom",
    "thermal_headroom",
    "swap_rate",
    "thrashing",
    "memory_psi",
)

# Headroom (in C) at which the thermal-headroom risk term saturates to 1.0
# (spec 6.4 — each component normalized to [0,1] before weighting). 20C of
# headroom or more is treated as zero thermal risk; 0C as full risk.
_THERMAL_HEADROOM_FULL_RISK_C = 0.0
_THERMAL_HEADROOM_ZERO_RISK_C = 20.0

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


# ===========================================================================
# 6.3 ContentionEventDetector — discrete non-inference contention
# ===========================================================================


class ContentionEventDetector:
    """Stateful detector for discrete, attributable host-contention events (6.3).

    Compares consecutive ticks and emits a :class:`ContentionEvent` **only when**
    a threshold crossing **coincides** with an active inference stall — the
    simultaneous-confirmation join ("IO at 94 % **AND** inference stalled at the
    same instant") that is the moat. ``htop`` shows the IO alone; only BASTION
    shows the coincidence.

    Two clearly-separated thresholds, each on its **own unit**, both from
    :class:`~bastion.models.CorrelationConfig` (no hard-coded constants):

    * **disk leg** — ``contention_block_write_mb_s_threshold`` (MB/s) against the
      busiest discovered block device's ``write_rate_mb_s``. Device-generic: it
      keys off whatever base devices ``block_devices`` discovered
      (``nvme*/sd*/vd*/mmcblk*``), not NVMe specifically.
    * **PSI leg** — ``contention_psi_threshold`` against ``psi_mem_some_avg10``.

    Each leg uses **edge detection** (fire at the crossing, not every tick above
    threshold) plus a **2-tick hysteresis** (``contention_hysteresis_ticks``): a
    leg's condition must hold for that many consecutive ticks before it is
    eligible to fire, which kills transient kernel-flush spikes. On a host with
    no PSI and/or no matching block device the corresponding leg's input is
    ``None`` and that leg simply never fires; the detector degrades to whatever
    legs *are* available (legs degrade independently).

    Discrete events land in a dedicated bounded ``deque(maxlen=50)``
    (:data:`CONTENTION_MAXLEN`) — separate from the ring because contention
    events are not in the snapshot body (they ride
    ``GET /broker/correlation/contentions``).
    """

    def __init__(self, config: CorrelationConfig | None = None) -> None:
        self.config = config if config is not None else CorrelationConfig()
        self.recent_contentions: deque[ContentionEvent] = deque(maxlen=CONTENTION_MAXLEN)
        # Per-leg consecutive-over-threshold counters (the hysteresis state) and
        # the "already fired on this sustained crossing" edge latch.
        self._disk_streak = 0
        self._psi_streak = 0
        self._disk_fired = False
        self._psi_fired = False

    # -- config accessors (so tests/callers can assert the two units) ---------

    @property
    def write_mb_s_threshold(self) -> float:
        """Disk-leg threshold (MB/s) — ``contention_block_write_mb_s_threshold``."""
        return self.config.contention_block_write_mb_s_threshold

    @property
    def psi_threshold(self) -> float:
        """PSI-leg threshold — ``contention_psi_threshold`` on ``psi_mem_some_avg10``."""
        return self.config.contention_psi_threshold

    @property
    def hysteresis_ticks(self) -> int:
        """Consecutive over-threshold ticks required before a leg may fire."""
        return self.config.contention_hysteresis_ticks

    # -- the per-tick entry point --------------------------------------------

    def feed(
        self,
        snapshot: MachineSnapshot | None,
        *,
        inference_stalled: bool,
        stall_reason: str | None,
    ) -> ContentionEvent | None:
        """Advance the detector by one tick; return a fired event or ``None``.

        ``inference_stalled``/``stall_reason`` describe the inference state *at
        this instant* (the scheduler stall the caller already knows). The
        coincidence join requires a **real** stall: ``inference_stalled`` True
        **and** a non-empty ``stall_reason``. A missing ``contention`` block (or
        a ``None`` snapshot) yields no event and never raises — host data is
        best-effort. Both legs advance their hysteresis each tick; the first leg
        that satisfies hysteresis *and* coincides with a stall fires (disk
        preferred, then PSI), and the resulting event is appended to the bounded
        deque.
        """
        try:
            return self._feed(snapshot, inference_stalled, stall_reason)
        except Exception:
            # Contention detection is best-effort intelligence; never raise into
            # the snapshot loop.
            logger.debug("contention detector tick failed", exc_info=True)
            return None

    def _feed(
        self,
        snapshot: MachineSnapshot | None,
        inference_stalled: bool,
        stall_reason: str | None,
    ) -> ContentionEvent | None:
        contention = snapshot.contention if snapshot is not None else None

        # --- evaluate each leg's raw over-threshold condition ----------------
        write_mb_s = self._busiest_write_rate(contention)
        disk_over = write_mb_s is not None and write_mb_s >= self.write_mb_s_threshold

        psi_mem = contention.psi_mem_some_avg10 if contention is not None else None
        psi_over = psi_mem is not None and psi_mem >= self.psi_threshold

        # --- advance hysteresis streaks + edge latches -----------------------
        self._disk_streak, self._disk_fired = self._advance_leg(
            disk_over, self._disk_streak, self._disk_fired
        )
        self._psi_streak, self._psi_fired = self._advance_leg(
            psi_over, self._psi_streak, self._psi_fired
        )

        # A real inference stall = flagged AND a non-empty reason. The join is
        # the contract: without it, no event regardless of how high the IO is.
        stalled = bool(inference_stalled and stall_reason)
        if not stalled:
            return None

        threshold = self.hysteresis_ticks

        # Disk leg first (the canonical "NVMe burst"), then PSI. A leg is
        # eligible once its streak reaches the hysteresis and it has not already
        # fired on the current sustained crossing.
        if disk_over and self._disk_streak >= threshold and not self._disk_fired:
            self._disk_fired = True
            return self._fire(
                kind="nvme_burst",
                attribution=self._disk_attribution(contention, write_mb_s),
                stall_reason=stall_reason,
                payload={
                    "write_rate_mb_s": write_mb_s,
                    "threshold_mb_s": self.write_mb_s_threshold,
                },
            )
        if psi_over and self._psi_streak >= threshold and not self._psi_fired:
            self._psi_fired = True
            return self._fire(
                kind="mem_pressure",
                attribution=f"memory PSI some={psi_mem:.1f} (>= {self.psi_threshold:.0f})",
                stall_reason=stall_reason,
                payload={
                    "psi_mem_some_avg10": psi_mem,
                    "threshold": self.psi_threshold,
                },
            )
        return None

    @staticmethod
    def _advance_leg(over: bool, streak: int, fired: bool) -> tuple[int, bool]:
        """Advance one leg's (streak, fired-latch) for this tick.

        While the condition holds the streak increments (capped where it stops
        mattering). When it clears, the streak resets to 0 and the edge latch
        clears so a *new* crossing can fire again.
        """
        if over:
            # Cap the streak so a long sustained crossing does not overflow; any
            # value >= hysteresis is equivalent for the eligibility check.
            return min(streak + 1, 1_000_000), fired
        return 0, False

    def _fire(
        self,
        *,
        kind: str,
        attribution: str,
        stall_reason: str | None,
        payload: dict,
    ) -> ContentionEvent:
        event = ContentionEvent(
            ts_monotonic=time.monotonic(),
            ts_wall=time.time(),
            domain="system",
            kind=kind,
            payload=payload,
            attribution=attribution,
            inference_was_stalled=True,
            stall_reason_at_time=stall_reason,
        )
        self.recent_contentions.append(event)
        return event

    @staticmethod
    def _busiest_write_rate(contention: ContentionSnapshot | None) -> float | None:
        """Highest ``write_rate_mb_s`` across discovered block devices, or ``None``.

        ``None`` when there is no contention block or no matching device (the
        disk leg input is absent — the leg simply never fires, no misleading 0).
        """
        if contention is None:
            return None
        devices = getattr(contention, "block_devices", None) or []
        busiest: float | None = None
        for dev in devices:
            rate = getattr(dev, "write_rate_mb_s", None)
            if rate is None:
                continue
            if busiest is None or rate > busiest:
                busiest = float(rate)
        return busiest

    @staticmethod
    def _disk_attribution(
        contention: ContentionSnapshot | None, write_mb_s: float | None,
    ) -> str:
        """Category-level attribution string for a disk-leg event.

        Stays category-level (device name + rate) — process names are reserved
        for the TUI process list, never the JSON API, to avoid leaking process
        info (spec 6.3).
        """
        name = "block-device"
        if contention is not None:
            for dev in getattr(contention, "block_devices", None) or []:
                if getattr(dev, "write_rate_mb_s", None) == write_mb_s:
                    name = getattr(dev, "device", None) or name
                    break
        rate_txt = f"{write_mb_s:.0f}MB/s" if write_mb_s is not None else "?"
        return f"{name} write {rate_txt}"


# ===========================================================================
# 6.4 RiskIndex — composite forward-looking gauge
# ===========================================================================


def _clamp01(x: float) -> float:
    """Clamp ``x`` into the unit interval [0, 1]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _swap_rate_component(level: str | None) -> float | None:
    """Normalize a scheduler swap-rate level label to [0, 1] (``None`` -> absent).

    The detector's level labels (``normal``/``warn``/``critical``) are mapped to
    risk magnitudes. An unknown label degrades to ``None`` (absent term), never a
    misleading 0. The mapping reflects the *ordering* of the configured
    thresholds, not their literal values, so it stays portable.
    """
    if level is None:
        return None
    mapping = {"normal": 0.0, "warn": 0.5, "critical": 1.0}
    return mapping.get(str(level).lower())


def _thrashing_component(verdict: str | None) -> float | None:
    """Normalize a thrashing worst-verdict label to [0, 1] (``None`` -> absent)."""
    if verdict is None:
        return None
    mapping = {"ok": 0.0, "warn": 0.5, "halt": 1.0, "halted": 1.0, "warned": 0.5}
    return mapping.get(str(verdict).lower())


def _level_for_score(score: float) -> Literal["nominal", "elevated", "high", "critical"]:
    """Map a composite score in [0, 1] to a discrete risk level (spec 6.4)."""
    if score < _RISK_LEVEL_NOMINAL:
        return "nominal"
    if score < _RISK_LEVEL_ELEVATED:
        return "elevated"
    if score < _RISK_LEVEL_HIGH:
        return "high"
    return "critical"


def compute_risk_index(
    *,
    vram_utilization_pct: float | None,
    thermal_headroom_c: float | None,
    swap_rate_level: str | None,
    thrashing_verdict: str | None,
    memory_psi: float | None,
    config: CorrelationConfig | None = None,
) -> RiskIndexResult:
    """Fold five live signals into one composite risk gauge (spec 6.4).

    Pure function: takes already-extracted scalar inputs (so it is trivially
    testable and has no I/O), normalizes each to [0, 1], weights them by
    ``config.risk_weights``, and returns a :class:`RiskIndexResult` with
    ``score`` ∈ [0, 1], a discrete ``level``, the per-component ``component_scores``
    (only the *measured* components), and a ``dominant_factor`` (always one of the
    five bounded component names, so it is safe as a Prometheus label).

    Each component degrades **independently**: any input that is ``None`` (e.g.
    thermal headroom on a no-GPU host, PSI on an old kernel) contributes nothing
    — the term is *absent* from the weighted average, **not** a misleading
    zero-risk reading for a present-but-unmeasured signal. When every input is
    ``None`` the score is ``0.0``/``nominal``. The composite is the weighted mean
    over the components that *are* present, so dropping a component does not
    artificially deflate the score.

    Parameters
    ----------
    vram_utilization_pct:
        VRAM used as a percentage (0-100); higher = less headroom = more risk.
    thermal_headroom_c:
        Minimum thermal headroom in C (e.g. from :class:`ThermalCoupling`); less
        headroom = more risk. Saturates to full risk at 0C, zero risk at
        :data:`_THERMAL_HEADROOM_ZERO_RISK_C`.
    swap_rate_level:
        Scheduler swap-rate level label (``normal``/``warn``/``critical``).
    thrashing_verdict:
        Worst thrashing verdict label (``ok``/``warn``/``halt``).
    memory_psi:
        ``psi_mem_some_avg10`` (0-100); higher = more stalled-on-memory = more risk.
    """
    cfg = config if config is not None else CorrelationConfig()
    weights = cfg.risk_weights or {}

    # --- normalize each component to [0, 1]; None => absent term -------------
    components: dict[str, float | None] = {
        "vram_headroom": (
            _clamp01(vram_utilization_pct / 100.0)
            if vram_utilization_pct is not None
            else None
        ),
        "thermal_headroom": _normalize_thermal_headroom(thermal_headroom_c),
        "swap_rate": _swap_rate_component(swap_rate_level),
        "thrashing": _thrashing_component(thrashing_verdict),
        "memory_psi": (
            _clamp01(memory_psi / 100.0) if memory_psi is not None else None
        ),
    }

    component_scores: dict[str, float] = {
        name: val for name, val in components.items() if val is not None
    }

    # Weighted mean over the PRESENT components (so an absent term neither adds
    # risk nor deflates the score by occupying weight it cannot fill).
    weighted_sum = 0.0
    weight_total = 0.0
    for name, val in component_scores.items():
        w = float(weights.get(name, 0.0))
        weighted_sum += w * val
        weight_total += w
    score = _clamp01(weighted_sum / weight_total) if weight_total > 0 else 0.0

    dominant_factor = _dominant_factor(component_scores, weights)

    return RiskIndexResult(
        score=score,
        level=_level_for_score(score),
        component_scores=component_scores,
        dominant_factor=dominant_factor,
    )


def _normalize_thermal_headroom(headroom_c: float | None) -> float | None:
    """Map thermal headroom (C) to a [0, 1] risk magnitude (``None`` -> absent).

    0C of headroom (or less) = full risk (1.0); ``_THERMAL_HEADROOM_ZERO_RISK_C``
    or more = zero risk (0.0); linear in between.
    """
    if headroom_c is None:
        return None
    span = _THERMAL_HEADROOM_ZERO_RISK_C - _THERMAL_HEADROOM_FULL_RISK_C
    if span <= 0:
        return 1.0 if headroom_c <= _THERMAL_HEADROOM_FULL_RISK_C else 0.0
    risk = (_THERMAL_HEADROOM_ZERO_RISK_C - headroom_c) / span
    return _clamp01(risk)


def _dominant_factor(
    component_scores: dict[str, float], weights: dict[str, float],
) -> str:
    """The single most risk-contributing component name (always one of the five).

    Ranks by **weighted** contribution (``weight * component_score``) so the
    dominant factor reflects what actually moved the composite, then breaks ties
    by the canonical order. When no component is measured, falls back to the
    first canonical name so the field is never empty and stays a bounded label.
    """
    best_name: str | None = None
    best_contrib = -1.0
    for name in RISK_COMPONENT_NAMES:
        if name not in component_scores:
            continue
        contrib = float(weights.get(name, 0.0)) * component_scores[name]
        if contrib > best_contrib:
            best_contrib = contrib
            best_name = name
    if best_name is not None:
        return best_name
    return RISK_COMPONENT_NAMES[0]


# ===========================================================================
# 6.5 CPU<->GPU thermal coupling
# ===========================================================================


def build_thermal_coupling(
    *,
    cpu_temp_c: float | None,
    gpu_temp_c: float | None,
    fan_speed_pct: int | None,
    gpu_max_temperature_c: int | float | None,
    config: CorrelationConfig | None = None,
) -> ThermalCoupling:
    """Build the :class:`ThermalCoupling` derivation (spec 6.5).

    Makes explicit what the TUI auto-fan logic already knows implicitly: CPU heat
    drives the GPU fan, so CPU heat indirectly constrains GPU throughput.

    ``coupling_active`` is derived from the **definitive fan curve**
    (:func:`bastion.constants._fan_band`) — never a duplicated constant —
    as ``cpu_temp_c is not None and _fan_band(cpu_temp_c) is not None``, so any
    future change to the escalation curve is honored automatically and there is
    no app->engine / engine->app import (ADR-005).

    ``thermal_headroom_min_c`` is the minimum headroom over the two terms that
    are computable::

        min(
            gpu_ceiling      - gpu_temp_c,   # GPU term — only if both non-None
            cpu_safe_ceiling - cpu_temp_c,   # CPU term — only if both non-None
        )

    where ``cpu_safe_ceiling`` = ``config.cpu_safe_ceiling_c`` (default **85.0**,
    NOT the 60C fan-engagement threshold — that would read zero headroom the
    instant the fan engages) and ``gpu_ceiling`` = ``config.gpu_safe_ceiling_c``
    if set, else ``gpu_max_temperature_c`` (the device-auto-detected
    ``GPUConfig.max_temperature_c``). A term is **skipped** when its inputs are
    missing (``gpu_temp_c`` None, or the GPU ceiling unset/0 on a no-GPU host),
    so the headroom is the present-terms-only value, never a misleading 0; it is
    ``None`` only when *neither* term is computable.

    All inputs are ``None``-tolerant: ``gpu_temp_c``/``fan_speed_pct`` are
    ``None`` on non-NVIDIA / no-GPU / fanless-server-GPU hosts; ``cpu_temp_c`` is
    ``None`` when no CPU sensor is discovered.
    """
    cfg = config if config is not None else CorrelationConfig()

    coupling_active = cpu_temp_c is not None and _fan_band(cpu_temp_c) is not None

    # Resolve the GPU ceiling: explicit override wins, else the device-detected
    # GPUConfig.max_temperature_c. 0/None/<=0 means "no GPU ceiling known".
    gpu_ceiling: float | None = cfg.gpu_safe_ceiling_c
    if gpu_ceiling is None:
        if gpu_max_temperature_c is not None and gpu_max_temperature_c > 0:
            gpu_ceiling = float(gpu_max_temperature_c)

    headroom_terms: list[float] = []
    if gpu_ceiling is not None and gpu_ceiling > 0 and gpu_temp_c is not None:
        headroom_terms.append(gpu_ceiling - gpu_temp_c)
    if cpu_temp_c is not None:
        headroom_terms.append(cfg.cpu_safe_ceiling_c - cpu_temp_c)

    thermal_headroom_min_c = min(headroom_terms) if headroom_terms else None

    return ThermalCoupling(
        cpu_temp_c=cpu_temp_c,
        gpu_temp_c=gpu_temp_c,
        fan_speed_pct=fan_speed_pct,
        coupling_active=coupling_active,
        thermal_headroom_min_c=thermal_headroom_min_c,
    )
