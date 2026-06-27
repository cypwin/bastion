"""SwapBrake unit tests — deterministic, no GPU, no asyncio (injected FakeClock).

Covers the spec §5 test matrix and every adversarial guardrail folded into the
plan (T2/T3/T4): min-spacing, token bucket, CLOSED→THROTTLED→OPEN→HALF_OPEN state
machine, backoff + reset, time-floor-authoritative probe, drain hold-state,
hysteresis, monotonic backward-step safety, two-token eviction accounting,
candidate-keyed set-level infeasible latch, hw-degraded refill, auto-expiring
force override, snapshot, and restart "just-swapped" seeding.
"""

from __future__ import annotations

from bastion.models import SwapBrakeConfig
from bastion.swapbrake import BrakeState, SwapBrake


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cfg(**overrides: object) -> SwapBrakeConfig:
    return SwapBrakeConfig(**overrides)  # type: ignore[arg-type]


def _brake(clock: FakeClock, **overrides: object) -> SwapBrake:
    return SwapBrake(_cfg(**overrides), clock=clock)


# ---------------------------------------------------------------------------
# T2 — min-spacing floor
# ---------------------------------------------------------------------------


class TestMinSpacing:
    def test_spacing_blocks_second_load_until_floor(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        assert b.acquire("m").action == "proceed"
        b.record_load("m")
        clk.advance(7.9)
        assert b.acquire("m").action == "stall"
        clk.advance(0.1)  # now exactly 8.0s
        assert b.acquire("m").action == "proceed"

    def test_first_load_proceeds_when_unseeded(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        assert b.acquire("m").action == "proceed"

    def test_note_load_issued_advances_spacing_without_token_debit(self) -> None:
        # NH — the inrush transient happens when a cold load is ISSUED, not when it
        # succeeds. So the spacing floor must advance at issue even if the load then
        # FAILS (no token debit on failure → that stays on record_load success).
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        tokens_before = b.snapshot()["tokens"]
        b.note_load_issued("m")
        # Spacing now in effect — a second issue within the floor stalls.
        d = b.acquire("m")
        assert d.action == "stall"
        assert d.reason == "min-spacing"
        # No token was debited (only record_load debits).
        assert b.snapshot()["tokens"] == tokens_before
        clk.advance(8.0)
        assert b.acquire("m").action == "proceed"


# ---------------------------------------------------------------------------
# T2 — token bucket (spacing disabled to isolate the bucket)
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_bucket_drains_then_refills(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=3.0, refill_per_minute=5.0)
        for _ in range(3):
            assert b.acquire("m").action == "proceed"
            b.record_load("m")
        # bucket now empty -> stall
        assert b.acquire("m").action == "stall"
        # refill is 5/min = 1 token / 12s
        clk.advance(12.0)
        assert b.acquire("m").action == "proceed"

    def test_acquire_never_debits(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=3.0)
        # many acquires without record_load must not drain the bucket
        for _ in range(10):
            assert b.acquire("m").action == "proceed"
        assert b.snapshot()["tokens"] >= 2.99


# ---------------------------------------------------------------------------
# T2 — state machine: OPEN hold, HALF_OPEN single probe, re-open backoff, close
# ---------------------------------------------------------------------------


class TestStateMachine:
    def _drive_to_open(self, clk: FakeClock, b: SwapBrake) -> None:
        # drain the bucket then keep demanding until OPEN escalation
        for _ in range(3):
            b.acquire("m")
            b.record_load("m")
        # sustained empty demand for min_state_hold -> OPEN
        for _ in range(60):
            b.acquire("m")
            clk.advance(0.1)
        assert b.snapshot()["state"] == BrakeState.OPEN

    def test_open_holds_for_cooloff_then_halfopen_one_probe(self) -> None:
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=3.0, refill_per_minute=0.0,
            cooloff_seconds=30.0, min_state_hold_seconds=5.0, release_rate_per_minute=3.0,
        )
        self._drive_to_open(clk, b)
        assert b.acquire("m").action == "stall"  # still OPEN within cooloff
        clk.advance(31.0)  # past cooloff + min_state_hold
        # window has stale loads; with refill 0 the windowed rate decays as they age out (>60s)
        clk.advance(60.0)
        first = b.acquire("m")
        assert first.action == "proceed"  # HALF_OPEN grants exactly one probe
        # a second acquire before the probe is recorded must NOT grant another probe
        assert b.acquire("m").action == "stall"

    def test_time_floor_grants_probe_even_when_rate_high(self) -> None:
        # R3-5: a never-draining queue keeps rate high, but the time-floor must
        # still grant the single probe so the brake can never permanently wedge.
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=2.0, refill_per_minute=0.0,
            cooloff_seconds=10.0, min_state_hold_seconds=2.0, release_rate_per_minute=3.0,
        )
        self._drive_to_open(clk, b)
        # keep the window "hot" by NOT advancing past the 60s prune; advance just past cooloff
        clk.advance(12.0)
        # rate is still elevated (recent loads in window) yet probe must be granted
        assert b.acquire("m").action == "proceed"

    def test_resumed_storm_reopens_with_backoff(self) -> None:
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=2.0, refill_per_minute=0.0,
            cooloff_seconds=30.0, cooloff_backoff_max_seconds=60.0, min_state_hold_seconds=2.0,
        )
        self._drive_to_open(clk, b)
        snap1 = b.snapshot()
        assert snap1["backoff_level"] >= 1
        clk.advance(91.0)  # past cooloff + window prune
        b.acquire("m")  # half-open probe
        b.record_load("m")
        # storm resumes: keep demanding with empty bucket -> re-OPEN, deeper backoff
        for _ in range(60):
            b.acquire("m")
            clk.advance(0.1)
        snap2 = b.snapshot()
        assert snap2["state"] == BrakeState.OPEN
        assert snap2["backoff_level"] >= snap1["backoff_level"]

    def test_cooloff_is_capped_at_backoff_max(self) -> None:
        # NH-3 — exponential cooloff (cooloff_seconds·2^(n-1)) must saturate at
        # cooloff_backoff_max_seconds no matter how deep the backoff goes, so a long
        # storm can't extend the hard pause unboundedly (forgiving recovery).
        clk = FakeClock()
        b = _brake(clk, cooloff_seconds=10.0, cooloff_backoff_max_seconds=30.0)
        for _ in range(8):
            b._open(clk())  # each re-open deepens backoff: 10, 20, 40→30, 80→30, ...
        assert b._backoff_level == 8
        assert (b._brake_until - clk()) <= 30.0
        assert b.snapshot()["cooloff_remaining_s"] <= 30.0

    def test_backoff_resets_after_clean_closed_window(self) -> None:
        # NH-3 — after a clean CLOSED window (rate below release for min_state_hold)
        # the backoff ladder resets to 0, so an isolated past storm doesn't penalize a
        # later one with a stale-deep cooloff.
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=2.0, refill_per_minute=0.0,
            cooloff_seconds=10.0, min_state_hold_seconds=2.0, release_rate_per_minute=3.0,
        )
        self._drive_to_open(clk, b)
        assert b.snapshot()["backoff_level"] >= 1
        clk.advance(11.0)   # past cooloff
        clk.advance(60.0)   # prune the window so the rate reads low
        assert b.acquire("m").action == "proceed"  # HALF_OPEN probe granted
        b.record_load("m")                          # probe succeeds
        b._tokens = float(b._cfg.bucket_capacity)   # refill so CLOSE doesn't re-throttle
        clk.advance(3.0)
        b.acquire("m")      # probe consumed → CLOSE
        clk.advance(3.0)
        b.acquire("m")      # clean CLOSED window → backoff reset
        assert b.snapshot()["backoff_level"] == 0


# ---------------------------------------------------------------------------
# T2 — drain hold-state (R3-1)
# ---------------------------------------------------------------------------


class TestDrainHoldState:
    def test_drain_blocks_auto_release(self) -> None:
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=2.0, refill_per_minute=0.0,
            cooloff_seconds=10.0, min_state_hold_seconds=2.0, release_rate_per_minute=99.0,
        )
        for _ in range(2):
            b.acquire("m")
            b.record_load("m")
        for _ in range(40):
            b.acquire("m")
            clk.advance(0.1)
        assert b.snapshot()["state"] == BrakeState.OPEN
        b.set_drain(True)
        clk.advance(100.0)  # well past cooloff with zero new loads
        assert b.acquire("m").action == "stall"  # drain holds the brake engaged
        assert b.snapshot()["state"] == BrakeState.OPEN
        b.set_drain(False)
        assert b.acquire("m").action == "proceed"  # release path re-enabled


# ---------------------------------------------------------------------------
# T2 — monotonic backward-step safety
# ---------------------------------------------------------------------------


class TestMonotonicSafety:
    def test_backward_clock_step_does_not_unbrake(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        b.acquire("m")
        b.record_load("m")
        clk.t -= 5.0  # backward step (NTP / suspend)
        # delta clamped to >=0; spacing must NOT be considered satisfied
        assert b.acquire("m").action == "stall"


# ---------------------------------------------------------------------------
# T2 — peek is pure (no side effects) — used by the scheduler pre-gate
# ---------------------------------------------------------------------------


class TestPeekPurity:
    def test_peek_does_not_mutate(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=3.0)
        before = b.snapshot()
        for _ in range(5):
            b.peek("m")
        after = b.snapshot()
        assert before["tokens"] == after["tokens"]
        assert before["state"] == after["state"]


# ---------------------------------------------------------------------------
# T3 — eviction two-token accounting + min-spacing gates loads only
# ---------------------------------------------------------------------------


class TestEvictionAccounting:
    def test_record_unload_debits_token(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=3.0, count_evictions=True)
        b.record_unload("m")
        assert b.snapshot()["tokens"] <= 2.01

    def test_record_unload_noop_when_count_evictions_false(self) -> None:
        clk = FakeClock()
        b = _brake(clk, bucket_capacity=3.0, count_evictions=False)
        b.record_unload("m")
        assert b.snapshot()["tokens"] >= 2.99

    def test_eviction_does_not_gate_spacing(self) -> None:
        # a multi-evict swap must not self-deadlock against its own spacing floor
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        b.record_unload("a")
        b.record_unload("b")
        # an eviction does NOT set the load-spacing clock, so a load can proceed
        assert b.acquire("c").action == "proceed"


# ---------------------------------------------------------------------------
# T3 — candidate-keyed set-level infeasible latch
# ---------------------------------------------------------------------------


class TestInfeasibleLatch:
    def test_latched_candidate_sheds(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0)
        b.clear_on_residency_delta({"trio_a", "trio_b"})  # establish baseline
        b.note_infeasible("big27b")
        d = b.acquire("big27b")
        assert d.action == "shed"
        assert "exceeds" in d.reason.lower() or "capacity" in d.reason.lower()
        # a different, feasible model is unaffected
        assert b.acquire("small").action == "proceed"

    def test_latch_clears_on_residency_delta(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0)
        b.clear_on_residency_delta({"trio_a", "trio_b"})
        b.note_infeasible("big27b")
        assert b.acquire("big27b").action == "shed"
        # residency changed (a pin dropped) -> latch clears, candidate can proceed
        b.clear_on_residency_delta({"trio_a"})
        assert b.acquire("big27b").action == "proceed"

    def test_latch_never_clears_on_pure_time(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0, infeasible_window_seconds=120.0)
        b.clear_on_residency_delta({"trio_a"})
        b.note_infeasible("big27b")
        clk.advance(60.0)  # time passes, no residency delta
        assert b.acquire("big27b").action == "shed"

    def test_latch_ttl_backstop_clears(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0, infeasible_window_seconds=120.0)
        b.clear_on_residency_delta({"trio_a"})
        b.note_infeasible("big27b")
        clk.advance(121.0)  # past the TTL backstop
        assert b.acquire("big27b").action == "proceed"

    def test_force_release_does_not_re_authorize_latched_infeasible(self) -> None:
        # NH — force-release DISABLES the velocity brake but must NOT re-authorize
        # evicting a caller's pin: a latched-infeasible candidate still SHEDS, on
        # both acquire() and peek(). (A pin evicted by a force-released swap is the
        # exact storm restart the latch exists to prevent.)
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0)
        b.clear_on_residency_delta({"trio_a", "trio_b"})
        b.note_infeasible("big27b")
        b.force(release=True, ttl_s=300.0)
        # A non-latched model is force-released through (brake disabled)...
        proceed = b.acquire("small")
        assert proceed.action == "proceed"
        assert proceed.reason == "force-released"
        # ...but the latched-infeasible candidate STILL sheds (pin protection holds).
        assert b.acquire("big27b").action == "shed"
        assert b.peek("big27b").action == "shed"


# ---------------------------------------------------------------------------
# T4 — hw-degraded refill, force override, snapshot, restart seeding
# ---------------------------------------------------------------------------


class TestDegradedRefill:
    def test_degraded_halves_refill(self) -> None:
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=4.0, refill_per_minute=60.0,
            degraded_refill_factor=0.5,
        )
        for _ in range(4):
            b.acquire("m")
            b.record_load("m")  # bucket -> 0
        b.set_hw_degraded(True)
        clk.advance(1.0)  # normal refill 60/min = 1/s; degraded = 0.5/s
        tokens = b.snapshot()["tokens"]
        assert 0.4 <= tokens <= 0.6


class TestForceOverride:
    def test_force_release_auto_expires(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        b.acquire("m")
        b.record_load("m")
        assert b.acquire("m").action == "stall"  # spacing
        b.force(release=True, ttl_s=5.0)
        assert b.acquire("m").action == "proceed"  # override
        clk.advance(6.0)
        # override expired AND spacing now satisfied (>8s) -> proceed for spacing,
        # so assert the override itself is gone via snapshot
        assert b.snapshot().get("force_release_active") is False

    def test_force_engage_holds(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0)
        b.force(release=False, ttl_s=10.0)
        assert b.acquire("m").action == "stall"
        clk.advance(11.0)
        assert b.acquire("m").action == "proceed"

    def test_force_release_ttl_is_clamped_to_max(self) -> None:
        # F-5 — an absurd ttl_s must NOT disable the backstop indefinitely; the
        # brake clamps force-release to force_release_max_ttl_seconds (defence in
        # depth: the backstop protects itself even if the server clamp is bypassed).
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0,
                   force_release_max_ttl_seconds=600.0)
        b.force(release=True, ttl_s=1e12)  # ~31000 years — would silently disarm
        snap = b.snapshot()
        assert snap["force_release_active"] is True
        assert snap["force_release_remaining_s"] <= 600.0
        assert snap["force_release_remaining_s"] > 599.0
        # the override still auto-expires at the clamped horizon
        clk.advance(601.0)
        assert b.snapshot()["force_release_active"] is False

    def test_force_engage_ttl_is_not_clamped(self) -> None:
        # Force-ENGAGE (keeps the brake on) fails SAFE; only force-RELEASE is bounded.
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0,
                   force_release_max_ttl_seconds=600.0)
        b.force(release=False, ttl_s=5000.0)
        clk.advance(601.0)  # past the release cap, but engage is not clamped
        assert b.acquire("m").reason == "force-engaged"


class TestSnapshot:
    def test_snapshot_exposes_observability_fields(self) -> None:
        clk = FakeClock()
        b = _brake(clk)
        snap = b.snapshot()
        for key in (
            "state", "reason", "cooloff_remaining_s", "windowed_rate_per_min",
            "backoff_level", "tokens", "hardware_gate_blind", "latched",
            "force_release_active", "force_release_remaining_s",
        ):
            assert key in snap

    def test_snapshot_force_release_remaining_counts_down(self) -> None:
        # F-5 — operators need to see HOW LONG the backstop stays disabled.
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=100.0)
        assert b.snapshot()["force_release_remaining_s"] == 0.0  # no override
        b.force(release=True, ttl_s=300.0)
        assert abs(b.snapshot()["force_release_remaining_s"] - 300.0) < 0.5
        clk.advance(100.0)
        assert abs(b.snapshot()["force_release_remaining_s"] - 200.0) < 0.5


class TestRestartSeeding:
    def test_seed_just_swapped_denies_free_first_swap(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=8.0, bucket_capacity=100.0)
        b.seed_just_swapped()
        # the first post-restart acquire must STALL within min_spacing (no free swap)
        assert b.acquire("m").action == "stall"
        clk.advance(8.0)
        assert b.acquire("m").action == "proceed"


# ---------------------------------------------------------------------------
# F-1 [BLOCKER] — abort_probe recovers a wedged HALF_OPEN probe
# ---------------------------------------------------------------------------


class TestAbortProbe:
    """A granted HALF_OPEN probe whose load never records (queue swept by TTL →
    dequeue None, or _dispatch raised) must NOT wedge the brake forever: only
    record_load/_open clear _probe_outstanding, so without recovery every later
    acquire short-circuits at 'half-open probe in flight' and all swaps brick
    until restart. abort_probe() re-OPENs so the cooloff ladder re-arms a fresh
    probe (fails SAFE — stalled swaps = no inrush — while restoring liveness)."""

    def _grant_orphan_probe(self, clk: FakeClock, b: SwapBrake) -> None:
        # Drive CLOSED→OPEN (drain bucket, then sustained empty demand).
        for _ in range(3):
            b.acquire("m")
            b.record_load("m")
        for _ in range(60):
            b.acquire("m")
            clk.advance(0.1)
        assert b.snapshot()["state"] == BrakeState.OPEN
        # Past cooloff + window prune → HALF_OPEN grants the single probe.
        clk.advance(31.0)
        clk.advance(60.0)
        assert b.acquire("m").action == "proceed"  # probe granted; _probe_outstanding=True

    def test_abort_probe_recovers_a_wedged_halfopen_probe(self) -> None:
        clk = FakeClock()
        b = _brake(
            clk, min_spacing_seconds=0.0, bucket_capacity=3.0, refill_per_minute=0.0,
            cooloff_seconds=30.0, min_state_hold_seconds=5.0, release_rate_per_minute=3.0,
        )
        self._grant_orphan_probe(clk, b)
        # The probe's load never records → every later acquire is wedged.
        assert b.acquire("m").reason == "half-open probe in flight"
        # Recover: abort the orphaned probe → re-OPEN (not stuck in HALF_OPEN).
        b.abort_probe()
        assert b.snapshot()["state"] == BrakeState.OPEN
        # After another cooloff the brake re-arms a fresh probe — liveness restored.
        clk.advance(91.0)
        assert b.acquire("m").action == "proceed"

    def test_abort_probe_is_safe_noop_without_outstanding_probe(self) -> None:
        clk = FakeClock()
        b = _brake(clk, min_spacing_seconds=0.0, bucket_capacity=3.0)
        # CLOSED, no probe in flight → abort_probe must not perturb a healthy brake.
        b.abort_probe()
        assert b.snapshot()["state"] == BrakeState.CLOSED
        assert b.acquire("m").action == "proceed"


# ---------------------------------------------------------------------------
# F-4 — numeric brake-state gauge mapping (feeds bastion_swap_brake_state)
# ---------------------------------------------------------------------------


class TestBrakeStateGauge:
    def test_gauge_value_is_severity_ascending(self) -> None:
        # Higher = more engaged, so a dashboard can alert by thresholding the gauge.
        assert BrakeState.CLOSED.gauge_value == 0.0
        assert BrakeState.THROTTLED.gauge_value == 1.0
        assert BrakeState.HALF_OPEN.gauge_value == 2.0
        assert BrakeState.OPEN.gauge_value == 3.0
        vals = [
            BrakeState.CLOSED.gauge_value, BrakeState.THROTTLED.gauge_value,
            BrakeState.HALF_OPEN.gauge_value, BrakeState.OPEN.gauge_value,
        ]
        assert vals == sorted(vals)  # strictly increasing with engagement severity
