"""Swap-velocity circuit breaker — the sensor-independent crash backstop (F1/F2/F4).

The 2026-06-26 hard lockup was a physical power/driver transient produced by the
*velocity* of (individually correct) model swaps, not a logical VRAM over-commit.
This module bounds that velocity by counting BASTION's OWN residency transitions
on an injected monotonic clock — so it keeps working when every nvidia-smi /
``/api/ps`` sensor is dark, which is exactly when the host is most likely to die.

Three composed layers (see the design spec, §1):
  1. **Min-spacing floor** between cold *loads* — the inrush guarantee.
  2. **Token bucket** — bounds the sustained average (drains *during* a burst).
  3. **CLOSED→THROTTLED→OPEN→HALF_OPEN** state machine — a hard pause with
     hysteresis + exponential backoff.

Plus a candidate-keyed **set-level infeasible latch** (F4): once BASTION detects
that satisfying the demanded resident set would require evicting an externally
pinned model, it sheds the offending candidate instead of thrashing.

Design contract (load-bearing):
  - **Every method is strictly synchronous** (zero ``await``). Each call is atomic
    under asyncio; the only async serialization is the scheduler-owned load
    serializer, inside which the authoritative ``acquire()``+``record_load()`` run.
  - **All clock deltas are clamped ``max(0.0, now - last)``** so a backward NTP step
    / suspend-resume cannot silently disable the brake.
  - ``acquire()`` is the authoritative gate but **never debits a token**;
    ``record_load()`` debits at the true GPU-I/O point. ``peek()`` is a pure,
    side-effect-free variant for the scheduler's cheap pre-eviction early-reject.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from bastion.models import SwapBrakeConfig

_WINDOW_SECONDS = 60.0  # rolling window for the per-minute rate (release hysteresis)


class BrakeState(StrEnum):
    CLOSED = "closed"        # healthy; swaps allowed subject to spacing + tokens
    THROTTLED = "throttled"  # soft-limiting (spacing/bucket), will self-resolve
    OPEN = "open"            # hard pause for the cooloff window
    HALF_OPEN = "half_open"  # post-cooloff; grants exactly one probe


@dataclass
class BrakeDecision:
    """Outcome of a brake gate check."""
    action: Literal["proceed", "stall", "shed"]
    reason: str
    retry_after_s: float


class SwapBrake:
    """Sensor-independent swap-velocity circuit breaker.

    Parameters
    ----------
    cfg : SwapBrakeConfig
        Brake tuning (portable floors; calibrated down via ``--stress-test``).
    clock : Callable[[], float]
        Monotonic clock source. Injected for deterministic testing; defaults to
        ``time.monotonic`` (NEVER ``time.time`` — a wall-clock step would disarm
        the brake).
    """

    def __init__(self, cfg: SwapBrakeConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._clock = clock
        now = clock()

        self._state: BrakeState = BrakeState.CLOSED
        self._tokens: float = float(cfg.bucket_capacity)
        self._last_refill_t: float = now
        self._last_load_t: float | None = None  # None ⇒ no spacing constraint yet
        self._state_entered_t: float = now
        self._brake_until: float = 0.0
        self._backoff_level: int = 0
        self._empty_since: float | None = None
        self._hw_degraded: bool = False
        self._drain_active: bool = False

        self._window: deque[float] = deque()  # transition timestamps (loads + counted unloads)

        # HALF_OPEN probe lifecycle
        self._probe_outstanding: bool = False
        self._half_open_probe_used: bool = False

        # Candidate-keyed set-level infeasible latch: model -> TTL-expiry (monotonic)
        self._infeasible: dict[str, float] = {}
        self._latch_baseline: dict[str, frozenset[str]] = {}
        self._last_resident: frozenset[str] | None = None

        # Auto-expiring admin override
        self._force_release_until: float = 0.0
        self._force_engage_until: float = 0.0

    # ── internal helpers (all sync) ────────────────────────────────────

    def _refill_rate_per_sec(self) -> float:
        rate = self._cfg.refill_per_minute / 60.0
        if self._hw_degraded:
            rate *= self._cfg.degraded_refill_factor
        return rate

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill_t)  # clamp: backward step ⇒ no negative refill
        self._tokens = min(
            float(self._cfg.bucket_capacity),
            self._tokens + elapsed * self._refill_rate_per_sec(),
        )
        self._last_refill_t = now

    def _effective_tokens(self, now: float) -> float:
        """Read-only token estimate (for peek/snapshot) — does NOT mutate state."""
        elapsed = max(0.0, now - self._last_refill_t)
        return min(float(self._cfg.bucket_capacity), self._tokens + elapsed * self._refill_rate_per_sec())

    def _prune_window(self, now: float) -> None:
        while self._window and (now - self._window[0]) > _WINDOW_SECONDS:
            self._window.popleft()

    def _windowed_rate_per_min(self, now: float) -> float:
        self._prune_window(now)
        return float(len(self._window))  # count within the 60s window == per-minute rate

    def _spacing_ok(self, now: float) -> bool:
        if self._last_load_t is None:
            return True
        return (now - self._last_load_t) >= self._cfg.min_spacing_seconds

    def _spacing_remaining(self, now: float) -> float:
        if self._last_load_t is None:
            return 0.0
        return max(0.0, self._cfg.min_spacing_seconds - (now - self._last_load_t))

    def _tokens_eta(self, now: float) -> float:
        rate = self._refill_rate_per_sec()
        if rate <= 0.0:
            return self._cfg.cooloff_seconds
        return max(0.0, (1.0 - self._effective_tokens(now)) / rate)

    def _enter(self, state: BrakeState, now: float) -> None:
        if state != self._state:
            self._state = state
            self._state_entered_t = now

    def _open(self, now: float) -> None:
        """Enter OPEN with cooloff + exponential backoff (capped)."""
        self._backoff_level += 1
        cooloff = min(
            self._cfg.cooloff_seconds * (2 ** (self._backoff_level - 1)),
            self._cfg.cooloff_backoff_max_seconds,
        )
        self._brake_until = now + cooloff
        self._empty_since = None
        self._probe_outstanding = False
        self._half_open_probe_used = False
        self._enter(BrakeState.OPEN, now)

    def _can_enter_halfopen(self, now: float) -> bool:
        # Time-floor authoritative (R3-5): the probe is granted on cooloff +
        # min-state-hold + not-drain, regardless of rate — so a never-draining
        # queue can never wedge the brake permanently. Rate gates CLOSING only.
        if self._drain_active:
            return False
        if now < self._brake_until:
            return False
        return (now - self._state_entered_t) >= self._cfg.min_state_hold_seconds

    def _can_close(self, now: float) -> bool:
        if self._drain_active:
            return False
        if (now - self._state_entered_t) < self._cfg.min_state_hold_seconds:
            return False
        return self._windowed_rate_per_min(now) <= self._cfg.release_rate_per_minute

    def _maybe_reset_backoff(self, now: float) -> None:
        if (
            self._state == BrakeState.CLOSED
            and (now - self._state_entered_t) >= self._cfg.min_state_hold_seconds
            and self._windowed_rate_per_min(now) <= self._cfg.release_rate_per_minute
        ):
            self._backoff_level = 0

    def _expire_forces(self, now: float) -> None:
        if self._force_release_until and now >= self._force_release_until:
            self._force_release_until = 0.0
        if self._force_engage_until and now >= self._force_engage_until:
            self._force_engage_until = 0.0

    def _expire_infeasible(self, now: float) -> None:
        expired = [m for m, ttl in self._infeasible.items() if now >= ttl]
        for m in expired:
            self._infeasible.pop(m, None)
            self._latch_baseline.pop(m, None)

    def _latch_retry_after(self, now: float, model: str) -> float:
        return max(0.0, self._infeasible.get(model, now) - now)

    def _dec(self, action: Literal["proceed", "stall", "shed"], reason: str, retry: float) -> BrakeDecision:
        return BrakeDecision(action=action, reason=reason, retry_after_s=retry)

    def _throttle_or_reopen(self, now: float, allow_proceed: bool) -> BrakeDecision:
        """Shared CLOSED/THROTTLED/HALF_OPEN-settling gate."""
        if not self._spacing_ok(now):
            if self._state == BrakeState.CLOSED:
                self._enter(BrakeState.THROTTLED, now)
            return self._dec("stall", "min-spacing", self._spacing_remaining(now))
        if self._tokens < 1.0:
            if self._state == BrakeState.CLOSED:
                self._enter(BrakeState.THROTTLED, now)
            if self._empty_since is None:
                self._empty_since = now
            elif (now - self._empty_since) >= self._cfg.min_state_hold_seconds:
                self._open(now)  # sustained over-demand ⇒ hard pause (or re-open from HALF_OPEN)
                return self._dec("stall", "swap brake OPEN (sustained over-demand)",
                                 max(0.0, self._brake_until - now))
            return self._dec("stall", "token bucket empty", self._tokens_eta(now))
        # tokens available + spacing ok
        self._empty_since = None
        if not allow_proceed:
            # HALF_OPEN settling but rate hysteresis not yet satisfied
            return self._dec("stall", "half-open settling", 0.0)
        if self._state == BrakeState.THROTTLED:
            self._enter(BrakeState.CLOSED, now)
        self._maybe_reset_backoff(now)
        return self._dec("proceed", "ok", 0.0)

    # ── public gate API ────────────────────────────────────────────────

    def acquire(self, model: str) -> BrakeDecision:
        """Authoritative gate (call INSIDE the load serializer). Never debits a token."""
        now = self._clock()
        self._refill(now)
        self._expire_forces(now)
        self._expire_infeasible(now)

        if not self._cfg.enabled:
            return self._dec("proceed", "brake disabled", 0.0)

        # Admin overrides (highest priority, auto-expiring).
        if self._force_engage_until and now < self._force_engage_until:
            return self._dec("stall", "force-engaged", max(0.0, self._force_engage_until - now))
        forced_release = bool(self._force_release_until and now < self._force_release_until)

        # Set-level infeasible latch — shed the offending candidate.
        if not forced_release and model in self._infeasible:
            return self._dec("shed", "demanded resident set exceeds VRAM capacity",
                             self._latch_retry_after(now, model))
        if forced_release:
            return self._dec("proceed", "force-released", 0.0)

        # OPEN → maybe HALF_OPEN (time-floor authoritative).
        if self._state == BrakeState.OPEN:
            if self._can_enter_halfopen(now):
                self._enter(BrakeState.HALF_OPEN, now)
                self._half_open_probe_used = False
                self._probe_outstanding = False
            else:
                return self._dec("stall", "swap brake OPEN (cooloff)",
                                 max(0.0, self._brake_until - now))

        # HALF_OPEN — exactly one probe.
        if self._state == BrakeState.HALF_OPEN:
            if self._probe_outstanding:
                return self._dec("stall", "half-open probe in flight", 0.0)
            if not self._half_open_probe_used:
                if not self._spacing_ok(now):
                    return self._dec("stall", "min-spacing", self._spacing_remaining(now))
                self._probe_outstanding = True
                return self._dec("proceed", "half-open probe", 0.0)
            # probe consumed: close, re-open, or keep settling
            if self._can_close(now):
                self._enter(BrakeState.CLOSED, now)
                self._maybe_reset_backoff(now)
                return self._throttle_or_reopen(now, allow_proceed=True)
            return self._throttle_or_reopen(now, allow_proceed=False)

        # CLOSED / THROTTLED
        return self._throttle_or_reopen(now, allow_proceed=True)

    def peek(self, model: str) -> BrakeDecision:
        """Pure, side-effect-free preview of ``acquire`` — for the pre-eviction early-reject."""
        now = self._clock()
        if not self._cfg.enabled:
            return self._dec("proceed", "brake disabled", 0.0)
        if self._force_engage_until and now < self._force_engage_until:
            return self._dec("stall", "force-engaged", max(0.0, self._force_engage_until - now))
        forced_release = bool(self._force_release_until and now < self._force_release_until)
        if not forced_release and model in self._infeasible and now < self._infeasible[model]:
            return self._dec("shed", "demanded resident set exceeds VRAM capacity",
                             self._latch_retry_after(now, model))
        if forced_release:
            return self._dec("proceed", "force-released", 0.0)
        if self._state == BrakeState.OPEN and now < self._brake_until:
            return self._dec("stall", "swap brake OPEN (cooloff)", max(0.0, self._brake_until - now))
        if not self._spacing_ok(now):
            return self._dec("stall", "min-spacing", self._spacing_remaining(now))
        if self._effective_tokens(now) < 1.0:
            return self._dec("stall", "token bucket empty", self._tokens_eta(now))
        return self._dec("proceed", "ok", 0.0)

    # ── recording API ──────────────────────────────────────────────────

    def record_load(self, model: str) -> None:
        """Debit a token for a completed BASTION-initiated cold load (at the GPU-I/O point)."""
        now = self._clock()
        self._refill(now)
        self._tokens = max(0.0, self._tokens - 1.0)
        self._last_load_t = now
        self._window.append(now)
        self._prune_window(now)
        if self._state == BrakeState.HALF_OPEN and self._probe_outstanding:
            self._probe_outstanding = False
            self._half_open_probe_used = True

    def record_unload(self, model: str) -> None:
        """Debit a token for a BASTION-initiated eviction (does NOT gate min-spacing)."""
        if not self._cfg.count_evictions:
            return
        now = self._clock()
        self._refill(now)
        self._tokens = max(0.0, self._tokens - 1.0)
        self._window.append(now)
        self._prune_window(now)

    # ── infeasible-set latch (F4) ──────────────────────────────────────

    def note_infeasible(self, model: str) -> None:
        """Latch a candidate whose load would require evicting an externally pinned model."""
        now = self._clock()
        self._infeasible[model] = now + self._cfg.infeasible_window_seconds  # monotonic TTL backstop
        self._latch_baseline[model] = self._last_resident or frozenset()

    def clear_on_residency_delta(self, resident: set[str]) -> None:
        """Clear latches when the resident set changes (never on a pure time advance)."""
        rs = frozenset(resident)
        for model, baseline in list(self._latch_baseline.items()):
            if rs != baseline:
                self._infeasible.pop(model, None)
                self._latch_baseline.pop(model, None)
        self._last_resident = rs

    def is_latched(self, model: str) -> bool:
        return model in self._infeasible and self._clock() < self._infeasible[model]

    # ── hardware-degrade, drain, admin override ────────────────────────

    def set_hw_degraded(self, blind: bool) -> None:
        """Tighten refill when the nvidia-smi hardware gate is blind (F5)."""
        now = self._clock()
        self._refill(now)  # settle accrued tokens at the old rate before switching
        self._hw_degraded = blind

    def set_drain(self, active: bool) -> None:
        """Hold brake state during drain — a drain-induced zero rate must not read as 'storm over'."""
        self._drain_active = active

    def force(self, release: bool, ttl_s: float) -> None:
        """Auto-expiring admin override. force-release cannot be left on silently."""
        now = self._clock()
        if release:
            self._force_release_until = now + ttl_s
            self._force_engage_until = 0.0
        else:
            self._force_engage_until = now + ttl_s
            self._force_release_until = 0.0

    def seed_just_swapped(self) -> None:
        """Seed 'just swapped' at startup so a post-restart first swap is spaced, not free."""
        self._last_load_t = self._clock()

    # ── observability ──────────────────────────────────────────────────

    def snapshot(self) -> dict:
        now = self._clock()
        return {
            "state": self._state,
            "reason": self._state.value,
            "cooloff_remaining_s": max(0.0, self._brake_until - now) if self._state == BrakeState.OPEN else 0.0,
            "windowed_rate_per_min": self._windowed_rate_per_min(now),
            "backoff_level": self._backoff_level,
            "tokens": round(self._effective_tokens(now), 4),
            "hardware_gate_blind": self._hw_degraded,
            "drain_active": self._drain_active,
            "latched": sorted(m for m, ttl in self._infeasible.items() if now < ttl),
            "force_release_active": bool(self._force_release_until and now < self._force_release_until),
            "force_engage_active": bool(self._force_engage_until and now < self._force_engage_until),
        }
