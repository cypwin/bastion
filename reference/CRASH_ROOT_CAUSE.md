# CRASH_ROOT_CAUSE — Host Hard-Lockup During Model-Swap Storm

> **Status:** Reference (forensics + maintainer guardrail)
> **Incident:** Host hard-lockup 2026-06-26 ~15:20–15:21 during a model-swap storm.
> **Companion design:** `docs/design/specs/2026-06-26-swap-velocity-circuit-breaker-design.md`
>   (the F1–F6 swap-velocity circuit-breaker sweep).

This document records *why* the host died and, just as importantly, what is **not**
a useful fix — so a future maintainer does not "improve" a deliberately
sensor-light defense (F6) into a more expensive one that cannot possibly work.

---

## 1. The reframing (read this before touching any swap code)

BASTION's logical VRAM bin-packer is **correct**. The crash did **not** come from a
logical VRAM over-commit — the scheduler's individual swap decisions were each sound.
The crash came from the **velocity of correct swap decisions**: too many cold loads /
evictions packed into too short a window, compounded by BASTION **fighting Ollama's
`keep_alive=-1` caller pins in an expiry-timer namespace it cannot see** (eviction =
`POST /api/generate {keep_alive:0}` at `vram.py:463-466`, the exact inverse of the
caller's pin — last-writer-wins).

The consequence is a physical one: each cold load drives a **millisecond-scale GPU
power inrush**. Individually safe; in rapid succession the repeated inrush transients
are what took the host down. The defense is therefore about **load FREQUENCY**, not
about a smarter per-decision VRAM check.

---

## 2. F6 — the millisecond inrush is UNOBSERVABLE in-band

**Finding (do not regress):** the dangerous event is a **millisecond GPU power inrush**
at cold-load time. BASTION's health path polls `nvidia-smi` on a **5 second** cadence
(`health.py:check_gpu_safe`, ~54). A 5 s poll **cannot see a millisecond transient** —
the spike has come and gone hundreds of times over between two samples. The poll only
ever observes steady-state draw, never the inrush that actually matters.

**Therefore faster polling is theater.** Tightening the poll interval — to 1 s, to
100 ms, to anything an in-band Python `nvidia-smi` subprocess can achieve — does **not**
make the transient observable. It only adds CPU cost, subprocess churn, and a *false
sense* that the inrush is now "monitored." There is no in-band sensor BASTION can add
that catches a ms event from a multi-second software poll. **Do not add one.** If a
future maintainer's instinct is "we missed the spike, poll faster," that instinct is
wrong for this class of event and this entire section exists to stop it.

### What F6 actually does

1. **Publish the trend, do not pretend to catch the transient.** F6's only code change
   is to stop silently discarding the already-computed power reading: publish
   `bastion_gpu_power_watts` / `bastion_gpu_power_cap_watts` gauges
   (`update_gpu_power_watts`, mirroring the temperature publish at `health.py:45-46`).
   This gives **steady-state trend visibility** for dashboards and post-hoc forensics —
   it is explicitly **not** a transient detector.
2. **`check_gpu_safe` stays the steady-state veto** — unchanged. It guards against a
   sustained thermal/power condition, which a 5 s poll *can* see. It was never the
   inrush guard and must not be reworked into one.

---

## 3. The real transient mitigation is FREQUENCY control, not detection

Because the inrush cannot be **detected** in-band, it is instead **prevented from
recurring fast enough to matter**. That mitigation lives entirely in the swap-velocity
brake (F2), specifically its **`min_spacing_seconds`** floor (default **8.0 s** ⇒ a hard
7.5 cold-loads/min instantaneous ceiling, below the stated >8/min crash zone).

The linkage to internalize:

- `min_spacing_seconds` does **not** observe, measure, or react to the power transient.
- It controls **how often a cold load is allowed to happen at all** — i.e. it bounds the
  *rate* of inrush events so consecutive transients are spaced far enough apart that the
  power delivery never compounds into a host-killing condition.
- The token bucket (sustained ~2.5 swaps/min with two-token accounting) is the tighter
  *sustained* binding constraint; `min_spacing_seconds` is the *instantaneous* inrush
  floor. Both are **frequency governors**, not sensors.

In one sentence: **we cannot see the spike, so we ration the events that cause it.**
The brake is sensor-independent on purpose — it reads only `time.monotonic()` plus
BASTION's own transition log, so it keeps governing inrush frequency precisely when every
`nvidia-smi` / `/api/ps` sensor is dark, which is exactly when the host is most likely to
die.

---

## 4. Guardrail for future maintainers

If you are tempted to "fix F6" by making GPU monitoring faster or finer-grained:

- **Stop.** The inrush is a millisecond event; no in-band software poll can resolve it.
  Faster polling buys nothing and costs CPU + subprocess churn.
- The transient is **already mitigated** — by `swap_brake.min_spacing_seconds`
  controlling **load frequency**, not by any sensor.
- The power gauges from F6 are **steady-state trend telemetry only**. Do not wire them
  into a per-load admission decision expecting them to catch a spike. (The sole sanctioned
  coupling is the optional, default-off `gpu.power_headroom_pct`, which treats *sustained*
  steady-state draw near the cap as bucket-empty pressure — still a steady-state signal,
  never a transient detector.)
- If you genuinely need transient-level power data, it requires **out-of-band hardware
  telemetry** (e.g. driver-level power-capping or a hardware power monitor), which is
  out of scope for this package. Do not simulate it with a tighter Python poll.

**Bottom line:** the crash came from the **velocity of correct swap decisions**. The cure
is rationing that velocity (F2's `min_spacing_seconds`), not detecting the unobservable
inrush (a faster F6 poll). Keep F6 cheap.
