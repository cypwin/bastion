# Metric Freeze: observability-expansion Prometheus surface (propose freeze at v0.6)

**Date:** 2026-06-19
**Status:** Proposed freeze (target tag: **v0.6**)
**Scope:** Every Prometheus metric **object** added by the inference-correlated observatory expansion, Phases 1–3
**Source of truth:** `src/bastion/metrics.py` (the metrics enumerated below are read directly from it, not from the spec's aspirational surface table)
**Related:** `docs/design/specs/2026-06-19-observability-expansion.md` §3 (cardinality PERMITTED-SET, rule #2), §7 (surface mapping), §9 (ADR-010 freeze relationship); ADR-010 (Grafana JSON CI); `scripts/check_metric_cardinality.py` (the CI lint that mechanically enforces the permitted-set)

---

## 1. Purpose and the freeze contract

ADR-010 and spec §9 establish that **expansion metric names are not part of the existing Vision C schema freeze** (which covers only `bastion_request_queue_wait_seconds`, `bastion_vram_used_mb`, `bastion_thrashing_detector_halt_total`, `bastion_concurrent_requests_active`, `bastion_model_swap_total`). Spec §9 directs that the **new** names are "proposed in the panel-catalogue doc for cardinality/naming review before code, **freezing at the v0.6 tag**." This document is that proposal for the metric **names + label sets**; the Grafana panel↔metric mapping lives in the companion `…-grafana-catalogue.md`.

**Freeze contract (what "frozen at v0.6" means):** once tagged v0.6, the metric **names** and their **label-name sets** below become a public contract — they are not renamed or relabeled without a deprecation cycle, exactly as the Vision C set is treated today. Label **values** are explicitly **not** frozen (a `device="sda"` series is as valid as `device="nvme0n1"`; rev. 3).

---

## 2. The PERMITTED-SET (spec §3 rule #2)

Every label name on every metric below MUST be a member of the permitted-set. The set is enforced mechanically by `scripts/check_metric_cardinality.py` (AST lint over `metrics.py`, label-**name** check, value-agnostic), wired into CI.

**Observatory permitted-set (spec §3, verbatim):**

```
{model, resource, device, op, reason, kind, factor, xid_code, gpu_index}
```

- `resource` ∈ {cpu, memory, io}
- `device` = dynamically discovered base storage device (`nvme*`/`sd*`/`vd*`/`mmcblk*`/`hd*`; 1–8 per host) — a **name**, never NVMe-locked
- `reason` / `kind` / `factor` = fixed enums (≤5)
- `xid_code` = ≤15 known NVIDIA codes + `unknown`
- `gpu_index` = single-GPU `"0"` today; the seam for a non-breaking multi-GPU future

**Legacy bounded labels** (predate the observatory; sanctioned by `metrics.py`'s own "Tier 1 always-safe" header and listed in the lint's `LEGACY_BOUNDED_LABELS`): `endpoint`, `status_code`, `tier`, `from_model`, `to_model`, `priority`, `agent_id`, `verdict`, `skill`, `state`, `method`, `error_code`. None of the new metrics below introduce a label outside the union of these two sets.

**Forbidden (unbounded) — never a label:** `pid`, `request_id`, `task_id`, `context_id`, `trace_id`, `span_id`, `session_id`, `agent_pid`, `process_id`. Per-process/per-PID/per-event data stays on the **TUI + JSON** surfaces only (spec §3, §7).

---

## 3. NET-NEW metric objects added in Phases 1–3 (the freeze set)

These are the metric **objects** newly defined in `metrics.py` by the expansion. Each row asserts the label set and its permitted-set membership.

| # | Metric name | Type | Labels | Permitted-set verdict | Spec | Source |
|---|---|---|---|---|---|---|
| 1 | `bastion_vram_reconcile_stale_total` | Counter | *(none)* | ✅ label-less | §5.4 | model-name would be unbounded → deliberately label-less |
| 2 | `bastion_vram_reconcile_import_total` | Counter | *(none)* | ✅ label-less | §5.4 | model-name would be unbounded → deliberately label-less |
| 3 | `bastion_vram_ledger_drift_mb` | Gauge | `gpu_index` | ✅ `gpu_index` ∈ set | §5.4 / §7 | single-GPU `"0"`; multi-GPU-safe alert `max by(gpu_index)(abs(...))>2048` |
| 4 | `bastion_risk_index` | Gauge | *(none)* | ✅ label-less | §6.4 / §7 | composite score ∈ [0,1], one global gauge |
| 5 | `bastion_risk_dominant_factor_total` | Counter | `factor` | ✅ `factor` ∈ set (5 enum) | §6.4 / §7 | `factor` ∈ {vram_headroom, thermal_headroom, swap_rate, thrashing, memory_psi} |
| 6 | `bastion_contention_events_total` | Counter | `kind` | ✅ `kind` ∈ set (4 enum) | §6.3 / §7 | `kind` ∈ {nvme_burst, mem_pressure, cpu_contention, combined}; attribution string is JSON-only |
| 7 | `bastion_thermal_coupling_active` | Gauge | *(none)* | ✅ label-less | §6.5 / §7 | 0/1 fan-curve engaged |
| 8 | `bastion_thermal_headroom_celsius` | Gauge | *(none)* | ✅ label-less | §6.5 / §7 | min computable CPU/GPU headroom term; skipped (not 0) when neither term computable |

**All eight new metric objects pass the permitted-set check.** The only labels used are `gpu_index`, `factor`, and `kind` — all members of the observatory permitted-set — and five of the eight carry no label at all. The CI lint (`scripts/check_metric_cardinality.py`) exits 0 on the current `metrics.py`, confirming this mechanically.

---

## 4. Tier-0 ACTIVATIONS (pre-existing objects, newly *emitted* in Phase 1)

These metric **objects already existed** in `metrics.py` (defined for Vision C / earlier work) but were computed-yet-never-emitted "dead metrics." Phase 1 wakes them by wiring their emit call-sites (spec §9: "the dead Prometheus metrics genuinely activated here … are `gpu_temperature_celsius`, `cooldown_waits_total`, and `model_swap_duration_seconds`"). They are listed for completeness — the **freeze applies to their emission contract too** — but they are **not** new names.

| Metric name | Type | Labels | Permitted-set verdict | Activation note |
|---|---|---|---|---|
| `bastion_gpu_temperature_celsius` | Gauge | *(none)* | ✅ label-less | now emits on the fast 2 s tick; **skipped (not 0)** when the backend returns `None` (StubBackend / non-NVIDIA) |
| `bastion_cooldown_waits_total` | Counter | *(none)* | ✅ label-less | now incremented once per enforced scheduler cooldown |
| `bastion_model_swap_duration_seconds` | Histogram | `model` | ✅ `model` ∈ set | now observed in **both** swap branches (semaphore + no-semaphore) |

**Explicitly NOT activated (spec §9 correction):** `bastion_vram_used_bytes` (the bytes gauge). The Vision-C `bastion_vram_used_mb` is already emitted; waking the bytes gauge at the same site would be a redundant second object. It remains defined but dormant.

---

## 5. Catalogued-but-DEFERRED metrics (NOT in `metrics.py`; not part of this freeze)

The spec's §7 surface table is the *design intent* and lists many `bastion_*` names for PSI, swap rate, block-device util/await, GPU compute/memory utilization, throttle reasons, Xid, PCIe gen/width, OOM, and per-model LLM decode/prefill-tps + ctx-utilization. **As implemented in Phases 1–3, those signals are surfaced on the JSON (`/broker/contention`, `/broker/gpu/extended`, `/broker/recent`, `/broker/snapshot`) and TUI surfaces only — they have no Prometheus metric object in `metrics.py`.** They are therefore **out of scope for the v0.6 freeze**: there is nothing to freeze yet.

When/if these are promoted to Prometheus, each MUST be added to the freeze set above with its label set asserted against the permitted-set, and a matching Grafana panel added per ADR-010. Their *intended* (not-yet-built) label sets, recorded so the eventual names are reviewable now, are:

| Intended name | Intended labels | Permitted-set check (pre-review) | Surface today |
|---|---|---|---|
| `bastion_gpu_compute_utilization_pct` | *(none)* | ✅ label-less | `/broker/status.gpu`, TUI |
| `bastion_gpu_memory_utilization_pct` | *(none)* | ✅ label-less | `/broker/status.gpu`, TUI |
| `bastion_gpu_memory_junction_temp_celsius` | *(none)* | ✅ label-less | `/broker/status.gpu`, TUI |
| `bastion_gpu_throttle_events_total` | `reason` | ✅ `reason` ∈ set (4 enum) | `/broker/gpu/extended`, TUI |
| `bastion_gpu_pcie_gen` / `bastion_gpu_pcie_width` | *(none)* | ✅ label-less | `/broker/gpu/extended`, TUI |
| `bastion_gpu_xid_errors_total` | `xid_code` | ✅ `xid_code` ∈ set (≤16) | `/broker/gpu/extended`, TUI |
| `bastion_psi_some_avg10` / `bastion_psi_full_avg10` | `resource` | ✅ `resource` ∈ set (3) | `/broker/contention`, TUI |
| `bastion_swap_in_mb_s` / `bastion_swap_out_mb_s` | *(none)* | ✅ label-less | `/broker/contention`, TUI |
| `bastion_block_device_util_pct` | `device` | ✅ `device` ∈ set (1–8) | `/broker/contention`, TUI |
| `bastion_block_device_await_ms` | `device`, `op` | ✅ both ∈ set | `/broker/contention`, TUI |
| `bastion_cpu_package_watts` | *(none)* | ✅ label-less | `/broker/contention`, TUI |
| `bastion_oom_kill_total` | *(none)* | ✅ label-less | `/broker/contention`, TUI |
| `bastion_llm_decode_tps` / `bastion_llm_prefill_tps` | `model` | ✅ `model` ∈ set | `/broker/recent`, TUI |
| `bastion_llm_ctx_utilization_ratio` | `model` | ✅ `model` ∈ set | `/broker/recent`, TUI |

(`bastion_llm_time_to_first_token_seconds{model}` already exists as a pre-expansion object and is unaffected.)

Every intended label above is within the permitted-set, so promoting any of them later is a cardinality-clean change — but the **freeze at v0.6 covers only the objects that actually exist in `metrics.py` (§3 and §4)**.

---

## 6. Freeze proposal

1. **Freeze the eight NET-NEW names and label sets in §3 at the v0.6 tag.** After v0.6 they are public contract; renames/relabels require a deprecation cycle (same policy as the Vision C set).
2. **Freeze the emission contract of the three Tier-0 activations in §4** (they keep their existing names — already stable — and the §4 emission semantics, especially "skip, never 0," become part of the contract).
3. **Do not freeze the deferred §5 names.** They are not in `metrics.py`; freezing absent names is meaningless. They re-enter review when promoted.
4. **CI gate:** `scripts/check_metric_cardinality.py` stays wired in CI so any future label addition is checked against the permitted-set before merge. This document + the lint together are the freeze's enforcement.
5. **ADR-010 coupling:** every frozen metric must have a matching Grafana panel (companion `…-grafana-catalogue.md`), validated by ADR-010's multi-version CI — **gated on the `dashboards/grafana/` base dashboard existing** (currently absent).

---

## 7. Verification

- `python scripts/check_metric_cardinality.py` → exit 0 (all labelnames within the permitted-set) on the `metrics.py` enumerated here.
- The §3 table was generated by AST-walking `metrics.py`; it is the authoritative object list, not the spec's aspirational §7 table.
