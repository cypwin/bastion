# Grafana Panel Catalogue: observability-expansion metrics (INTENT ONLY — GATED)

**Date:** 2026-06-19
**Status:** Catalogued intent — **GATED, no JSON authored in this document**
**Gate:** the **Vision C base dashboard** / the `dashboards/grafana/` directory (both currently **absent** from the tree — verified: no `dashboards/` directory exists). No panel JSON is authored here and none should be authored until the base dashboard lands.
**Related:** `docs/design/specs/2026-06-19-observability-expansion.md` §5.6 (Tier-3 catalogue, "gated on Vision C base"), §7 (surface mapping — the `Grafana` column), §9 (ADR-010 relationship); `docs/design/specs/2026-06-19-observability-expansion-metric-freeze.md` (the metric names this catalogue maps); ADR-010 (Grafana JSON CI compatibility — the multi-version validation any future JSON must pass)

---

## 1. Why this is intent-only

Spec §5.6 marks the Grafana panel catalogue as **blocked on the Vision C base dashboard / `dashboards/grafana/` dir (pending)**. ADR-010 makes any `dashboards/grafana/*.json` a v0.5 ship-blocker unless it passes the schema-validated multi-version CI — and that CI, the fixtures, and the base `bastion-overview.json` do not yet exist in this tree.

Therefore this document **describes the intended panels** so that when the Vision C base dashboard is authored, the expansion's panel set is already specified (1:1 metric↔panel intent, alert thresholds, panel types). It deliberately authors **no JSON**: per spec §5.6, "if expansion ships first, **catalogue is authoritative for what Vision C must include**." This is that authoritative catalogue, not the implementation.

**Hard constraint inherited:** only metrics that actually exist as Prometheus objects in `metrics.py` get a panel that can render today. The PSI/swap/block-device/throttle/Xid/PCIe/OOM/LLM-tps signals are **JSON/TUI-only at this stage** (no Prometheus object — see the metric-freeze doc §5); their panels are catalogued as **deferred-pending-metric** and cannot be authored until the metric is promoted.

---

## 2. Panel catalogue — metrics that exist in `metrics.py` (authorable once the base dashboard lands)

These map 1:1 to the NET-NEW + Tier-0-activated metric objects (metric-freeze doc §3–§4). Panel JSON is **not** authored here; the rows specify what to author.

| Metric | Panel type | Intended panel | Suggested alert | Spec §7 row |
|---|---|---|---|---|
| `bastion_risk_index` | Gauge / time series | "Composite Risk Index" — 0→1 gauge with nominal/elevated/high/critical thresholds at 0.4/0.6/0.8 | `bastion_risk_index > 0.7` for 2m | RiskIndex |
| `bastion_risk_dominant_factor_total{factor}` | Bar / stat (rate) | "Dominant Risk Factor" — `rate(...[5m])` by `factor`, showing which of the 5 components dominates | — (informational) | RiskIndex |
| `bastion_contention_events_total{kind}` | Time series (rate) | "Host Contention Events" — `rate(...[5m])` stacked by `kind` (nvme_burst/mem_pressure/cpu_contention/combined) | `increase(...{kind="combined"}[5m]) > 0` | Contention events |
| `bastion_thermal_coupling_active` | State timeline | "CPU→GPU Thermal Coupling" — 0/1 engaged band | — (context for headroom alert) | Thermal coupling |
| `bastion_thermal_headroom_celsius` | Time series | "Min Thermal Headroom (°C)" — single line, red zone ≤5 | `bastion_thermal_headroom_celsius <= 5` (genuine low-headroom, **not** `<= 0`) | Thermal coupling |
| `bastion_vram_ledger_drift_mb{gpu_index}` | Time series | "VRAM Ledger Drift (MB)" — signed line per `gpu_index` | `max by(gpu_index)(abs(bastion_vram_ledger_drift_mb)) > 2048` (multi-GPU-safe) | VRAM ledger drift |
| `bastion_vram_reconcile_stale_total` | Time series (rate) | "VRAM Reconcile — Stale Removals" — `rate(...[5m])` (Ollama auto-unloads / client bypass rate) | — | (reconcile) |
| `bastion_vram_reconcile_import_total` | Time series (rate) | "VRAM Reconcile — Untracked Imports" — `rate(...[5m])` | — | (reconcile) |
| `bastion_gpu_temperature_celsius` | Time series | "GPU Temperature (°C)" — now emits on the fast tick | (existing GPU-temp alert posture) | GPU die temp (Tier 0) |
| `bastion_cooldown_waits_total` | Time series (rate) | "Scheduler Cooldown Waits" — `rate(...[5m])` | — | Cooldown waits (Tier 0) |
| `bastion_model_swap_duration_seconds{model}` | Heatmap / histogram | "Model Swap Duration" — histogram quantiles, optionally by `model` | — | Swap duration (Tier 0) |

**PSI alert thresholds (Prometheus-alert, not TUI) live here per spec §5.6:** when `bastion_psi_*` is promoted to a metric, the io_full alert ladder is `io_full ≥ 5` (warn) / `≥ 25` (critical). The **TUI** display thresholds are separate and live in `ObservabilityConfig` (spec §5.2) — this catalogue owns only the Prometheus-alert numbers.

---

## 3. Deferred-pending-metric panels (catalogued; NOT authorable until the metric exists)

These are in the spec's §7 `Grafana` column but have **no Prometheus object** in `metrics.py` today (metric-freeze doc §5). Each panel is specified so it can be authored the moment its metric is promoted — but it **cannot render** before then.

| Intended metric | Intended panel | Suggested alert |
|---|---|---|
| `bastion_gpu_compute_utilization_pct` / `bastion_gpu_memory_utilization_pct` | "GPU Compute / Memory Utilization (%)" — dual line | — |
| `bastion_gpu_memory_junction_temp_celsius` | "GDDR Junction Temp (°C)" | panel + alert |
| `bastion_gpu_throttle_events_total{reason}` | "GPU Throttle Events" — rate by `reason`, red on `hw_*` | alert on `hw_*` reasons |
| `bastion_gpu_pcie_gen` / `bastion_gpu_pcie_width` | "PCIe Link Gen / Width" — stat with `DOWNGRADED` mapping | alert on gen/width drop |
| `bastion_gpu_xid_errors_total{xid_code}` | "GPU Xid Errors" — rate by `xid_code` | page on any increase |
| `bastion_psi_some_avg10{resource}` / `bastion_psi_full_avg10{resource}` | "Pressure Stall (PSI)" — by `resource` (cpu/memory/io) | io_full ≥ 5 / ≥ 25 (see §2) |
| `bastion_swap_in_mb_s` / `bastion_swap_out_mb_s` | "Swap Rate (MB/s)" — dual line | — |
| `bastion_block_device_util_pct{device}` / `bastion_block_device_await_ms{device,op}` | "Block-Device Utilization / Await" — by `device` (and `op` for await) | — |
| `bastion_cpu_package_watts` | "CPU Package Power (W)" — Intel **or** AMD RAPL source | — |
| `bastion_oom_kill_total` | "OOM Kills" — counter rate | alert on any increase |
| `bastion_llm_decode_tps{model}` / `bastion_llm_prefill_tps{model}` | "LLM Decode / Prefill Tokens-per-sec" — by `model` | — |
| `bastion_llm_ctx_utilization_ratio{model}` | "Context-Window Utilization" — by `model` | — |
| `bastion_llm_time_to_first_token_seconds{model}` *(already exists pre-expansion)* | "Time To First Token" — quantiles by `model` | p95 > 10s |

---

## 4. Authoring rules for whoever lands the JSON (after the gate clears)

1. **Author into `dashboards/grafana/bastion-overview.json`** (the Vision C base), not a new file, unless Vision C's structure dictates otherwise.
2. **ADR-010 is a ship-blocker:** every panel's metric reference must resolve, and the JSON must pass the multi-version schema CI fixture. No panel ships against a metric that does not exist in `metrics.py`.
3. **Label values are not pinned in panel queries** beyond what is necessary — e.g. use `max by(gpu_index)(...)` rather than hard-coding `gpu_index="0"`, so multi-GPU hosts render without edits (rev. 3 portability).
4. **Thresholds are documented, not magic:** the alert numbers above (0.7 risk, ≤5 headroom, >2048 drift, io_full ≥5/≥25) are the catalogued defaults; operators tune them. Where a threshold mirrors a config key (`ObservabilityConfig`/`CorrelationConfig`), the panel description should name the key.
5. **One panel per existing metric (§2 first).** Deferred-pending-metric panels (§3) are added only in the same change that promotes their metric to `metrics.py` (and adds it to the freeze doc).

---

## 5. Gate status

- `dashboards/grafana/` directory: **absent** (verified at authoring time).
- Vision C base dashboard JSON: **absent**.
- ADR-010 CI fixtures: **absent**.
- **Conclusion:** no JSON authored; this catalogue is the authoritative intent until the base lands. When it does, §2 panels are immediately authorable; §3 panels follow their metrics.
