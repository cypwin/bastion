# BASTION Swap-Velocity Circuit Breaker + F1–F6 Defense-in-Depth — Design

> **Status:** Implemented on `feat/swap-velocity-circuit-breaker`; all 5 §9 must-fixes + 1
> review-found follow-up landed test-first (see §9.1), full suite 1972 passed / 0 new regressions —
> ready to merge/PR.
> **Date:** 2026-06-26
> **Trigger:** Host hard-lockup 2026-06-26 ~15:20–15:21 during a model-swap storm.
> **Scope decision:** Full F1–F6 sweep in a single PR (in-memory state only; no on-disk breadcrumb).
> **Origin:** Multi-lens design council (6 lenses → 6 adversarial cross-exams → synthesis) +
> three engineer-verified refinements. Cross-session forensics from the SWARM BRAIN incident review.

---

## 0. Reframing (the one sentence that drives every choice)

BASTION's logical VRAM bin-packer is **correct**; the crash came from the **velocity of correct swap
decisions** plus BASTION **fighting Ollama's `keep_alive=-1` pins in an expiry-timer namespace it
cannot see** (eviction = `POST /api/generate {keep_alive:0}` at `vram.py:463-466`, the exact inverse
of the caller's pin — last-writer-wins). The fix is therefore **one new mechanism, not six new
numbers**: a **sensor-independent swap-velocity brake** that counts BASTION's *own* residency
transitions on a **monotonic clock**, paired with a **sticky "infeasible resident set" latch** that
*stops issuing evictions* once pinned demand provably overruns the budget. The brake is the
load-bearing backstop precisely because it keeps working when every `nvidia-smi` / `/api/ps` sensor is
dark — which is when the host is most likely to die.

### Why robustness is non-negotiable (incident context)

The 2026-06-26 crash was the failure of a **three-gate defense**, two of which had *silently* failed:

1. **BASTION** (this package) — working as designed, with the category gap above. The last line.
2. **OllamaClient VRAM gate** (`check_model_loadable`, caller-side) — **dead code**: imported modules
   that no longer exist after a monolith→peer migration (`ModuleNotFoundError` → silent no-op).
3. **Jury `_preflight_vram_fit`** (caller-side) — **live but toothless** (`strict=False` → logs, never
   raises; checked one model-set, never the combined footprint).

A purpose-built `ModelLoadingGate` (`max_total_vram_gb: 22`, `allowed_combinations` that explicitly
forbid the trio+27B footprint) existed and was **unplugged by import drift**. The decisive lesson:
**BASTION cannot assume any upstream gate protects anything.** Every mechanism in this design is
therefore (a) self-sufficient, (b) **fail-LOUD** (the two upstream failures were silent-degrade — the
anti-pattern this design must not replicate, and the one `_hardware_admits` currently embodies), and
(c) version/sensor-independent in its load-bearing core.

---

## 1. The ONE circuit-breaker mechanism

**A 3-layer hybrid, sensor-independent, evaluated synchronously at the single load chokepoint.**

1. **Absolute min-spacing floor** between cold *loads* (the inrush guarantee). Default **8.0 s** ⇒ a
   hard 7.5 loads/min instantaneous ceiling, **below the stated >8/min crash zone** — so spacing
   *alone* can never enter the crash zone even if every other parameter is misconfigured.
   *(Ratified: 8.0 s default, auto-lowered by `--stress-test` calibration.)*
2. **Token bucket** (capacity = burst tolerance, refill = sustained safe velocity) bounding the
   long-run average; drains *during* a burst — unlike the current trailing-60 s window that trips one
   event too late (F1's defect).
3. **`CLOSED → THROTTLED → OPEN → HALF_OPEN`** state machine turning the throttle into a hard
   **pause** (F2), with min-state-hold + exponential-backoff anti-flap.

**Why this, not the alternatives.** Pure sliding-window is reactive (6 swaps at 2 s land inside ~12 s
before the count hits 6). Pure token-bucket permits a sub-second burst that drains all credits inside
the inrush-dangerous interval. Pure min-spacing permits an unbounded sustained `1/spacing` rate
forever. Only the composition bounds **instantaneous succession**, **sustained average**, and provides
a **hard stop** in the crash zone.

### 1.1 Concurrency model — the chokepoint is the load semaphore (verified)

`vram.VRAMManager._load_semaphore` (`asyncio.Semaphore(1)`) is acquired in **exactly one place today**
— `scheduler.py:646`. Verification found a **live hole**: `/broker/preload` (`server.py:1968` *and*
the two-port-mode duplicate at `2834`) loads by **POSTing `/api/generate` to Ollama directly**
(`server.py:1986`, `2848`), bypassing both the scheduler *and* the load semaphore — an unbraked,
unserialized residency-increasing path. A2A preload is already safe (it enqueues a `QueuedRequest` →
scheduler → semaphore, `a2a.py:1363`).

**Resolution — single enforced chokepoint:**

- The **load semaphore is THE serialization point.** The brake's **authoritative** check-and-record
  (`acquire()` returning proceed, then `record_load()`) happens **inside** the `async with
  _load_semaphore` block, on `time.monotonic()`.
- **Every** direct-load path — scheduler swap and **both `/broker/preload` routes** — must acquire the
  load semaphore and call the brake inside it. The cheap **pre-eviction `acquire()`** (before
  `_evict_for_model`) stays as an early-reject so a doomed swap never evicts; the in-semaphore check is
  authoritative and closes the TOCTOU (two tasks can no longer both pass on stale state).
- If the in-semaphore check says "stall", we **do not sleep inside the semaphore** (that would block
  every load): release the reservation, release the semaphore, return `False` → the scheduler retries
  next tick. The min-spacing wait is realized by tick retries, never by holding the serializer.
- **Regression test (mandatory):** fail the build if any code path issues a model-loading
  `/api/generate` (i.e. without `keep_alive:0`) without holding `_load_semaphore`.

**State ownership (no new lock).** The scheduler is a single serialized `_loop` task; the brake's
POLICY (`state`, `brake_until`, backoff level, token count) lives as plain attributes on a `SwapBrake`
object owned by the scheduler. **All `SwapBrake` methods are strictly synchronous (zero `await`)**, so
each call is atomic under asyncio even when a second task (a preload coroutine) calls it; the load
semaphore serializes the multi-step acquire→load→record sequence. Admin override is a single atomic
boolean the scheduler **reads** — never a cross-task mutation of the state machine.

### 1.2 New module `src/bastion/swapbrake.py`

```python
from __future__ import annotations
class BrakeState(StrEnum): CLOSED; THROTTLED; OPEN; HALF_OPEN
@dataclass
class BrakeDecision: action: Literal["proceed", "stall", "shed"]; reason: str; retry_after_s: float
class SwapBrake:
    def __init__(self, cfg: SwapBrakeConfig, clock: Callable[[], float] = time.monotonic) -> None: ...
    # all sync, no await:
    def acquire(self, model: str) -> BrakeDecision          # gate; does NOT consume a token
    def record_load(self, model: str) -> None               # debit token at GPU-I/O point
    def record_unload(self, model: str) -> None             # BASTION-initiated eviction transition
    def note_infeasible(self, model: str) -> None           # latch sticky REFUSE for a candidate
    def clear_on_residency_delta(self, resident: set[str]) -> None
    def set_hw_degraded(self, blind: bool) -> None          # tighten refill when nvidia-smi dark
    def force(self, release: bool, ttl_s: float) -> None     # auto-expiring admin override
    def snapshot(self) -> dict                               # /broker/status + gauges
```

`clock` injection is the entire deterministic-testing story (§4).

---

## 2. Per-finding changes

### F1 — Demote the cooldown ladder to advisory; min-spacing floor + monotonic clock
- **Files:** `scheduler.py:_get_swap_cooldown` (~150); switch wall-clock → monotonic at the six
  **swap-timing** sites **161, 354, 441, 455, 498, 602–603**. These are all `_last_swap_time` deltas +
  the `_swap_timestamps` window; they are internally consistent (no display exposure). The separate
  `_last_stall_time` display stamp (467) stays **wall-clock**.
- **Mechanism:** keep the rolling window + level transitions for the `swap_rate` audit event and a new
  `bastion_swap_rate_per_min` gauge, but **its return value no longer gates** — go/no-go routes through
  `SwapBrake`. `cooldown_seconds` keeps its 2.0 s meaning for *co-resident* transitions only (which
  skip the brake); the brake's `min_spacing_seconds` owns cold-load pacing.
- **Config (new, nested `scheduler.swap_brake`):** `min_spacing_seconds: float = 8.0`.
- **Why monotonic is a prerequisite, not a nicety:** a backward NTP step / S3-resume on `time.time()`
  makes the trailing window read ~0 swaps and **silently disables the throttle**. All brake deltas are
  clamped `max(0.0, now - last)`.
- **Effort:** medium.

### F2 — The hard brake (this finding *is* the circuit breaker — full spec in §3)
- **Files:** new `swapbrake.py`; wired as the **first gate** in `scheduler.py:_handle_swap_dispatch`
  (~489), replacing the inline cooldown block at **496–513**. Authoritative `record_load` debit lives
  **inside** the `async with self.vram_manager._load_semaphore` block (~646); proactive eviction
  (606–632) gated behind the same pre-`acquire`.
- **Effort:** max (the bulk of the code and tests).

### F3 — Do NOT promote the per-agent thrashing detector (all six lenses + adversary agree)
- **Files:** `thrashing.py` HALT gate at **128 & 146** stays behind `mode=="strict"` — **no behavioral
  change**; `broker.yaml:thrashing_detection.mode` stays `"warn"`.
- **Rationale:** it is request-admission, per-agent (`X-Agent-Id`/IP) — structurally blind to a
  *system-wide* power event from one well-behaved calibration loop, and promoting it would pull
  request-task-mutated state into the scheduler task. Document the division of labor in the docstring.
- **Optional defense-in-depth (off by default):** feed aggregate `WARN` count as a soft input that
  lowers `SwapBrake` effective refill.
- **Also:** scrub RTX-5090 provenance from `thrashing.py:1-9` and `broker.yaml:300-308`.
- **Effort:** low.

### F4 — Detect the infeasible pinned set behaviorally; stop fighting the pin
- **Files:** `vram.py:get_loaded_models` (~310) captures the two fields currently dropped —
  `expires_at` and `size_vram` — onto `LoadedModel` (`models.py:455`); maintains `VRAMTracker._pinned`.
  `scheduler.py:_evict_for_model` (680–691) adds `and m.name not in self.vram._pinned` to the evictable
  filter (alongside `always_allowed`/reservation/in-flight). Behavioral detector promotes the existing
  `_eviction_stuck_streak` (scheduler.py:730, today log-only).
- **Two detectors, behavioral is PRIMARY:**
  1. **Behavioral, version-independent, fail-closed (primary):** count same-model evict→reload
     oscillations (`_evict_reload_history: dict[str, deque[float]]`, fed on `_unload_model` success +
     next-tick reappearance). ≥ `infeasible_evict_reload_threshold` within `infeasible_window_seconds`
     ⇒ the pinned working set is overflowing.
  2. **Proactive, additive hint:** `expires_at` beyond `now + expires_horizon_seconds` ⇒ externally
     pinned; when `Σ(pinned size_vram) + candidate > max_vram_gb`, latch immediately.
- **Latch semantics — SET-LEVEL freeze (engineer refinement; the council's per-model `note_infeasible`
  was under-specified).** In the real storm *all three* sets were pinned (44 GB > budget) — there is no
  single aggressor and the *victim* of an eviction is a wanted, fitting model. Therefore:
  - Hold whatever **feasible resident subset** currently fits; never evict a `_pinned` model.
  - **Latch the CANDIDATE** whose load would require evicting a currently-resident pinned model — never
    the evicted victim. Shed that candidate's swap demand with `503 + Retry-After + machine-readable
    "demanded resident set exceeds VRAM capacity"`.
  - The latch is keyed on the *(pinned-set overflows budget)* condition, applied per non-resident
    candidate that collides with it.
- **Once latched:** STOP issuing `keep_alive=0` for the pinned set (the actual storm-stopper); emit a
  **continuous** named alert + `/broker/status` fields (`pinned_models`, `pinned_vram_gb` vs
  `max_vram_gb`) + `bastion_pinned_vram_gb` gauge. Latch **clears only on a real residency delta or a
  monotonic TTL — never on a cooloff timer** (else HALF_OPEN re-arms the storm one inrush per cooloff).
  Pair operator force-unload (`unload_model_admin`, `scheduler.py:837`) with "refuse this model's next
  loads" so the `keep_alive=0` force isn't instantly re-pinned by last-writer-wins.
- **Config (new, nested `scheduler.pin_detection`):** `enabled: bool = True`,
  `expires_horizon_seconds: float = 3600.0`. (in `swap_brake`:) `infeasible_evict_reload_threshold: int
  = 3`, `infeasible_window_seconds: float = 120.0`.
- **Subordinate to the brake:** if `expires_at` is absent/unparseable on the target Ollama build, pin
  detection degrades to the behavioral signature, never to "no protection."
- **Effort:** high.

### F5 — Config-drive the margin; path-split fail mode with miss-degrade; make blindness visible
- **Files:** `vram.py:32` `HARDWARE_MARGIN_GB` constant → `GPUConfig.hardware_margin_gb`; thread
  through `_hardware_admits` (43-63), `can_load_model` (429-444), `VRAMManager.reserve` (659-684).
  Publish `bastion_hardware_gate_blind_total` + a `/broker/status` boolean. Unify
  `VRAMManager.safety_margin_pct` with `hardware_margin_gb` into one budget computation
  (`max_vram_gb` already nets headroom) to avoid stacked margins.
- **Fail mode (ratified: `closed_on_swap` + miss-degrade):**
  - Steady-state reads **fail OPEN** (a flaky sensor must not DoS the broker / wedge non-NVIDIA &
    container hosts).
  - **Cold-swap path fails CLOSED for a *transient* miss** (blind on the dangerous path = stop), BUT
    after `hardware_gate_miss_degrade_after` consecutive misses it **stops fail-closing, logs a loud
    DEGRADED banner, and hands the floor to the (now-tightened) sensor-independent velocity brake** —
    because a *permanent* fail-closed converts a sensor outage into a swap outage.
    `SwapBrake.set_hw_degraded(True)` multiplies refill by `degraded_refill_factor`.
  - Treat a **stale or implausible** nvidia-smi reading like a miss (bound reading age; if reported free
    is below ledger-implied-used beyond convergence tolerance, route through
    `wait_for_vram_convergence`). Pin the query to the configured GPU id. Cap the nvidia-smi query well
    under the tick budget so a *hanging* smi can't stall the single scheduler loop.
- **Non-Ollama VRAM:** `non_ollama_reserve_gb: float = 0.0` subtracts compositor/framebuffer VRAM from
  the budget (the clean knob, vs inflating the percentage margin). *Defense-in-depth (this PR):*
  dynamic per-reconcile re-derivation `max(floor, last_good_nvml_used − ledger_allocated −
  ollama_resident)`, smoothed/clamped for `/api/ps` skew, so a new monitor/CUDA job is caught instead of
  frozen at the idle value.
- **Ledger accuracy:** prefer Ollama `size_vram` over disk-`size` in `get_loaded_models`.
- **Config:** `GPUConfig.hardware_margin_gb: float = 2.0` (promoted, behavior-preserving; documented
  "raise to 3–4 for compositor/multi-monitor GPUs"), `non_ollama_reserve_gb: float = 0.0`,
  `hardware_gate_fail_mode: str = "closed_on_swap"` (`"open"`|`"closed_on_swap"`),
  `hardware_gate_miss_degrade_after: int = 3`.
- **Demotion:** F5 is documented as a *best-effort cross-check, not the crash boundary*; the brake is.
  `swap_brake.enabled` defaults true and is hard to disable.
- **Effort:** medium.

### F6 — Concede ms inrush is unobservable in-band; publish the trend, control frequency
- **Files:** `health.py:check_gpu_safe` (~54) unchanged as the steady-state veto. Add
  `update_gpu_power_watts(status.power_draw_watts)` guarded on not-None, mirroring the temperature
  publish at **45-46** (power is currently computed then silently discarded).
- **Mechanism:** **no new in-band sensor** — a 5 s poll cannot see a ms transient; faster polling is
  theater. The transient mitigation *is* F2's `min_spacing_seconds`. Document the linkage in
  `reference/CRASH_ROOT_CAUSE.md` so no future maintainer "improves" it with faster polling.
- **Config:** gauges `bastion_gpu_power_watts`, `bastion_gpu_power_cap_watts`. *Optional
  defense-in-depth (off):* `gpu.power_headroom_pct: float = 0.0` — when >0, steady-state draw within
  headroom of the cap treats the bucket as empty.
- **Effort:** low.

---

## 3. Circuit-breaker full specification

**`SwapBrakeConfig` (nested `scheduler.swap_brake`), portable floors for an unknown card:**
```
enabled: bool = True                         # hard to disable; backstop for fail-open gates
min_spacing_seconds: float = 8.0             # cold-LOAD floor; 7.5/min instantaneous ceiling
bucket_capacity: float = 3.0                 # burst tolerance (calibrated: safe_burst_depth)
refill_per_minute: float = 5.0               # sustained safe velocity (calibrated: safe_swap_rate_per_min)
count_evictions: bool = True                 # BASTION-initiated unloads debit a token (2 events/swap)
cooloff_seconds: float = 30.0                # base OPEN hold
cooloff_backoff_max_seconds: float = 60.0    # exponential 30→60 cap (forgiving, not 120)
min_state_hold_seconds: float = 5.0          # anti tick-flap (loop runs at 0.1s)
release_rate_per_minute: float = 3.0         # hysteresis (< refill): anti-flap band
shed_when_infeasible: bool = True            # 503 doomed swaps; do not stall them
infeasible_evict_reload_threshold: int = 3
infeasible_window_seconds: float = 120.0
degraded_refill_factor: float = 0.5          # tighten when hardware gate blind (F5)
```
> **Two-token accounting (D4).** With `count_evictions: True`, each *swap* spends ~2 tokens (one
> evict + one load), so `refill_per_minute: 5.0` ⇒ **~2.5 sustained swaps/min** — comfortably below the
> >8/min crash zone. The two layers are complementary: **min-spacing (8 s ⇒ 7.5 loads/min)** is the
> instantaneous *inrush* floor; the **token bucket (~2.5 swaps/min)** is the tighter *sustained* binding
> constraint. Calibration writes both from measured hardware. Defaults must be read in swap units, not
> token units.

> **Ratified D3 (in-memory only):** *no* `persist_breadcrumb`. Restart safety is handled purely by
> startup "just-swapped" seeding (below). The on-disk backoff breadcrumb is explicitly **out of scope**.

- **Trigger:** `acquire()` returns **stall** when bucket < 1 token OR `min_spacing` not elapsed since
  last load. Escalates to **OPEN** when the bucket stays empty under live demand for one
  `min_state_hold`, OR immediately when a candidate is latched **INFEASIBLE** (then `acquire` returns
  **shed**). Counts **both** cold loads (recorded inside the load semaphore) and **BASTION-initiated**
  unloads (recorded in `_unload_model`) toward bucket/window — each is a real residency transition.
  **Min-spacing gates cold loads ONLY** (evictions never gate the spacing floor, else a multi-evict
  swap self-deadlocks). **External** (Ollama-timeout / reconcile) unloads are NOT recorded — only
  BASTION-initiated ones — so external churn cannot wedge release.
- **Cooloff / release:** OPEN holds ≥ `cooloff_seconds` (monotonic). Release requires **BOTH** the time
  floor elapsed AND windowed load-rate ≤ `release_rate_per_minute` AND `min_state_hold` satisfied; the
  **time floor is authoritative** (can never wedge on a sensor or a never-empty queue), the rate gate
  only prevents re-bursting. → HALF_OPEN grants exactly ONE probe, preferring a **feasible, non-latched
  model** (never spends the probe evicting a pinned set). CLOSE only after the probe succeeds *and* rate
  stays below release for `min_state_hold`; resumed storm → re-OPEN with exponential backoff capped at
  `cooloff_backoff_max_seconds`; backoff resets after a clean CLOSED window. While `drain` is active the
  brake **holds state and does not auto-release** (a drain-induced zero rate must not read as "storm
  over").
- **Queued-work (tiered — degrade, don't halt):** Phase-1 co-resident dispatch is **never** gated
  (resident models cause zero cold-load inrush). Swap-needing requests **STALL** by default
  (non-destructive `pick_next`-then-`return False`, the proven pattern at 500–513; priority-aging keeps
  running). **SHED (503 + Retry-After + reason)** only for: (a) **model-aware** demand for a
  *latched-infeasible* candidate (provably never servable — return immediately, `Retry-After` derived
  from the authoritative `brake_until`/latch, **not** token-refill), and (b) a backlog ceiling so a long
  brake can't silently accumulate stale work. Existing bounds stay (`queue_timeout_seconds`=300→504,
  `max_queue_size`=512→503). A **swap-starvation ceiling** (distinct from `queue_timeout`) lets the next
  freed in-flight slot evict for a swap-needed request that has starved while residents stayed busy via
  ungated Phase-1 traffic. Priority-aging is **snapshotted at brake-engage** for swap-needing requests
  so the single swap granted at release does not load a stale, age-inflated background model and
  re-trigger the storm.
- **Admission coupling (this PR):** every shed also throttles that caller/model in `ratelimit.py`, so a
  client ignoring `Retry-After` (the calibration loop *will*) cannot hot-retry the enqueue→503 path into
  a CPU busy-loop. (Protects against churn/DoS, not the crash — the brake already keeps the GPU safe.)
- **Fail mode:** **Fail-CLOSED and sensor-INDEPENDENT.** The brake reads only `time.monotonic()` +
  BASTION's own transition log, so an nvidia-smi / `/api/ps` outage cannot disable it — it stays on and
  *tightens* (degraded refill) exactly when the F5/F6 physical gates go blind. The pin sub-detector
  depends on `/api/ps`; on outage it degrades to the behavioral oscillation signature, never silently
  re-enabling eviction of caller pins. Admin override is a single atomic boolean the scheduler **reads**;
  **force-release auto-expires** (`force(release, ttl_s)`) so the backstop can't be silently left off.
- **Single chokepoint (enforced):** see §1.1 — load semaphore is the serialization point; brake
  authoritative inside it; both `/broker/preload` routes funnelled in; regression test guards new
  bypasses.
- **Restart safety (ratified in-memory):** seed brake state at startup as "just swapped"
  (`_sync_current_model`, `scheduler.py:883` — currently leaves `_last_swap_time=0.0` when no models are
  loaded, giving the first post-restart swap an *infinite* elapsed = a free swap into the crash zone
  while the caller's `keep_alive=-1` pins survive in Ollama; this is the crash-restart-loop hole).

**Observability (3am visibility):** `/broker/status` exposes brake `state`, `reason`,
`cooloff_remaining_s`, `windowed_rate_per_min`, `backoff_level`, `pinned_models`/`pinned_vram_gb`,
`hardware_gate_blind`. Gauges: `bastion_swap_brake_state`, `bastion_swap_brake_engaged_total`,
`bastion_swap_rate_per_min`, `bastion_pinned_vram_gb`, `bastion_hardware_gate_blind_total`,
`bastion_gpu_power_watts`, `bastion_gpu_power_cap_watts`. One distinct audit event on engage/release
with duration + swaps-during-brake; brake-induced 503/504 carry a reason code. New admin endpoint
`POST /broker/swap-brake` (force-release / force-engage, auto-expiring), separate from `drain`.

---

## 4. Portability & calibration (extends existing machinery — verified)

The calibration path **already exists** and is extended, not built:
- `config._load_gpu_profile` + `_apply_gpu_profile` (config.py:280/317) already load
  `~/.config/bastion/gpu-profile.yaml` and **already respect explicit user fields**;
  `resolve_gpu_defaults` already tracks the explicit-keys set; `stress.py` already calibrates and writes
  `safe_swap_rate_per_min` (`stress.py:288/310/327`).
- **New work (small):**
  - `stress.py` emits **`safe_burst_depth`** alongside `safe_swap_rate_per_min`; runs the swap-ramp
    phase **with the min-spacing brake active**, stops at the FIRST sign of instability, and writes the
    **LAST-KNOWN-SAFE** value (never the value at which it became unstable).
  - `_apply_gpu_profile` maps `safe_swap_rate_per_min → swap_brake.refill_per_minute` and
    `safe_burst_depth → swap_brake.bucket_capacity`, with **only-tighten precedence** (auto-derivation
    may tighten but never relax an explicitly-set operator value — extends the existing
    explicit-respect) and a loud audit line when a profile would relax an explicit value.
  - **Refuse to apply** a `gpu-profile.yaml` whose recorded GPU name ≠ the currently detected card;
    surface an "uncalibrated — running portable floor" / "profile stale for detected GPU" flag in
    `/broker/status`. Validate auto-detected `total_vram_gb`/`max_power_watts` against sanity bounds.
  - Enforce calibrator invariants: `refill ≥ 1` (no divide-by-zero), `warn_threshold < critical`
    (preserve hysteresis), clamped ratios.
- **Protection must NOT depend on calibration having run:** the sensor-independent brake stays enabled
  by default on the conservative portable floor, so an un-calibrated / non-NVIDIA host still has a
  working backstop.
- Promote every remaining bare constant to a typed Pydantic field and **scrub all RTX-5090 numerics
  from BOTH code and comments** (`thrashing.py:4-5`, `broker.yaml:300-308`, `scheduler.py:322`),
  replacing with "conservative floor for an unknown card; calibrate via `--stress-test`."

---

## 5. Testing strategy — deterministic, no GPU, injectable clock

The brake core is **pure-sync Python with an injected `clock`**, unit-tested with a hand-advanced fake
monotonic clock — zero asyncio, zero GPU:
```python
class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt
```
- **Min-spacing:** `acquire` at t=0 → proceed; `record_load`; `acquire` at t=7.9 → stall; advance to
  8.0 → proceed.
- **Bucket/sustained:** capacity 3, refill 5/min (1 token/12 s). Three `record_load` in-window → 4th
  `acquire` → stall→OPEN; advance 12 s → one token back.
- **State machine:** OPEN holds `cooloff_seconds`; HALF_OPEN grants exactly one probe; resumed storm
  re-OPENs with backoff 30→60 (cap); CLOSE only after probe + rate ≤ release for `min_state_hold`.
- **Hysteresis / metronome guard:** engage at refill-exceeded, release at ≤ release_rate; assert it
  cannot flap faster than `min_state_hold`.
- **Monotonic safety:** feed a backward clock step; assert no negative-elapsed un-brake (delta ≥ 0).
- **INFEASIBLE latch:** `note_infeasible` after N evict→reload records → `acquire` returns shed;
  `clear_on_residency_delta` with the candidate's collision resolved → proceeds. Assert the latch never
  clears on pure time advance alone (only residency delta / TTL). Assert the **candidate** is latched,
  not the evicted victim.
- **count_evictions:** `record_unload` debits a token; external unloads do not.
- **Degraded:** `set_hw_degraded(True)` halves effective refill.
- **Restart seeding:** a freshly constructed brake seeded "just swapped" stalls the first `acquire`
  within `min_spacing` (no free first swap).
- **Wiring (asyncio, StubBackend, fake clock):** a braked swap does **not** call `reserve` /
  `_evict_for_model`; `record_load` fires *inside* the load semaphore; **both preload routes** route
  through the brake + semaphore; the **funnel regression test** fails on a model-loading `/api/generate`
  issued without the semaphore.
- **F5 path-split:** transient miss on cold swap → refuse; after K misses → degrade to brake-governed
  open + DEGRADED banner + blind counter; stale/implausible reading treated as a miss.
- **Calibration:** `_apply_gpu_profile` only-tightens an explicit value and refuses a GPU-name mismatch.

Run via the Tier-0 `quiet-test.sh` wrapper; keep the destructive e2e behind `BASTION_E2E=1`.

---

## 6. Work breakdown (single PR; core committed & tested first)

| Area | Files | Notes |
|---|---|---|
| Brake core | **new** `swapbrake.py`, `models.py` (`SwapBrakeConfig`, `PinDetectionConfig`) | sync, injected clock, full §3 |
| Scheduler wiring | `scheduler.py` | monotonic switch (F1); brake gate + in-semaphore record (F2); proactive-eviction gate; behavioral infeasible latch (F4); pinned-exclusion; starvation ceiling; drain coord; restart seeding |
| VRAM | `vram.py`, `models.py` (`LoadedModel`) | parse `expires_at`/`size_vram`; `_pinned`; margin config + path-split fail mode + non-Ollama reserve + miss-degrade (F5); margin unification |
| Load-path funnel | `server.py` (both `/broker/preload`), `a2a.py` (verify) | acquire semaphore + brake inside it; regression test |
| Health/metrics | `health.py`, `metrics.py` | power gauge publish (F6); all new gauges |
| Admission | `ratelimit.py` | shed → admission throttle |
| Calibration | `stress.py`, `config.py`, `gpu_profiles.py` | `safe_burst_depth`; only-tighten; GPU-name match (§4) |
| Status/admin | `server.py`, `models.py` (`BrokerStatus`) | brake snapshot fields; `POST /broker/swap-brake` override |
| Docs/scrub | `thrashing.py`, `broker.yaml`, `reference/CRASH_ROOT_CAUSE.md` | division-of-labor; scrub RTX-5090 numerics; F6 rationale |
| Tests | `tests/test_swapbrake.py` (new) + extend `test_scheduler*`, `test_vram*`, `test_health.py` | §5 |

**Ordering within the PR:** (1) brake core + unit tests → (2) scheduler wiring + monotonic →
(3) load-path funnel + regression test → (4) F4 infeasible latch → (5) F5 hardware gate →
(6) calibration + portability → (7) F6/observability/admin → (8) docs/scrub. Each step lands green
before the next.

---

## 7. Decision record (ratified 2026-06-26)

| # | Decision | Choice |
|---|---|---|
| D1 | Min-spacing default | **8.0 s**, auto-lowered by `--stress-test` |
| D2 | nvidia-smi miss on cold swap | **`closed_on_swap` + miss-degrade** |
| D3 | Restart state vs in-memory mandate | **Seed "just-swapped" only** (no on-disk breadcrumb) |
| D4 | Token accounting | **Count both loads and BASTION-initiated unloads** (2 events/swap) |
| D5 | Ship scope | **Full F1–F6 sweep in one PR** (breadcrumb excluded per D3) |

---

## 8. Out of scope (this package / this PR)

- **Caller-side fix** (do not pin a >budget working set; `keep_alive=-1` only on an allowed
  combination): lives in `swarm_memory` / `swarm_orchestrator`. BASTION is designed here to make the
  storm *survivable regardless of caller behavior*; it does not fix the caller.
- **On-disk brake breadcrumb** (backoff persistence across restart): excluded by D3 (in-memory mandate).
  Revisit only if a flapping-host restart loop is observed in practice.
- **Multi-GPU swap accounting:** single-GPU seam today (`gpu_index=0`); the brake is per-process.

---

## 9. Post-implementation adversarial review — follow-ups (2026-06-26)

The full F1–F6 sweep was implemented on `feat/swap-velocity-circuit-breaker` (20 commits, 496 tests
green). A 5-lens adversarial review (concurrency, spec-correctness, completeness, safety, storm-trace)
returned **ship-after-must-fix**. **No finding is an active crash hole** — every defect fails safe
toward GPU safety, and the core 3-layer velocity brake (min-spacing + bucket + state machine) is sound
and unit-tested. The must-fixes harden *secondary* guarantees the spec treats as load-bearing — and
several are the exact "defined-but-unwired guardrail" anti-pattern §0 was written to prevent. Turnkey:

**MUST-FIX (1 blocker + 4 high):**
1. **[BLOCKER] HALF_OPEN probe can permanently wedge the brake.** `_probe_outstanding` is reset only by
   `record_load`/`_open`; the scheduler calls `record_load` only on dispatch *success* (`scheduler.py:928`).
   If a granted probe's load doesn't record (queue swept by TTL → `dequeue_for_model` None → result False,
   or `_dispatch` raises), `_probe_outstanding` stays True forever and `acquire()` short-circuits at
   "half-open probe in flight" → swaps bricked until restart/force-release. Fails SAFE (stalled swaps =
   no inrush) but a real liveness outage on the post-storm path. **Fix:** add `SwapBrake.abort_probe()`
   (reset `_probe_outstanding`, re-enter `_open(now)`); call it from `scheduler.py` else/except branches
   (935–942) when `auth.action=="proceed"` but no `record_load`. + unit test (grant probe, no record,
   advance clock, assert recovery).
2. **[HIGH] F5 cold-swap fail mode is unreachable from the live path.** The scheduler reserves WITHOUT
   `is_swap=True` (`scheduler.py:797,807`) → default steady-state branch fails OPEN → the cold-swap
   fail-closed + degrade-after-K + `hardware_gate_blind` + `set_hw_degraded` are all silently inert
   (`vram.py:494`'s own comment says the fail-closed path lives in `reserve(is_swap=True)`). **Fix:**
   pass `is_swap=True` on both reserve calls + an integration test (transient miss on a swap fails closed;
   K misses set blind + drive `set_hw_degraded(True)`). NOTE: this flips the fail-closed path on, so
   scheduler/integration tests that rely on absent-nvidia-smi fail-open need a mocked free-VRAM reading.
3. **[HIGH] F4 latch is not version-independent.** Both branches of `_maybe_latch_infeasible` are gated
   behind `_pinned_resident` (`scheduler.py:628`), which is filtered by `vram._pinned` — populated
   EXCLUSIVELY from `expires_at` (`vram.py:373/384`). On Ollama builds without parseable `expires_at`,
   `vram._pinned` is empty → latch never fires, contradicting the spec + `PinDetectionConfig`'s own
   docstring ("degrades to the behavioral signature, never to no protection"). The version-independent
   `_evict_reload_history` is fed but then dead. **Fix:** when `_pinned_resident` is empty, fall back to
   latching on raw same-model evict↔reload oscillation count ≥ threshold. + test with no `expires_at`.
4. **[HIGH] Fail-LOUD-on-infeasible observability is not wired** (the incident's own failure mode).
   `update_swap_brake_state` / `record_swap_brake_engaged` / `update_pinned_vram_gb` are defined +
   exported + unit-tested but have ZERO runtime callers; the infeasible latch logs ONE `warning` then
   sheds at `debug`; no engage/release audit event exists. **Fix:** from `_update_brake_engage_snapshot`
   (edges already detected) push the brake/pinned gauges each tick, `record_swap_brake_engaged` on the
   engage edge, emit one audit event on engage+release (duration + swaps-during-brake), and re-log the
   latch at WARNING on a throttled heartbeat. + test.
5. **[HIGH] Force-release override defeats two invariants.** No upper bound on `ttl_s` (a single
   `POST /broker/swap-brake {release:true, ttl_s:1e12}` disables the backstop ~31000 yrs), and it's
   invisible on `/broker/status` (`force_release_active` exists in `snapshot()` but is dropped by
   `_embed_brake_snapshot`; no gauge). **Fix:** add `SwapBrakeConfig.force_release_max_ttl_seconds`
   (~600) + clamp/reject server-side with a loud audit; surface `force_release_active` + remaining TTL in
   `BrokerStatus`/`_embed_brake_snapshot` + a `bastion_swap_brake_force_active` gauge held for the window.

**NICE-TO-HAVE — ALL RESOLVED (2026-06-27, see §9.2):** min-spacing advances only on dispatch success;
latched-infeasible models on the normal *inference* path aren't fast-shed; backoff cap/reset
under-tested; keep the infeasible shed even during force-release; force-release reachable unauthenticated;
document the serializer-held-through-inference duration and the proxy-passthrough unbraked path.

### 9.1 Resolution — all 5 must-fixes + 1 review-found follow-up landed (2026-06-27)

All five MUST-FIX items implemented test-first on `feat/swap-velocity-circuit-breaker`; a second
adversarial 5-lens review (concurrency, spec-correctness, GPU-safety, storm-trace, test-quality) of the
diff returned **3 lenses pass-with-nits** and surfaced **one genuine HIGH** — a sixth item below — which
was then fixed. Full suite: **1972 passed**, 6 pre-existing env-only failures (no live Ollama/GPU in the
sandbox; identical on the pre-change baseline), **0 new regressions**. The test-quality reviewer
empirically reverted each fix and confirmed the matching test fails — the tests are behavioral.

1. **[BLOCKER] HALF_OPEN probe wedge — DONE.** `SwapBrake.abort_probe()` (re-OPENs an orphaned probe,
   guarded to `HALF_OPEN ∧ _probe_outstanding`, idempotent) + wired on the `else` (result False) and
   `except` branches of `_handle_swap_dispatch`. Tests: `test_swapbrake.py::TestAbortProbe`,
   `test_scheduler.py::TestAbortProbeWiring` (both else + except branches).
2. **[HIGH] F5 cold-swap fail mode unreachable — DONE.** `reserve(is_swap=True)` on both scheduler
   reserve sites. New integration test drives transient-miss→fail-closed and K-misses→degrade-to-blind +
   `set_hw_degraded(True)`; the 3 fail-open-dependent tests were given a mocked free-VRAM reading and the
   blind-forward test reworked through the degraded verdict (a naive stub would have reset the flag).
   Tests: `test_scheduler.py::TestColdSwapFailClosed`, fixed `TestHandleSwapDispatch`/`TestGPUGatingMidSwap`/`TestHardwareGateBlindForward`.
3. **[HIGH] F4 latch version-independence — DONE.** `_max_oscillation_count()` (raw same-model
   evict↔reload count, unfiltered by `vram._pinned`) backs `_maybe_latch_infeasible` when the pin set is
   invisible; the pinned-present branch is unchanged. Tests:
   `test_scheduler.py::TestPinAwareInfeasibleLatch::test_behavioral_latch_fires_without_pin_metadata` (+ below-threshold guard).
4. **[HIGH] Fail-LOUD observability wired — DONE.** `_update_brake_engage_snapshot` now pushes the
   state + pinned gauges each tick, counts the engage edge, emits one audit (`EVENT_SWAP_BRAKE`) on
   engage + release (duration + swaps-during-brake), and re-logs a latch at WARNING on a throttled
   heartbeat; `BrakeState.gauge_value` is severity-ascending. Tests:
   `test_scheduler.py::TestBrakeObservabilityWiring`, `test_swapbrake.py::TestBrakeStateGauge`.
5. **[HIGH] Force-release bound + surfaced — DONE.** `SwapBrakeConfig.force_release_max_ttl_seconds=600`,
   clamped in **both** `SwapBrake.force()` (self-protecting) and the server handler (loud
   `swap_brake_override_clamped` audit); `force_release_active` + `force_release_remaining_s` on
   `/broker/status` and a `bastion_swap_brake_force_active` gauge held each tick. Force-engage stays
   uncapped (fails safe). Tests across `test_swapbrake.py`, `test_server_swapbrake.py`,
   `test_obs_vram_metrics.py`, `test_config_models.py`.
6. **[HIGH — found in this review] F-1 wedge on the `/broker/preload` path — DONE.** `_funnel_preload`
   is the SECOND `acquire()` site on the shared brake; its no-fit 409 re-check and a raising cold-load
   POST both skipped `record_load`, orphaning the probe (the same wedge as #1, on the recovery path). Now
   a `try/finally` calls `abort_probe()` on every non-recording exit. Tests:
   `test_funnel.py::test_preload_no_fit_recheck_aborts_orphan_probe`, `::test_preload_post_failure_aborts_orphan_probe`.

**Remaining (after §9.1):** only the NICE-TO-HAVE list above — now also resolved in §9.2.

### 9.2 Nice-to-have resolution + a second review-found hardening (2026-06-27)

All six NICE-TO-HAVE items landed test-first; a focused 3-lens adversarial review (safety/spec,
missed-second-site, concurrency/test-quality) of the diff surfaced **one genuine HIGH** in the NH-5 fix
itself (item 5 below), now fixed. Full suite after: **1987 passed**, 6 pre-existing env-only failures, 0
new regressions.

1. **NH-1 — min-spacing advances at the GPU-I/O issue point.** `SwapBrake.note_load_issued()` stamps
   `_last_load_t` at issue (spacing only, no token); called before `_dispatch_for_model` and the preload
   POST, so a load that is issued then FAILS still spaces the next attempt (`record_load` re-stamps on
   success, so the success path is unchanged).
2. **NH-2 — fast-shed latched models on the inference path.** The proxy consults the brake at admission
   (`_latch_retry_after_fn` → `swap_brake.peek`) and returns `503 + Retry-After` (latch-derived) for a
   latched-infeasible model instead of enqueuing doomed work that would block to the 504 timeout.
3. **NH-3 — backoff cap + reset now tested** (`test_cooloff_is_capped_at_backoff_max`,
   `test_backoff_resets_after_clean_closed_window`).
4. **NH-4 — infeasible shed holds even under force-release.** Dropped the `not forced_release` guard in
   `acquire()`/`peek()`: force-release disables the velocity brake but no longer re-authorizes evicting a
   caller's pin (the eviction path independently refuses `vram._pinned`, so pins are doubly protected).
5. **NH-5 — force-release loopback-gated when admin auth is not enforced.** A force-RELEASE from a
   non-loopback peer is refused (403) unless auth is actually enforced. *Review catch:* the first cut
   keyed on `auth.enabled` alone, but `make_admin_key_dependency` only enforces when `enabled AND
   api_keys` is non-empty — so the realistic `enabled=True, api_keys=[]` misconfig left the surface open
   AND bypassed the gate. Fixed to gate on `auth_enforced = enabled AND api_keys` (and a missing config
   now fails safe to loopback-required). Force-engage/drain/unload TIGHTEN safety and stay open.
6. **NH-6 — docs.** The load serializer is held through the *blocking inference* (not just the cold
   load), so a long swap-in delays the next swap + both preload routes (documented at the serializer
   block); passthrough endpoints are an intentionally UNBRAKED path (documented on `_handle_passthrough`).
