# Design Spec: BASTION Inference-Correlated Observatory

**Date:** 2026-06-19
**Status:** Draft (rev. 3 — hardware/model portability applied)
**Supersedes-by-extension:** `docs/design/specs/2026-03-13-observability-first-design.md` (extends, does not duplicate)
**Related ADRs:** ADR-005 (BastionPanel direct-accessor contract), ADR-007 (MCP tool versioning), ADR-009 (TUI deprecation instrumentation), ADR-010 (Grafana JSON CI compatibility)
**Origin:** Synthesis of 6 independently-authored cluster designs (gpu-device, system-contention, process-attribution, inference-native, correlation-engine, surfaces-spine)
**Rev. 3 note:** BASTION is a public, GitHub-facing project. Rev. 3 removes every assumption that the deployment is the original developer's box (a single consumer NVIDIA RTX 5090, an AMD CPU read via k10temp, a fixed `nvme0n1`/`/mnt/nvme_data` layout, and magic constants like 85 °C / 200 MB/s / 300 W). All hardware access is routed through the existing `GPUBackend` protocol seam; all numeric thresholds become documented config keys with **auto-detected-from-device** defaults where the device can report them and documented fallback constants otherwise; and every "other hardware" path (non-NVIDIA, no-GPU, multi-GPU, server-GPU, Intel/AMD RAPL, k10temp/coretemp/unknown CPU sensor, dynamic block devices, no-PSI) is a **tested default**, not a special case. See Constraint #7 (Section 3) and the scope guardrail therein: the goal is portable seams + config + graceful degradation so the design does not *preclude* other hardware — **not** a mandate to build full multi-vendor backends now.

---

## 1. Vision

BASTION sits at the one chokepoint every inference request must cross: it is the proxy in front of Ollama, the scheduler that serializes model swaps, and the only process that simultaneously sees the GPU, the host, and the token stream. The **Inference-Correlated Observatory** turns that vantage point into the product's defensible moat: not "another GPU dashboard," but the only tool that can answer *"why did inference stall at 14:32:07?"* by joining a token-rate collapse to a concurrent NVMe burst, a PCIe downgrade, a throttle event, and a competing process — all on one monotonic clock. External tools (`nvidia-smi`, `htop`, `iostat`, Grafana node-exporter) each see one layer; only BASTION sees the correlation. This spec designs the collection, the unified `MachineSnapshot` model, the surface mapping, and the `correlation.py` engine that synthesizes raw signals into forward-looking intelligence (stall-reason enrichment, contention events, a composite RiskIndex). The dominant engineering constraint is that all of this must be threaded through the existing non-buffering NDJSON stream and the in-memory state model without a database, without per-PID Prometheus cardinality, and without a single new heavy dependency — **and, as of rev. 3, without hard-coding any one machine's hardware.** Every GPU signal degrades to `None` on non-NVIDIA / no-GPU hosts through the `GPUBackend` seam; every host signal (CPU temp, RAPL, block-device IO, PSI) discovers its source dynamically and returns `None` when the source is absent.

---

## 2. Scope

Signals are stratified into five tiers. This spec **fully designs Tiers 0–2**, **catalogues Tier 3**, and **defers Tier 4 with reasons**.

| Tier | Definition | Treatment |
|------|-----------|-----------|
| **0** | Already-collected data that is computed but never emitted; activation is a call-site wire-up (the "dead metrics"). | Fully designed; Phase 1. |
| **1** | New signals with a cheap, always-available source (single subprocess field append, plain `/proc` read, existing psutil counter). Direct correlation value. | Fully designed; Phases 1–2. |
| **2** | New signals requiring a derivation, a slow-poll subprocess, or cross-signal joins (throttle reasons, Xid, PCIe tx/rx, process churn, the correlation engine). | Fully designed; Phases 2–3. |
| **3** | Net-new surfaces and tooling that ride on the above but are blocked on other in-flight work (MCP `broker_snapshot_v1` tool, SSE `/broker/snapshot/stream`, Grafana panel catalogue, CI cardinality lint). | Catalogued (Section 5.6, Phase 4); design intent fixed, implementation gated. |
| **4** | Deferred-with-reasons (below). | Not designed. |

**Tier 4 — deferred, with reasons (rev. 3 reframing: deferrals are justified by *cost/maturity*, not by "the dev box is NVIDIA"):**

- **ECC error counters** (`ecc.errors.*.volatile.total`) — **server GPUs with ECC exist in the wild** (A100/H100/L40/A6000 enable ECC by default), so the value is **not** universally `[N/A]`; on consumer cards (RTX 40xx/50xx) ECC is disabled by default and the field is `[N/A]→None`. Mechanism is trivial; designed as an **opt-in** slow-poll field (`observability.ecc_enabled: false`) on the **`GPUBackend` protocol** (`NvidiaBackend.query_ecc_errors()` parses `ecc.errors.*`; `StubBackend` returns `None`). Opt-in because the polling cost is non-zero and most consumer deployments gain nothing; **not deferred because "no server GPU exists."** Not built in this spec.
- **Per-fan speed** (multi-fan triple-fan readback) — `nvidia-smi` reports one aggregate `fan.speed` per GPU, not per fan. Per-fan requires NVML direct binding (new dependency). Deferred. (Aggregate `fan.speed` *is* shipped as a read-only signal, and tolerates `[N/A]` on fanless server GPUs — see 5.1.)
- **GPU board power via amdgpu/RAPL hwmon fill** — **moved to Tier 4 as a future BACKEND track, not as "the dev box is NVIDIA so skip it."** Rev. 3 reframing: `gpu_board_watts` is a **backend-provided** field. On NVIDIA, `NvidiaBackend.query_status()` already populates `power_draw_watts` from `power.draw`, so `GPUStatus.power_draw_watts` is non-`None` and `health.check_gpu_safe()` works with zero new code. On a future `AMDBackend`, board power would come from `rocm-smi --showpower` or amdgpu hwmon **inside that backend** and populate the same field. The point is that no higher layer reads board power from a hwmon path directly — it always comes through the protocol. Until an `AMDBackend` exists, `gpu_board_watts` (the *separate* contention-cluster field) stays `None`; `GPUStatus.power_draw_watts` is **not** filled from a host-side hwmon scan. `cpu_package_watts` (host RAPL, Intel **or** AMD — see 5.2) is the only package-power signal that ships, and it is a host signal, not a GPU-board signal.
- **`/dev/kmsg` killed-process-name enrichment** for OOM — requires `CAP_SYSLOG`; the `/proc/vmstat` `oom_kill` counter delivers the correlation without the permission dependency. Name extraction deferred.
- **`sudo nvidia-smi pmon`** fallback for cross-user GPU process visibility — privilege escalation; out of scope. The bastion-group user sees its own and (typically) all GPU processes; multi-user gap is documented, not closed.
- **Dynamic baseline auto-calibration** for contention thresholds (idle-disk / idle-PSI measurement on first boot) — adds collector statefulness; config-driven thresholds ship first. **This is the correct long-term default for portability** (it auto-tunes the NVMe/PSI thresholds to whatever drive and kernel the operator actually has), so it is explicitly the recommended future replacement for the static defaults, not merely a deferral of convenience.
- **Full multi-vendor GPU backends (AMD via ROCm/rocm-smi, Intel iGPU via `intel_gpu_top`/sysfs)** — **explicitly a future track.** Rev. 3 adds the *seams* (protocol methods, `StubBackend` returns, detection-ordering documentation, a `gpu.backend` override) so the higher layers do not preclude these backends, but **building them is out of scope here** (scope guardrail, Constraint #7).
- **Multi-GPU per-device iteration** (emitting one `GPUStatus`/metric series per GPU with a `gpu_index` label across all GPUs) — the data model is made **list-extensible** now (`GPUStatus.gpu_index`, `gpu.gpu_index` selector), but iterating *all* GPUs is a future track; the shipped path reports the configured single GPU. See 4.2 and 5.1.
- **Subscriber/pub-sub bus for the TUI** — explicitly excluded per ADR-005 (the direct-accessor contract stands; see Section 9).

---

## 3. Architecture Constraints

1. **In-memory, no database.** All state (snapshots, event rings, recent-request deques, leases) lives in bounded in-process structures, consistent with the project's "no external DB" rule. The new structures are: `_machine_snapshot_deque` (`maxlen=180`, ~6 min @ 2s), the `CorrelationRing` (`maxlen=512`), the `ContentionEventDetector._recent_contentions` (`maxlen=50`), per-process churn deque (`maxlen=10`), and bounded Xid/OOM event rings (`maxlen=20`). The Xid rising-edge dedup set is **not** an unbounded set — it derives from the bounded `recent_xids` deque (maxlen=20), so it cannot grow across long uptime (addresses constraint-lens nice-to-have). Total memory ceiling for all new rings is well under 1 MB.
2. **Prometheus cardinality discipline (hard rule).** **Never** use per-PID, per-request-id, per-task-id, or per-context-id as a Prometheus label. Bounded labels only: `model` (existing convention), `resource` ∈ {cpu, memory, io}, `device` = **dynamically discovered base storage device name** (`nvme*`, `sd*`, `vd*`, `mmcblk*`, `hd*`; expected 1–8 physical drives on a host — **not** an NVMe-only set), `reason`/`kind`/`factor` (fixed enums ≤5), `xid_code` (≤15 known codes + `unknown`), `gpu_index` (single-GPU deployments emit `0`; the label exists so multi-GPU is a non-breaking future extension). Per-PID/per-process data is **TUI + JSON/MCP only**. This is enforced by a CI lint (Tier 3, Section 5.6). The lint checks `labelnames` against the **permitted-set** (so `gpu_index`, `device`, `op`, etc. pass) rather than only blacklisting `pid`/`request_id`/`task_id`/`context_id` — and it validates label **names**, never label **values**, so a `device="sda"` or `device="vdb"` series is as valid as `device="nvme0n1"`.
3. **Streaming integrity (non-negotiable).** The NDJSON passthrough for `ollama run` must never be buffered. The token-derived signals (tokens/sec, TTFT, ctx-utilization) are collected by an **O(1) tap** that parses one small JSON object per chunk and yields every chunk immediately. The tap state is closure-captured; the existing `proxy.py` streaming loop keeps yielding each chunk before/after the parse. The inference streaming path is `_stream_response.generate()` (currently `proxy.py:591`), **not** the raw `_stream_passthrough.generate()` (currently `proxy.py:523`); see Section 5.4 for which path is instrumented and why. The token signals are **model-agnostic**: they read whatever model name and token counts Ollama returns in the `done:true` chunk for *any* model the user runs; no model is assumed. Divide-by-zero on cache-hit (`eval_duration == 0`) yields `None`, never an exception.
4. **Graceful degradation everywhere.** Every collector is individually `try/except`-wrapped and returns `None`/`[]` on failure. A partial `MachineSnapshot` with `None` fields is valid and still emitted. Specific degradation paths that MUST be the tested default: `dmesg_restrict=1` (Xid scan denied), PSI absent (kernel < 4.20 / container), RAPL absent (no powercap, ARM, container), unknown CPU sensor (neither k10temp nor coretemp), no block device matching the portable base-device regex, `nvidia-smi` `[N/A]` fields (P8/D3 power states, memory-junction temp on pre-Ampere), `psutil.AccessDenied` on `io_counters()`, `pmon` unsupported on older drivers, and — added in rev. 3 — **non-NVIDIA / no-GPU hosts where the active backend is `StubBackend`** (all GPU signals `None`/`[]`; this is the *correct complete* implementation for that hardware, **not** a degraded one). No degradation path may emit a misleading `0` (e.g., skip the VRAM-drift gauge when the backend returns `None` rather than publishing drift=0).
5. **Collection cost & cadence (two-cadence model, monotonic-anchored).** A single canonical **fast tick (2s)** and **slow tick (10s/30s)** govern all collection. Fast path = cheap reads (one extended GPU status query via the backend, `/proc` text reads, psutil counter deltas). Slow path = subprocess-heavy or rarely-changing signals (throttle reasons 10s, PCIe tx/rx 10s, GPU process `pmon` 10s, process churn 10s, Xid `dmesg` 30s). The fast GPU status query is extended from 5 fields to ~12 in **one** backend call (`NvidiaBackend` issues one `nvidia-smi` subprocess; `StubBackend` returns an empty `GPUStatus()`); no second subprocess on the fast path. The loop is **monotonic-anchored**: it records `collection_start = time.monotonic()` before the work and sleeps `max(0.0, 2.0 - elapsed)` so a slow `nvidia-smi` (up to its 5s timeout during a GPU lockup) does not compound drift (addresses constraint-lens nice-to-have). No collector blocks the scheduler hot loop; subprocess work uses `asyncio.create_subprocess_exec` with the existing timeout pattern.
6. **No new heavy dependencies.** Everything uses the existing stack: `asyncio` + `httpx` + FastAPI/uvicorn + Pydantic v2 + PyYAML + (optional) `psutil` + (optional) `prometheus_client`. The `mcp` SDK is the only new dependency and is gated behind the existing optional `bastion[mcp]` extra (ADR-007), not the base install. No vendor GPU SDK (NVML, ROCm bindings, Level Zero) is added — backend implementations shell out to vendor CLIs (`nvidia-smi`, and in a future track `rocm-smi`/`intel_gpu_top`) exactly as `NvidiaBackend` does today.
7. **Hardware & Model Portability (public repo) — CROSS-CUTTING (new in rev. 3).** BASTION ships publicly; downstream users run *other* hardware and *other* models. The design MUST NOT hard-code the original developer's box (consumer NVIDIA RTX 5090, AMD CPU via k10temp, `nvme0n1`/`/mnt/nvme_data`, 85 °C / 200 MB/s / 300 W constants). Concretely:
   - **(a) Hardware/model-agnostic.** Do not assume NVIDIA-only, single-GPU, consumer-GPU, a specific CPU sensor, a specific block device, or a specific model. Support (or at minimum do not *preclude*) `{NVIDIA, AMD, Intel-iGPU, no-GPU, multi-GPU, server-GPU}` for the GPU, `{Intel-RAPL, AMD-RAPL/amd_energy, no-RAPL}` for package power, `{k10temp, coretemp, unknown/any hwmon}` for CPU temperature, `{nvme*, sd*, vd*, mmcblk*}` for block storage, `{PSI present, PSI absent}` for pressure, and **arbitrary models** for the inference signals.
   - **(b) Config-driven with auto-detected defaults.** Every numeric threshold (GPU temp ceiling, GPU power limit/TDP, NVMe MB/s, swap rates, PSI alert levels, CPU safe ceiling) is a documented config key (see Section 4.8). Defaults are **auto-detected from the device wherever the device can report them** — e.g. the GPU thermal ceiling and power limit come from the GPU's own driver-reported `temperature.gpu.tlimit`/`temperature.gpu.shutdown`/`power.limit` at startup (extending the existing `resolve_gpu_defaults` VRAM auto-detect at `config.py:111`), **not** a literal — and fall back to a documented constant only when the device is silent (`[N/A]`, `StubBackend`, container).
   - **(c) GPU access via the protocol seam.** All GPU signals are reached through the existing `GPUBackend` protocol (`gpu/base.py`), with `NvidiaBackend` implementing the nvidia-smi field parsing and `StubBackend` (covering AMD/Intel/no-GPU until dedicated backends exist) returning `None`/`[]`. nvidia-smi field names (`utilization.gpu`, `clocks_throttle_reasons.*`, `temperature.memory`, `pcie.link.*`, `NVRM: Xid`) live **only inside `NvidiaBackend`** — never inline in `correlation.py`, `server.py`, the collectors, or the `_machine_snapshot_loop`. New GPU signals get **new protocol methods/fields**, not inline nvidia-smi assumptions in higher layers.
   - **(d) Dynamic device discovery.** Do not assume `nvme0n1`. Discover block devices by a portable base-device regex over `psutil.disk_io_counters(perdisk=True)` keys. Handle **both** Intel (`/sys/class/powercap/intel-rapl:0/energy_uj`) and AMD (`amd_energy` hwmon / `/sys/class/powercap` AMD domains) RAPL. Handle **both** `k10temp` and `coretemp` CPU sensors, and degrade gracefully (try a documented priority list, then any `temp*_input`, then `None`) on unknown ones. Discover mount points dynamically rather than hard-coding `/mnt/nvme_data`.
   - **(e) Graceful degradation is the tested default for every "other hardware" path.** No GPU, non-NVIDIA GPU, multi-GPU, no PSI (old kernel / container), no RAPL, unknown CPU sensor, denied dmesg — each is exercised by a unit test asserting a partial snapshot with `None` fields and **never a misleading `0`** (Section 10.3).
   - **SCOPE GUARDRAIL (guard both directions).** The deliverable is portable **seams + config + graceful degradation** so the design does not *preclude* other hardware. It is **NOT** a mandate to build full multi-vendor backends now (e.g. a complete AMD GPU backend). That stays a future track (Tier 4). Do not over-scope. Equally, do not leave NVIDIA-only assumptions baked into the higher layers: every GPU touchpoint above `NvidiaBackend` must already be `None`-tolerant and protocol-routed.

---

## 4. Unified Data Model — `MachineSnapshot`

The collection layer populates **one** canonical, fully-correlated Pydantic v2 model per tick. This merges every cluster's `unified_model_contributions` into a single surface-independent container (ADR-009 data-model principle). All new fields are `Optional` with `None` defaults for backward compatibility; nothing existing is modified destructively.

### 4.1 Top-level container

```python
class MachineSnapshot(BaseModel):
    snapshot_ts: float                                   # time.time() at collection
    broker: BrokerStatus                                 # existing model, promoted in
    gpu: GPUStatus                                       # existing model, EXTENDED (4.2)
    gpu_extended: GPUExtendedStatus | None = None        # slow-path GPU signals (4.3)
    contention: ContentionSnapshot | None = None         # host pressure (4.4)
    process: ProcessSnapshot | None = None               # per-process attribution (4.5)
    inference: InferenceThroughputState | None = None    # stream-tapped LLM rates (4.6)
    correlation: CorrelationState | None = None          # engine outputs (4.7)
```

`MachineSnapshot.model_dump()` round-trips through JSON identically to the existing `BrokerStatus` pattern and is the payload of `GET /broker/snapshot`. The `_machine_snapshot_deque: deque[dict]` (`maxlen=180`) stores `model_dump()` results so `?history=N` slices need no re-serialization (mirrors the existing `_recent_requests` deque).

**Multi-GPU forward-compatibility (rev. 3).** `MachineSnapshot.gpu` is a single `GPUStatus` reporting the configured GPU (`gpu.gpu_index`, default 0). This is **single-GPU now, list-extensible later**: because `GPUStatus` carries a `gpu_index: int = 0` field (4.2), a future multi-GPU build can emit `gpu_list: list[GPUStatus] | None = None` as an *additive* field without breaking the existing scalar `gpu`. The spec does **not** build multi-GPU iteration now (scope guardrail), but it does not harden the schema against it.

### 4.2 `GPUStatus` extensions (fast path, populated by the backend's extended status query)

Eleven new optional fields + a `gpu_index` field + one computed field, all `None`-/`0`-default:

```python
gpu_index: int = 0                          # which GPU this row describes (multi-GPU seam)
compute_utilization_pct: int | None        # utilization.gpu        (NvidiaBackend)
memory_bandwidth_utilization_pct: int | None  # utilization.memory  (NvidiaBackend)
sm_clock_mhz: int | None                    # clocks.sm             (NvidiaBackend)
gr_clock_mhz: int | None                    # clocks.gr             (NvidiaBackend)
mem_clock_mhz: int | None                   # clocks.mem            (NvidiaBackend)
fan_speed_pct: int | None                   # fan.speed (READ; distinct from write-path)
memory_junction_temp_c: int | None          # temperature.memory (GDDR junction)
pcie_link_gen_current: int | None
pcie_link_gen_max: int | None
pcie_link_width_current: int | None
pcie_link_width_max: int | None

@computed_field
def pcie_downgraded(self) -> bool:          # True iff (cur_gen < max_gen OR cur_width < max_width)
    ...                                      #   AND all four are non-None (partial-data guard)
```

**Provenance & portability (rev. 3).** All eleven value fields are populated **by `NvidiaBackend`** from the single extended `nvidia-smi --query-gpu` call and remain `None` from `StubBackend` (non-NVIDIA / no-GPU). The nvidia-smi field-name comments above are documentation of the `NvidiaBackend` implementation, **not** a contract any higher layer parses. Specific "other hardware" expectations, so implementors do not treat `None` as a bug:
- `memory_junction_temp_c` (`temperature.memory`) returns `[N/A]→None` on **pre-Ampere NVIDIA** (Pascal/most Turing) and on **all AMD/Intel** — so `None` is the *expected* value on most hardware, not a degradation special-case.
- `fan_speed_pct` (`fan.speed`) returns `[N/A]→None` on **fanless server GPUs** (A100/H100/L40 use facility cooling) and on BIOS-auto passive cards. `None` here means "this GPU has no readable fan," which the TUI/correlation engine must tolerate (see 5.1 and 6.5).
- All PCIe fields are `None` on `StubBackend`/non-NVIDIA, so `pcie_downgraded` returns **False** (all-four-non-None guard) — never a false "downgraded" alarm on hardware that simply does not expose PCIe link state via the active backend.

**`pcie_downgraded` guard extension (rev. 3):** the computed field returns `True` only when all four link fields are non-`None` **and** a downgrade is present. On partial data **and on all non-NVIDIA hardware** it returns `False`. (Rev. 2 already noted the partial-data case; rev. 3 makes the non-NVIDIA case explicit.)

**Reconciliation note:** the correlation-engine section proposed adding `throttle_reasons_mask: int | None` and `fan_speed_pct` to `GPUStatus`. This spec resolves the overlap: `fan_speed_pct` is the single canonical field on `GPUStatus` (4.2); throttle reasons live on `GPUExtendedStatus` as a decoded `list[str]` (`throttle_reasons`, 4.3), **not** a raw bitmask on `GPUStatus`. The correlation engine consumes the decoded list. There is no `throttle_reasons_mask` field — decoding happens at collection inside `NvidiaBackend`, not in the model.

### 4.3 `GPUExtendedStatus` (slow path; NOT embedded in `BrokerStatus` to keep the 2s fast path free of 30s-stale data)

```python
class GPUExtendedStatus(BaseModel):
    throttle_reasons: list[str] = []        # e.g. ['sw_thermal_slowdown','hw_power_brake_slowdown']
    pcie_tx_kb_s: int | None = None
    pcie_rx_kb_s: int | None = None
    recent_xids: list[XidEvent] = []        # bounded list, maxlen=20 at collection
    xid_count_since_start: int = 0
    last_polled_at: float = 0.0

class XidEvent(BaseModel):
    timestamp: str
    xid_code: int                           # NVIDIA Xid code today; a generic device error-code
                                            #   field a future AMDBackend can reuse (see 5.1 docstring note)
    raw_message: str
```

**Portability (rev. 3).** Every field here is populated by `NvidiaBackend` slow-path methods and is empty/`None` from `StubBackend`. `throttle_reasons` and `recent_xids` are **NVIDIA concepts**; on non-NVIDIA hardware the empty list is the *correct complete* value (see 5.1). The `xid_code` int is documented as a **generic device error-code field**, not Xid-semantics-locked, so a future `AMDBackend` could map amdgpu reset events onto the same `XidEvent` structure and reuse the `bastion_gpu_xid_errors_total{xid_code}` metric without a schema change.

Exposed via `GET /broker/gpu/extended` (separate endpoint, consistent with `/broker/watchdog` and `/broker/thrashing` being separate from `/broker/status`). **Dual-registration applies** (Section 4.10).

### 4.4 `ContentionSnapshot` (host pressure)

```python
class ContentionSnapshot(BaseModel):
    psi_cpu_some_avg10: float | None = None
    psi_cpu_full_avg10: float | None = None
    psi_mem_some_avg10: float | None = None
    psi_mem_full_avg10: float | None = None
    psi_io_some_avg10: float | None = None
    psi_io_full_avg10: float | None = None
    swap_in_rate_mb_s: float | None = None
    swap_out_rate_mb_s: float | None = None
    block_devices: list[BlockDeviceIOStats] = []   # renamed from nvme_devices (rev. 3): covers nvme*/sd*/vd*/mmcblk*
    cpu_package_watts: float | None = None          # host RAPL — Intel OR AMD source (5.2)
    gpu_board_watts: float | None = None            # Tier-4: backend-provided; None until an AMD/Intel backend fills it (Section 2)
    oom_kill_total: int | None = None
    oom_kill_rate: float | None = None
    sampled_at: float = Field(default_factory=time.time)

class BlockDeviceIOStats(BaseModel):
    device: str                                  # discovered base device (nvme0n1 / sda / vdb / mmcblk0), never a partition
    util_pct: float                              # busy_time delta / elapsed (the canonical device util%)
    read_await_ms: float | None = None
    write_await_ms: float | None = None
    read_rate_mb_s: float
    write_rate_mb_s: float
```

**Rev. 3 model changes:**
- `nvme_devices: list[NVMeIOStats]` → **`block_devices: list[BlockDeviceIOStats]`** so the model name does not imply NVMe-only. The model is otherwise identical (the `device` field still carries a base device name, never a partition). SATA SSDs (`sdX`), virtio (`vdX`), and eMMC (`mmcblkX`) populate it on hosts that have them; NVMe hosts are unchanged.
- `cpu_package_watts` is explicitly a **host** package-power signal sourced from RAPL **Intel or AMD** (5.2), not Intel-only.
- `gpu_board_watts` is documented as **backend-provided and `None` until a backend fills it** (NVIDIA fills `GPUStatus.power_draw_watts` instead; a future AMD/Intel backend would fill `gpu_board_watts` inside the backend), not "None because the dev box is NVIDIA."

`gpu_board_watts` remains a declared field (so the model is forward-compatible when a non-NVIDIA backend fills it) but is **never populated from a host-side hwmon scan**, and `GPUStatus.power_draw_watts` is **not** filled from it. Exposed via `GET /broker/contention` (dual-registration, 4.10).

### 4.5 `ProcessSnapshot` (per-process attribution — TUI + JSON only, never Prometheus labels)

```python
class ProcessSnapshot(BaseModel):
    top_processes: list[ProcessRow] = []
    gpu_processes: list[ProcessGPURow] = []
    own_pids: dict[int, str] = {}                # pid -> role ('bastion'|'ollama')
    watchlist_hits: list[ProcessRow] = []
    recent_churn_events: list[ProcessChurnEvent] = []
    collected_at: float
    gpu_collected_at: float | None = None        # slow-tick GPU sub-data age

class ProcessRow(BaseModel):
    pid: int; name: str
    cpu_pct: float | None = None; rss_mb: float | None = None
    io_read_bytes_s: float | None = None; io_write_bytes_s: float | None = None
    is_inference_owned: bool = False; role: str | None = None; watchlisted: bool = False
    gpu_row: ProcessGPURow | None = None

class ProcessGPURow(BaseModel):
    pid: int; name: str
    vram_mb: int | None = None
    sm_pct: int | None = None; mem_pct: int | None = None
    enc_pct: int | None = None; dec_pct: int | None = None
    is_inference_owned: bool = False; role: str | None = None

class ProcessChurnEvent(BaseModel):
    timestamp: float; new_count: int; exited_count: int; new_names: list[str]
```

`gpu_processes` is populated through the `GPUBackend` (`query_processes()` / a new `query_process_utilization()` for pmon data) and is **empty on `StubBackend`** — so on non-NVIDIA / no-GPU hosts the panel shows CPU/IO/watchlist/churn but no GPU rows, with no error. Exposed via `GET /broker/processes` (dual-registration, 4.10). **Not** embedded in `BrokerStatus` (too large/volatile).

### 4.6 `InferenceThroughputState` (stream-tapped)

The token-derived signals write into both the existing `_recent_requests` deque (per-request) and an aggregate snapshot. These signals are **model-agnostic** — they read whatever model name and token accounting Ollama emits for any model the user runs.

**`record_recent_request()` signature change (load-bearing, explicit).** The current signature (server.py:197) is:

```python
def record_recent_request(
    model, endpoint, tier, queue_wait_s, duration_s, status_code,
    streaming=False, source=None,
) -> None: ...
```

It gains **six new optional keyword parameters, all `None`-default**, appended after `source`:

```python
def record_recent_request(
    model, endpoint, tier, queue_wait_s, duration_s, status_code,
    streaming=False, source=None,
    *,
    prefill_tps: float | None = None,
    decode_tps: float | None = None,
    ttft_s: float | None = None,
    ctx_utilization: float | None = None,
    eval_count: int | None = None,
    prompt_eval_count: int | None = None,
) -> None: ...
```

Because every new parameter defaults to `None`, **all existing call sites that omit them keep working unchanged** (mandatory regression test). The six values are added to the `_recent_requests` dict alongside the existing keys. The `InferenceTapCollector.flush()` (Section 5.4) is the only caller that passes them. The aggregate:

```python
class InferenceThroughputState(BaseModel):
    decode_tps_p50: float | None = None
    prefill_tps_p50: float | None = None
    ttft_p50_s: float | None = None
    ctx_utilization_p50: float | None = None
    last_model: str | None = None
    sampled_at: float = Field(default_factory=time.time)
```

### 4.7 `CorrelationState` (engine outputs; see Section 6)

```python
class CorrelationState(BaseModel):
    risk_index: RiskIndexResult | None = None
    thermal_coupling: ThermalCoupling | None = None
    recent_contentions: list[ContentionEvent] = []
    enriched_stall_reason: str | None = None
    ring_size: int = 0
    recent_ring_events: list[CorrelationEvent] = []   # bounded tail (default last 32) — see 6.1
```

Supporting models (all new in `models.py`): `RiskIndexResult` (`score: float`, `level: Literal['nominal','elevated','high','critical']`, `component_scores: dict[str,float]`, `dominant_factor: str`), `ThermalCoupling` (`cpu_temp_c`, `gpu_temp_c`, `fan_speed_pct`, `coupling_active: bool`, `thermal_headroom_min_c`), `CorrelationEvent` (`ts_monotonic`, `ts_wall`, `domain: Literal['gpu','system','inference','scheduler']`, `kind: str`, `payload: dict`), `ContentionEvent` (extends `CorrelationEvent` + `attribution: str`, `inference_was_stalled: bool`, `stall_reason_at_time: str | None`, `latency_spike_ratio: float | None`), `SystemSnapshot`/`PsiSnapshot` (engine-internal tick buffers).

The full event ring is **not** a standalone endpoint (review: YAGNI). Its last-N tail rides inside `CorrelationState.recent_ring_events` on `/broker/snapshot`; the full 512-entry ring is reachable only via the debugging query parameter `GET /broker/snapshot?include_ring=true` (Section 5.6).

### 4.8 Cross-cluster model reconciliations & the `observability:` config block

- **GPU board power fill — REMOVED from the NVIDIA path; reframed as backend-provided (rev. 3).** Rev. 1 proposed filling `GPUStatus.power_draw_watts` from a system-contention `gpu_board_watts` (amdgpu hwmon). That host-side fill is deleted. On NVIDIA, `NvidiaBackend.query_status()` already populates `power_draw_watts`, so `health.check_gpu_safe()` fires correctly with zero new code. A future non-NVIDIA backend would populate board power **inside the backend** (`gpu_board_watts`), not via a host hwmon scan in a higher layer. `cpu_package_watts` (host RAPL, Intel **or** AMD — 5.2) is the only package-power signal that ships.
- **Single `/proc/vmstat` read.** `SystemDataCollector._read_vmstat()` is the canonical parser; swap-rate and OOM both consume its cached dict (one read per tick, not three).
- **`resolve_gpu_defaults` extension (rev. 3) — auto-detect the safety ceilings from the device, not just VRAM.** Today `config.py:resolve_gpu_defaults` (line 111) auto-detects `total_vram_gb` and `max_power_watts` from `nvidia-smi --query-gpu=name,memory.total,power.limit` and falls back to static defaults (VRAM 8 GB, power 300 W) when nvidia-smi is absent. Rev. 3 extends this same startup resolution to also resolve **`max_temperature_c`** and the new **`gpu_safe_ceiling_c`** from the device's own driver-reported limits, so BASTION works correctly out of the box on a 700 W / 93 °C-shutdown server GPU without manual config:
  - Query `nvidia-smi --query-gpu=temperature.gpu.tlimit,temperature.gpu.shutdown,power.limit --format=csv,noheader,nounits` at startup (one call, reuse the existing try/except/FileNotFoundError structure).
  - **`gpu.max_temperature_c` default** = `tlimit - 2` if `tlimit` is reported; else `shutdown - 10` if `shutdown` is reported; else `GPUProfile.thermal_ceiling_c` for the detected GPU name (`gpu_profiles.py`); else the static `83`. Record the resolved value in a startup INFO log so operators can verify it.
  - **`gpu.max_power_watts` default** = device `power.limit` (already wired); fall back to `300.0` with a startup **WARNING** when nvidia-smi is absent and the field is still at its default, so AMD-host operators are told to set `gpu.max_power_watts` to their TDP rather than silently inheriting 300 W.
  - On `StubBackend` / `[N/A]` / non-NVIDIA, all device queries return nothing and the static fallbacks apply with the INFO/WARNING logs above. (`gpu_profiles.py` `thermal_ceiling_c` per-SKU values become a *fallback of last resort*, below the device-reported value — the device is authoritative, the profile is the floor. Optional future track: a `rocm-smi --showmaxpower` probe as a secondary detection path after nvidia-smi fails.)
- **The `observability:` config block.** A new `observability:` block in `broker.yaml` (greenfield — no `observability` key exists today), modeled as `ObservabilityConfig` + a nested `CorrelationConfig`, both Pydantic models optional on `BrokerConfig` with default factories. **config.py wiring (explicit):** because both models carry default factories, an absent `observability:` block produces working defaults; but `config.py` MUST add the `observability` key to its `BrokerConfig` parse so a **present** YAML block is not silently ignored. This is a named Phase-1 task (Section 8). The full key list with documented defaults and auto-detect/discovery strategy:

  **`ObservabilityConfig`:**
  | Key | Default | Purpose / auto-detect or discovery strategy |
  |---|---|---|
  | `process_watchlist` | `[]` | List of process names or `pid:NNN` always shown in the attribution panel. |
  | `churn_threshold` | `5` | New-PID count per slow tick that fires a `ProcessChurnEvent`. Conservative; raise on busy/cron-heavy hosts. |
  | `ecc_enabled` | `false` | Opt-in slow-poll of GPU ECC error counters (Tier 4). Justified by server GPUs in the wild; off by default because consumer cards report `[N/A]`. |
  | `cpu_sensor_name` | `null` | Pin a specific hwmon `name` (e.g. `nct6775`, `zenpower`). If `null`: try priority list (`k10temp`,`coretemp`,`zenpower`,`nct6775`,`it87`,`acpitz`), then any dir exposing `temp1_input`. Discovered sensor logged at INFO. |
  | `rapl_domain_path` | `null` | Pin a specific RAPL energy path. If `null`: probe Intel `/sys/class/powercap/intel-rapl:0/energy_uj`, then AMD `amd_energy` hwmon `power1_input` / AMD powercap domain, then `None` (one-time DEBUG). |
  | `storage_device_filter` | `null` | Explicit allow-list of base device names. If `null`: match `psutil` perdisk keys against `^(nvme\d+n\d+\|sd[a-z]+\|vd[a-z]+\|mmcblk\d+\|hd[a-z]+)$` (no partition suffixes; loop/dm devices excluded). |
  | `disk_mount_labels` | `null` | Optional `dict[str,str]` of mount→label for the TUI disk panel. If `null`: discover real mounts via `psutil.disk_partitions(all=False)` filtered to physical devices. Removes the developer-specific `/mnt/nvme_data` hard-code. |
  | `psi_io_full_warn_pct` | `5.0` | PSI io_full avg10 TUI warn threshold (moved out of `helpers.py` magic literals into config). Containers/slow-IO hosts may need higher. |
  | `psi_io_full_crit_pct` | `25.0` | PSI io_full avg10 TUI critical threshold (companion to the above). |

  **`CorrelationConfig`** (nested under `observability.correlation:`):
  | Key | Default | Purpose / auto-detect strategy |
  |---|---|---|
  | `ring_maxlen` | `512` | Correlation ring capacity. |
  | `ring_tail_in_snapshot` | `32` | Last-N ring events embedded in `/broker/snapshot`. |
  | `contention_block_write_mb_s_threshold` | `200.0` | Block-device write-throughput contention threshold (MB/s), applies to **all** discovered block devices (`nvme*/sd*/vd*/mmcblk*`), not NVMe only. Device-dependent: tune to ~50–70% of the drive's observed sustained write rate (enterprise NVMe → 2000+; SATA/eMMC → ~100). Startup INFO logs the active value. Dynamic idle-calibration is the recommended long-term default (Tier 4). |
  | `contention_psi_threshold` | `20.0` | `mem_psi_some_avg10` contention threshold. |
  | `contention_cpu_psi_threshold` | `60.0` | `cpu_psi_some_avg10` contention threshold (separate unit from the mem PSI knob). |
  | `contention_hysteresis_ticks` | `2` | Consecutive ticks a leg must exceed threshold before emitting (kills transient spikes). |
  | `cpu_safe_ceiling_c` | `85.0` | CPU thermal ceiling for the headroom formula (6.5). Documented fallback; differs by CPU (Ryzen 7000 Tjmax 95, EPYC 90, Cortex-A 105). Startup INFO logs it and points to this key. **Not** auto-detected from `temp1_crit` (absent on many hwmon drivers). |
  | `gpu_safe_ceiling_c` | `null` | GPU thermal ceiling for the headroom formula (6.5). **Auto-detected from the device** via the `resolve_gpu_defaults` extension above (bound to `gpu.max_temperature_c`, which itself comes from `tlimit`/`shutdown`). `null` ⇒ use `gpu.max_temperature_c`; if that is unset/0 (no-GPU), the GPU headroom term is skipped (CPU-only headroom). |
  | `risk_weights` | see 6.4 | Per-component RiskIndex weights (config-tunable). |

  The block-device contention default (`contention_block_write_mb_s_threshold`) is **200.0 MB/s sustained write** (not 500): 500 MB/s sits above the sustained mixed-load write rate of many consumer NVMe drives, so the detector would rarely fire; 200 MB/s targets mid-range consumer NVMe. The threshold governs **whatever base devices `block_devices` discovered** (`nvme*/sd*/vd*/mmcblk*`), not NVMe specifically. Per the portability principle this is explicitly **device-dependent** — `broker.yaml.example` carries a tuning note (Section 8), the active value is logged at startup, and dynamic calibration (Tier 4) is the recommended portable default.

### 4.9 The one collection tick (CRITICAL reconciliation)

Five cluster sections refer to "the existing `_gauge_update_loop`." **This loop does not exist** — the 2026-03-13 spec *proposed* it (Section 2, "Periodic Gauge Updater") but it was never built (verified: zero matches for `_gauge_update_loop` / `gauge_update` across `src/` and `tests/`). Therefore this spec introduces it as **net-new** and makes it the single canonical collection authority:

```python
async def _machine_snapshot_loop():   # NEW; started in lifespan() after _sweep_task (server.py ~751)
    tick = 0
    while True:
        collection_start = time.monotonic()           # monotonic anchor (Constraint #5)
        try:
            snap = await _collect_machine_snapshot(tick)   # fast every tick; slow gated by tick%5, tick%15
            _machine_snapshot_deque.appendleft(snap.model_dump())
            _update_prometheus_from_snapshot(snap)          # wakes dead gauges; no new queries
            await _correlation_engine.tick(snap)            # engine consumes the same snapshot
        except Exception:
            logger.exception("snapshot loop iteration failed")  # loop never dies
        tick += 1
        elapsed = time.monotonic() - collection_start
        await asyncio.sleep(max(0.0, 2.0 - elapsed))    # cadence stable under slow nvidia-smi
```

- **Fast path (every 2s tick):** the backend's extended status query (GPUStatus 4.2 — `NvidiaBackend` returns 12 fields, `StubBackend` returns an empty `GPUStatus()`), PSI/swap/block-device-IO/CPU-package-power (4.4), top-N CPU+IO processes (4.5), the dead-metric Prometheus updates (Tier 0), **including `GPU_TEMPERATURE`** — the die-temp value is already in hand from the fast-path status query, so it is updated on the 2s fast tick, not the 10s slow tick. When the backend returns `None` for `temperature_c` (StubBackend/non-NVIDIA), the gauge is **skipped**, never set to 0.
- **Slow path (tick % 5 == 0 → 10s):** throttle reasons, PCIe tx/rx, GPU `pmon` process data, process churn, VRAM-drift gauge. All GPU sub-items route through `GPUBackend` slow-path methods and yield `[]`/`None` on `StubBackend`.
- **Slowest path (tick % 15 == 0 → 30s):** Xid `dmesg` scan (via `NvidiaBackend.query_xid_errors()`; `[]` on StubBackend), own-PID registry refresh.

The dashboard does **not** run this loop; it polls `GET /broker/snapshot` over HTTP (TUI is a client, ADR-005). The broker owns collection so `/broker/snapshot`, `/broker/processes`, etc. work headless. The dashboard's own client-side `SystemDataCollector` (CPU/mem/net/disk not in the broker snapshot) and the broker's instance are independent; each tracks its own delta state. **psutil cpu_percent caveat:** per-process `cpu_percent()` needs a ≥0.1 s interval between successive calls per process to be meaningful. Both instances call it on independent cadences (TUI 2s render, broker 2s tick), each safely above 0.1 s; they do **not** share a `Process` object, so neither resets the other's interval baseline. Process attribution is owned by the **broker-side** collector folded into `_machine_snapshot_loop`; the TUI consumes it via `GET /broker/processes` rather than running a second process scan against the same PIDs.

### 4.10 Dual-factory route registration (CRITICAL — corrects rev. 1's false "shared router" claim)

**Rev. 1 was factually wrong.** `server.py` defines **two completely independent** FastAPI apps, each with its **own local** `broker_router`:
- `create_app()` declares `broker_router = APIRouter(prefix="/broker", …)` at **server.py:836** and calls `app.include_router(broker_router)` at **server.py:1570**.
- `create_admin_app()` declares a **separate** `broker_router` at **server.py:1707** and calls `app.include_router(broker_router)` at **server.py:2325**.

The two routers share no state. Every existing `/broker/*` route is duplicated verbatim across both factories (~600 lines each). **Therefore every new endpoint this spec adds — `GET /broker/snapshot` (+ `?history`/`?include_ring`), `GET /broker/contention`, `GET /broker/gpu/extended`, `GET /broker/processes`, `GET /broker/correlation/risk`, `GET /broker/correlation/contentions` — MUST be registered in BOTH `create_app` and `create_admin_app`, or it 404s in the admin-only two-port deployment.**

**Implementation pattern (mandatory):** extract each handler to a standalone module-level coroutine (e.g., `async def _handle_snapshot(request): ...`) and register it in both factories via `broker_router.add_api_route("/snapshot", _handle_snapshot, methods=["GET"])` (or the decorator form repeated in each factory). This keeps the handler body single-sourced while satisfying both routers. The long-term fix — hoisting a shared module-level `broker_router` that both factories `include_router` — is a ~1-day refactor that is **explicitly out of scope here** and flagged as a follow-up; this spec does not assume it exists. The dual-registration requirement is restated in every build phase that adds a route (Section 8) and is the corrected resolution of the "duplicate-route trap" (Section 10.1).

---

## 5. Per-Domain Designs

### 5.1 GPU Device Cluster (Tier 1–2) — all signals via the `GPUBackend` protocol seam

Transforms `GPUStatus` from a 5-field snapshot into a full device-health record. **All GPU access is through the `GPUBackend` protocol (Constraint #7c).** `NvidiaBackend` implements the nvidia-smi field parsing; `StubBackend` (the active backend on AMD / Intel-iGPU / no-GPU hosts until dedicated backends exist) returns `None`/`[]` for every signal — which is the *correct complete* implementation on that hardware, not a degraded one. Fast-path signals extend the single backend status query (`NvidiaBackend.query_status`, `nvidia.py:22`, async — from 5 to ~12 nvidia-smi fields); slow-path signals (throttle, Xid, PCIe tx/rx) become **new async protocol methods**. CSV parsing inside `NvidiaBackend` becomes **per-field** (`_safe_int`/`_safe_float` per position) so a driver returning fewer columns degrades per-field rather than dropping everything after the first gap.

**Multi-GPU note (rev. 3, applies to the whole cluster):** `NvidiaBackend.query_status()` today takes `output.strip().split("\n")[0]` (`nvidia.py:52`) — i.e. **GPU 0 only**. Rev. 3 makes this explicit and parametric rather than silently hardened: add `gpu.gpu_index: int = 0` to `GPUConfig`, pass it to `NvidiaBackend`, and select `lines[gpu_index]` (a one-line change in `nvidia.py`). This is **single-GPU now, list-extensible later**: emitting all GPUs with `gpu_index` labels is a future track (Section 2). The shipped path reports the configured GPU; on a multi-GPU host without config it reports GPU 0 — documented, not accidental.

| Signal | What | Why | Source (backend method) | Collection + Cadence | Files | Surface | Effort | Risks (incl. other-hardware) | Tests |
|---|---|---|---|---|---|---|---|---|---|
| Compute+mem utilization | `compute_utilization_pct`, `memory_bandwidth_utilization_pct` (%) | util≈0 during a queue stall ⇒ scheduler/PCIe issue; util≈100 ⇒ genuinely busy; mem-util≈100 with VRAM headroom ⇒ bandwidth saturation not capacity | `NvidiaBackend.query_status` (appends `utilization.gpu,utilization.memory` to the existing async call); `StubBackend`→`None` | Fast, **2s** (piggybacks `query_status`) | `nvidia.py`, `models.py`, `panels_gpu.py`, `metrics.py`, `test_health.py`, `test_panels_gpu.py` | TUI GPUPanel; Prometheus `bastion_gpu_compute_utilization_pct`, `bastion_gpu_memory_utilization_pct` (no labels); JSON `/broker/status.gpu` | S | `[N/A]` in P8/D3 states → `_safe_int`→`None`; non-NVIDIA → `None` via StubBackend | Mock stdout `'55,8192,24576,32768,185.50,87,45'` → `compute_utilization_pct==87`, `memory_bandwidth_utilization_pct==45`; `[N/A]`→None; **StubBackend.query_status()→all None** |
| Throttle reasons | `clocks_throttle_reasons.{sw_thermal,hw_thermal,hw_power_brake,sw_power_cap,gpu_idle}` collapsed to `list[str]` | Best "why is it slow" signal; `hw_power_brake` is the PCIe-transient crash fingerprint from CRASH_ROOT_CAUSE | **New protocol method** `query_throttle_reasons() -> list[str]`. `NvidiaBackend` parses `clocks_throttle_reasons.*` in a **second** nvidia-smi call (boolean fields mis-align with numerics in one CSV pass). `StubBackend`→`[]`. A future `AMDBackend` would read `rocm-smi`/sysfs and map vendor reasons onto the fixed enum below | Slow, **10s**, async | `nvidia.py`, `gpu/base.py`, `gpu/stub.py`, `models.py`, `server.py`, `metrics.py`, `panels_gpu.py`, `test_health.py` | TUI (comma-joined, red if any `hw_*`); Prometheus `bastion_gpu_throttle_events_total{reason}` **Counter, rising-edge**; JSON `/broker/gpu/extended` | M | Field names differ pre-R525; non-zero exit ⇒ `[]` never crash; restart resets rising-edge baseline (1 spurious non-emit, acceptable); **non-NVIDIA → `[]` (correct, complete)**. The reason vocabulary is a **fixed enum** `{sw_thermal_slowdown, hw_power_brake_slowdown, hw_thermal_slowdown, sw_power_cap_slowdown}`; any future backend maps its vendor terms onto this set before emitting the counter (keeps the counter bounded) | `'Active,Not Active,Active,Not Active,Not Active'`→`['sw_thermal_slowdown','hw_power_brake_slowdown']`; timeout→`[]`; **StubBackend→`[]`**; counter increments on rising edge only |
| Clock speeds | `sm_clock_mhz/gr_clock_mhz/mem_clock_mhz` (MHz) | Quantifies throttle impact: throttle says *why*, clocks say *by how much* (2520→2000 MHz ≈ 20% slower inference) | `NvidiaBackend.query_status` (appended `clocks.sm/gr/mem`); `StubBackend`→`None` | Fast, **2s** | `nvidia.py`, `models.py`, `panels_gpu.py`, `app.py`, `test_health.py`, `test_panels_gpu.py` | TUI `SM: 2520MHz Mem: 10501MHz` + sparkline deques; JSON `/broker/status.gpu` | S | **Multi-GPU returns one line/GPU; `NvidiaBackend` selects `lines[gpu_index]` (gpu.gpu_index, default 0) — single-GPU now, list-extensible later (rev. 3)** | Extend happy-path mock with 3 cols; `gpu_index=1` selects 2nd line; sparkline deque init test |
| Fan speed (read) | `fan_speed_pct` (%) | Closes the auto-fan loop (verify commanded speed reached); fan at 100% with temp still rising is a pre-throttle crash precursor | `NvidiaBackend.query_status` (appended `fan.speed`, read-only; distinct from write-path `gpu_fan_control_wrapper`); `StubBackend`→`None` | Fast, **2s** | `nvidia.py`, `models.py`, `panels_gpu.py`, `app.py`, `test_health.py`, `test_panels_gpu.py` | TUI `Fan: 87%` color-coded (<70 green/70-90 yellow/≥90 red); **row omitted entirely when `fan_speed_pct` is None for >N consecutive ticks** (fanless server GPU), not a blank/0 row; JSON | S | `[N/A]` → None on **BIOS-auto passive cards AND fanless server GPUs (A100/H100/L40 use facility cooling)** and on non-NVIDIA; no write-path conflict. The correlation engine's `ThermalCoupling.fan_speed_pct` must tolerate None (no fan) — see 6.5 | Mock fan col → parses; `[N/A]`→None; **panel omits Fan row when None for N ticks**; thermal coupling tolerates fan=None |
| PCIe link state + throughput | `pcie_link_gen_{current,max}`, `pcie_link_width_{current,max}` (fast) + `pcie_tx_kb_s/pcie_rx_kb_s` (slow); derived `pcie_downgraded` | A silent Gen5×16→Gen4×4 downgrade after a power transient cuts load bandwidth 4× ⇒ every swap 4× slower ⇒ longer Xid exposure | gen/width appended to `query_status`; tx/rx via a new slow protocol method (R418+; `[N/A]` older). `StubBackend`→`None` for all | Fast **2s** (gen/width) + slow **10s** (tx/rx) | `nvidia.py`, `gpu/base.py`, `gpu/stub.py`, `models.py`, `server.py`, `panels_gpu.py`, `test_health.py`, `test_panels_gpu.py` | TUI `PCIe: Gen5 x16` / `Gen4 x4 DOWNGRADED` red; Prometheus `bastion_gpu_pcie_gen`, `bastion_gpu_pcie_width` (Gauges, no labels); JSON `/broker/gpu/extended` | M | tx/rx `[N/A]` pre-R418/virtualized → None + DEBUG; **on StubBackend/non-NVIDIA all four link fields are None so `pcie_downgraded` returns False — never a false-alarm "downgraded" on hardware that doesn't expose PCIe link state via the active backend (rev. 3)** | gen/width parse; downgraded=True when cur<max; downgraded=False on partial **and on StubBackend**; tx/rx slow mock |
| Xid error detection | `dmesg` scan for `NVRM: Xid (...) N,`; bounded `recent_xids` (maxlen 20) + `xid_count_since_start` | Xid errors are the kernel's GPU-fault channel; Xid 79 ("GPU fell off the bus") is the crash fingerprint seen in this project's consumer-GPU crash forensics, and Xid faults are a general NVIDIA hardware-health signal across SKUs. 30s detection surfaces it in AlertPanel + pages via Prometheus *before* inference returns 5xx | **New protocol method** `query_xid_errors() -> list[dict]`. `NvidiaBackend` runs async `dmesg --time-format iso --since '30 seconds ago'`, 5s timeout, rising-edge dedup keyed on `(ts,code)` sourced from the bounded `recent_xids` deque; fallback last 100 lines if no `--since`. **`StubBackend`→`[]`.** The `NVRM: Xid` literal lives **only inside `NvidiaBackend`** — never in `_machine_snapshot_loop` or `correlation.py` | Slow, **30s** | `nvidia.py`, `gpu/base.py`, `gpu/stub.py`, `models.py`, `server.py`, `metrics.py`, `panels_gpu.py`, `test_health.py` | TUI AlertPanel `GPU Xid N` red + GPUPanel `Xid: N recent`; Prometheus `bastion_gpu_xid_errors_total{xid_code}` (≤15 codes + `unknown`); JSON `/broker/gpu/extended` | M | **`dmesg_restrict=1` is the most likely path — must be the tested default** (returns `[]`, DEBUG); rc=1+empty stdout (rotated logs/unreadable `/dev/kmsg`) → `[]`; unknown codes bucket to `'unknown'`. **N/A on non-NVIDIA: Xid is an NVIDIA kernel-module concept; `StubBackend`'s `[]` is the correct and complete implementation, not a degradation (rev. 3)** | Mock dmesg `...Xid (PCI:0000:01:00) 79...`→one entry code=79; PermissionError→`[]`; rc=1+empty→`[]`; 2nd identical poll no re-emit; **StubBackend.query_xid_errors()→`[]`** |
| Memory junction temp | `memory_junction_temp_c` (°C) | GDDR6X/7 junction runs 10–15°C hotter than the die and throttles mem-clock independently: util stays high, tok/s drops | `NvidiaBackend.query_status` (appended `temperature.memory`); `StubBackend`→`None` | Fast, **2s** | `nvidia.py`, `models.py`, `panels_gpu.py`, `helpers.py`, `test_health.py`, `test_panels_gpu.py` | TUI `MemTemp: 98C` with **new** `mem_temp_color()` (95 yellow/105 red); Prometheus **new** `bastion_gpu_memory_junction_temp_celsius` (not a label on the frozen die-temp gauge) | S | **`[N/A]`→None on pre-Ampere NVIDIA (Pascal/most Turing) AND all AMD/Intel — `None` is the EXPECTED value on most hardware, a happy path, not a special-case degradation (rev. 3)** | Add col→parses; `[N/A]`→None; `mem_temp_color()` unit tests; row only when non-None |
| **Tier 0:** activate `GPU_TEMPERATURE` gauge | Wire the dead `update_gpu_temperature()` (defined, exported, **zero call sites** — verified) | Dead gauge = confusing Grafana gap; cardinality discipline requires defined metrics to emit | `GPUStatus.temperature_c` (already fetched on the fast path via the backend) | In `_machine_snapshot_loop`, **fast 2s** (value already in hand), guarded `if temperature_c is not None` | `server.py`, `test_metrics.py` | Prometheus only (`bastion_gpu_temperature_celsius` now emits) | S | Label-free (single-GPU); the `gpu_index` label is already permitted so multi-GPU is a non-breaking future extension (rev. 3); **None on StubBackend → skip, never 0** | Mock GPUStatus(temp=72)→`GPU_TEMPERATURE._value.get()==72`; None→skip |

**New backend methods on the `GPUBackend` Protocol (rev. 3 — these MUST be added to `gpu/base.py` AND `gpu/stub.py` in the same commit as the `NvidiaBackend` implementation, so the higher layers call them through the typed protocol and non-NVIDIA hosts inherit the correct empty contract):**

```python
# gpu/base.py — additions to the GPUBackend Protocol
async def query_throttle_reasons(self) -> list[str]: ...          # NvidiaBackend parses clocks_throttle_reasons.*; StubBackend -> []
async def query_xid_errors(self) -> list[dict]: ...               # NVRM Xid scan in NvidiaBackend; StubBackend -> []
                                                                  # docstring: 'xid_code' is a generic device error-code int,
                                                                  #   not Xid-locked; a future AMDBackend maps reset events onto it
async def query_pcie_throughput(self) -> tuple[int | None, int | None]: ...  # (tx_kb_s, rx_kb_s); StubBackend -> (None, None)
async def query_process_utilization(self) -> list[dict]: ...      # pmon sm%/mem%/enc%/dec% per PID; StubBackend -> []
# query_status (existing) is EXTENDED to populate the 11 new GPUStatus fields on NvidiaBackend; StubBackend still returns GPUStatus()
```

**Async-conversion of `query_processes` (rev. 3, see also 5.3).** The existing `GPUBackend.query_processes()` is **synchronous** (`base.py:31`, `nvidia.py:71` uses `subprocess.run`). Because it will be called from the async `_machine_snapshot_loop`, it MUST be converted to `async def query_processes(self) -> list[dict[str, str]]` on the **Protocol** (`base.py`), in `NvidiaBackend` (use `asyncio.create_subprocess_exec`, matching `query_status`), **and** in `StubBackend` — all in the same commit — and documented as a breaking protocol change in the CHANGELOG so any custom-backend implementer is notified. Keeping the protocol synchronous while `NvidiaBackend` goes async would break type-checking and mislead future backend authors. The `gpu/__init__.py` `query_gpu_processes` callers (`collectors.py:262`) become `await`-ed accordingly.

**Backend detection ordering (rev. 3 — documentation, no new backend built).** `gpu/__init__.py:detect_backend()` today is NVIDIA-or-stub: `if shutil.which("nvidia-smi"): NvidiaBackend() else StubBackend()`. The spec documents the **intended** ordering for when non-NVIDIA backends are eventually added (future track, Section 2), so `NvidiaBackend` is not silently instantiated on a mixed-vendor system and forced to ignore half the GPUs: (1) `nvidia-smi` present → `NvidiaBackend`; (2) `rocm-smi` present or `/sys/class/drm/renderD*/device/vendor == 0x1002` → future `AMDBackend`; (3) Intel iGPU sysfs / `intel_gpu_top` → future `IntelBackend`; (4) `StubBackend`. The detection hook gains a config override **`gpu.backend: auto|nvidia|amd|intel|stub`** (default `auto`) so operators can force a specific backend. Building the AMD/Intel backends is **out of scope** (scope guardrail); only the `auto`/`nvidia`/`stub` paths and the override plumbing ship now.

### 5.2 System Contention Cluster (Tier 1–2)

Host-pressure signals, none of which exist today. Pure extension: new collector methods, new fields, new metrics, the `/broker/contention` endpoint, a `ContentionPanel`. Follows the existing patterns (psutil import guard `collectors.py:12`, `read_cpu_temp` static `/sys` reads, `get_network_data` delta-rate). **All sources are discovered dynamically (Constraint #7d):** RAPL probes Intel **and** AMD paths; block-device IO matches a portable base-device regex (`nvme*`/`sd*`/`vd*`/`mmcblk*`), not `nvme0n1`; CPU temperature tries a priority sensor list then any `temp*_input`. Every signal degrades to `None`/`[]` on hosts that lack the source (containers, ARM, no-powercap kernels, non-NVMe storage). **GPU board power stays Tier-4 / backend-provided** (Section 2/4.8) — only host `cpu_package_watts` ships from this cluster.

| Signal | What | Why | Source (discovery strategy) | Collection + Cadence | Files | Surface | Effort | Risks (incl. other-hardware) | Tests |
|---|---|---|---|---|---|---|---|---|---|
| PSI pressure | `/proc/pressure/{cpu,memory,io}` some/full avg10 (+ io totals) | Most direct host-pressure indicator: `io.full avg10>0` / `cpu.full avg10>0` is exactly what an NVMe burst / mem / CPU contention looks like to the scheduler. Zero overhead, no perms | `Path.read_text()` ×3, split after `some`/`full` prefix | Fast **2s**, `get_psi_data()` | `collectors.py`, `panels_system.py`, `app.py`, `models.py`, `metrics.py`, `server.py` | TUI ContentionPanel rows + io_some sparkline; Prometheus `bastion_psi_some_avg10{resource}`, `bastion_psi_full_avg10{resource}` (3 bounded values); JSON `/broker/contention` | S | **Needs Linux 4.20+/CONFIG_PSI; absent on old kernels / many containers — `Path('/proc/pressure').exists()` once → all None if absent is a TESTED DEFAULT.** PSI warn/crit display thresholds now live in **`ObservabilityConfig` (`psi_io_full_warn_pct`/`psi_io_full_crit_pct`, defaults 5.0/25.0)** and are read by `helpers.py`, not magic literals (rev. 3) — containers/slow-IO hosts can raise them | Mock 2-line format→correct floats; **`exists()`=False→all None no exception (tested default)**; gauge label set = {cpu,memory,io}; red row when io_full ≥ configured crit |
| Swap in/out rate | `pswpin`/`pswpout` from `/proc/vmstat`, delta→pages/s→MB/s via `os.sysconf('SC_PAGE_SIZE')` | A model that fits VRAM should never swap; a `pswpin` spike during inference = "this stall is a memory problem, not NVMe." Distinct from swap *occupancy* | `_read_vmstat()` cached dict | Fast **2s**, `get_swap_rate_data()` | `collectors.py`, `panels_system.py`, `app.py`, `models.py`, `metrics.py`, `server.py` | TUI 2 rows (yellow>0.1, red>5 MB/s); Prometheus `bastion_swap_in_mb_s`, `bastion_swap_out_mb_s` (Gauges no labels); JSON | S | First read → None (delta needs prior); robust to missing keys | Two snapshots +100 pages/2s→correct MB/s; first→None; OSError→None |
| Block-device util + await | `psutil.disk_io_counters(perdisk=True)` `busy_time`/`read_time`/`write_time` deltas, **discovered base devices** | With mmap=false, weights stream from disk at swap; a competitor saturating the data drive turns a 2s swap into 10–30s. `busy_time>80%` during a stall = direct storage-contention evidence | `disk_io_counters(perdisk=True)` filtered by the portable base-device regex `^(nvme\d+n\d+\|sd[a-z]+\|vd[a-z]+\|mmcblk\d+\|hd[a-z]+)$` (or `observability.storage_device_filter` override) — **NOT hardcoded `nvme0n1` (rev. 3)** | Fast **2s**, `get_block_io_data()` | `collectors.py`, `panels_system.py`, `app.py`, `models.py`, `metrics.py`, `server.py` | TUI per-device `nvme0/sda util% await_ms` (red>80, yellow>50); Prometheus `bastion_block_device_util_pct{device}`, `bastion_block_device_await_ms{device,op}` (1–8 drives) | S | Regex excludes partitions/loop/dm devices; `read_count` delta 0 → `await=None`; `busy_time` may be 0 on some kernels (one-time warn); **SATA/virtio/eMMC-only hosts populate `sd*`/`vd*`/`mmcblk*` rows — non-NVMe is a TESTED DEFAULT, not empty metrics (rev. 3)** | Two snapshots busy+1000ms/2000ms→util=50; read_count=0→await None; **`sda`/`vdb`/`mmcblk0` keys produce rows; loop/dm/partition keys absent**; gauge set |
| CPU package power | RAPL `energy_uj` delta→W (rollover-safe). **GPU-board hwmon path is Tier-4/backend-provided** | Crash history cites VRM/PSU transients; the CPU package envelope is a board-level stress signal visible before the next swap | **Probe in order (rev. 3):** (1) Intel `/sys/class/powercap/intel-rapl:0/energy_uj` + `max_energy_range_uj`; (2) AMD `amd_energy` hwmon `power1_input` / AMD powercap domain; (3) `None` (one-time INFO). Or `observability.rapl_domain_path` override. Rollover math identical across sources | Fast **2s**, `read_package_power()` | `collectors.py`, `panels_system.py`, `app.py`, `models.py`, `metrics.py`, `server.py` | TUI `CPU Pkg W` + sparkline; Prometheus `bastion_cpu_package_watts` (no labels); JSON | S | **Rollover**: if `new_uj<last_uj` add `max_energy_range_uj`; first read→None; **absent on containers/ARM/no-powercap → None (tested default); AMD Ryzen/EPYC hosts use the amd_energy path, NOT silently None (rev. 3)** | Intel RAPL +4000000µJ/2s→2.0W; **amd_energy path probed when intel-rapl absent**; rollover→positive; all-absent→None |
| OOM-kill detection | `/proc/vmstat oom_kill` cumulative counter, delta = new kills | OOM is the extreme end of memory pressure and currently invisible; an OOM that kills Ollama leaves BASTION's in-process queue/leases as zombies. `oom_kill` delta coinciding with a watchdog failure pins the root cause | `_read_vmstat()` (shared read, zero extra IO) | Fast **2s**, `get_oom_data()` | `collectors.py`, `panels_system.py`, `app.py`, `models.py`, `metrics.py`, `server.py` | TUI `OOM kills` row (red if rate>0) + push CRITICAL to existing `alert_history`; Prometheus `bastion_oom_kill_total` **Counter** `.inc(delta)`; JSON | S | `oom_kill` is 4.13+; alert only on positive delta (restart sees prior-boot total as non-new); skip `/dev/kmsg` name extraction (CAP_SYSLOG) | oom 5→7→rate>0 + alert; equal→0 no alert; first→None; missing key→None; counter increments by delta not total |

`ContentionSnapshot` (4.4) is the response of `GET /broker/contention` (dual-registered, 4.10); `BrokerStatus` gains `contention: ContentionSnapshot | None = None` (backward-compatible). Total new Prometheus series worst case ≈ 15–21 (6 PSI + 2 swap + 2×N block devices where N≤8 + 1 CPU power + 1 OOM) — within discipline (GPU-board-power series stays deferred). **TUI disk-mount labels** are no longer hard-coded to `/` and `/mnt/nvme_data` (`collectors.py:150`): `observability.disk_mount_labels` overrides, else mounts are discovered via `psutil.disk_partitions(all=False)` filtered to physical devices (rev. 3) — the new `ContentionPanel` makes the old developer-specific default visible to downstream users, so the hard-code is removed.

### 5.3 Process Attribution Cluster (Tier 1–2; TUI + JSON only)

Promotes per-process GPU/CPU/mem/IO data from the modal kill-dialog into an always-on `ProcessAttributionPanel`, tags inference-owned PIDs, adds a user watchlist and a churn detector. **Zero new Prometheus metrics** — process identity is human-operator classification, never a time-series label.

**Async prerequisite (CRITICAL, applies to the whole cluster).** `query_processes()` (`nvidia.py:71`, declared sync on the Protocol at `base.py:31`) is currently **synchronous** `subprocess.run` with a 5 s timeout. The planned `nvidia-smi pmon` call (new protocol method `query_process_utilization`) has the same shape. **Both MUST be converted to async (`asyncio.create_subprocess_exec`, matching `query_status` at `nvidia.py:22`) — on the Protocol (`base.py`), in `NvidiaBackend`, AND in `StubBackend`, in one commit — before they are called from `_machine_snapshot_loop`**, otherwise the synchronous subprocess blocks the entire asyncio event loop for up to 5 s on every 10 s slow tick. This conversion is an explicit Phase-3 prerequisite (Section 8) and applies to **both** the compute-apps query and the new pmon query, on **both** the `_machine_snapshot_loop` path and the admin endpoint. All GPU-process data is empty on `StubBackend`, so on non-NVIDIA / no-GPU hosts the panel shows CPU/IO/watchlist/churn only — no error.

| Signal | What | Why | Source | Collection + Cadence | Files | Surface | Effort | Risks | Tests |
|---|---|---|---|---|---|---|---|---|---|
| Per-PID GPU SM util | `pmon` `sm%/mem%/enc%/dec%` per PID joined to compute-apps VRAM | VRAM-per-PID says who *holds* memory; SM% says who is *burning compute now*. A competitor at 80% SM% during a stall is the smoking gun | **new async protocol method** `query_process_utilization()` → `NvidiaBackend` runs `nvidia-smi pmon -s u -c 1` (driver 358+); `StubBackend`→`[]` | Slow **10s** | `nvidia.py`, `gpu/base.py`, `gpu/stub.py`, `models.py`, `collectors.py` | TUI ProcessAttributionPanel; JSON `/broker/processes` | M | `pmon` unsupported on old drivers; check returncode AND header line; variable columns on headless; **non-NVIDIA → `[]`** | Fixtures: normal, missing enc/dec, empty, non-zero rc, timeout → all return `list[ProcessGPURow]` with None for missing; **StubBackend→`[]`** |
| Per-PID VRAM (promoted) | Move modal-only compute-apps data into the panel | Already collected on modal-open (blocks UI thread); moving to **async** background makes it always fresh and removes the UI-thread subprocess | **async** `query_processes()` (promote sync `nvidia.py:71` → async on the Protocol) | Slow **10s**, co-collected with pmon | `nvidia.py`, `gpu/base.py`, `gpu/stub.py`, `collectors.py`, `modals.py` | TUI panel (replaces modal-only) | S | `GPUProcessListModal.compose()` must read `app._last_process_snapshot.gpu_processes`, not re-run subprocess — verify kill-flow still works | Modal compose no subprocess; panel shows VRAM when snapshot non-empty; graceful empty |
| Own-PID registry | Tag BASTION (`os.getpid()`) + Ollama (name+port) as `is_inference_owned` | Lets operator instantly distinguish inference (cyan) from competitors (red/yellow) | `os.getpid()`; `psutil.process_iter` name-match + optional port cross-check (read port from `BrokerConfig`, not hardcoded) | **30s** refresh | `collectors.py`, `models.py` | TUI color coding; JSON `role` field | S | Ollama detection fails on non-Linux/unreadable `/proc/net/tcp`; `net_connections()` may raise AccessDenied → fall back to name-only + warn | Mock iter→PID role='ollama'; getpid→'bastion'; AccessDenied→name-only no crash |
| Top-N CPU/mem/IO (promoted+extended) | Wire the existing-but-unpassed `processes` param + add `io_counters()` read/write bytes/s + RSS, composite sort (CPU primary, VRAM secondary) | `CPUPanel.render_data()` already accepts `processes` but `app.py` never passes it — mostly wiring. IO bytes/s turns a CPU list into IO attribution (an NVMe burst from a 5% CPU process is the real PCIe stall cause) | `process_iter(['pid','name','cpu_percent','memory_info','io_counters'])` + `_last_proc_io` delta | Fast **2s** | `collectors.py`, `models.py`, `app.py` | TUI panel; JSON `/broker/processes` | M | `io_counters()` raises AccessDenied for many procs even as broker user → catch per-process, io fields None, do not drop; join two structs by PID gracefully | AccessDenied on some→io None but kept; composite sort puts high-VRAM low-CPU above low-VRAM high-CPU |
| User watchlist | `observability.process_watchlist: ['python3','pid:12345']` always shown regardless of rank | Operators know which process worries them (training loop, Jupyter); pin it always-visible | `process_iter` filtered by name/PID, partitioned from top-N in one pass | Fast **2s** | `models.py`, `broker.yaml`, `collectors.py`, `app.py` | TUI watchlist section; JSON | S | PID watchlist fragile across restart (name better but matches all); empty watchlist (common) = single `len()` early-exit, zero overhead | watchlist=['python3'] → appears regardless of rank, `watchlisted=True`; empty → no entries |
| Process churn detector | Symmetric PID-set diff per slow tick; `ProcessChurnEvent` if >threshold new PIDs; bounded deque(10) | A 32-worker conversion-script spawn right before a stall is a strong correlation signal; transient workers that already exited are otherwise invisible | `psutil.pids()` (cheap int list) for the set; `Process(pid)` only for new PIDs | Slow **10s** | `collectors.py`, `models.py`, `app.py` | TUI churn section; JSON | S | Busy machines (cron) cause false positives → tunable `churn_threshold` (default 5 conservative); deque(10) drops oldest by design | N+6 new→event; N+2→none; deque maxlen=10 oldest dropped |
| Processes admin endpoint | `GET /broker/processes` → `ProcessSnapshot` JSON | Headless/MCP operators need the same attribution as the TUI; matches `/broker/queue`/`/broker/watchdog` snapshot pattern | Module-level `_process_snapshot` updated by the **broker-side** slow tick (works without TUI) | **10s** background (broker process) | `server.py`, `client.py`, `models.py` | JSON/MCP; consumed by TUI via `BastionClient.get_processes()` | M | Background collector uses **async** `create_subprocess_exec` (per the cluster-wide prerequisite above); **dual-registered in both factories (4.10)** | 200 + correct shape when populated; empty lists (not 404) before first run; auth applied; route present in both apps |
| ProcessAttributionPanel | New `BastionPanel` subclass in **`panels_processes.py` (new file)**, 4 sections (GPU / CPU-IO / watchlist / churn), role-badge color, stale-GPU dim annotation | Primary user-facing surface for the cluster; shows individual contenders vs the existing aggregate panels | `BastionDashboard.refresh_data()` via `get_processes()` | **2s** render (GPU sub-data 10s) | `panels_processes.py` (new), `app.py`, `modals.py` | TUI only | M | Tall panel → cap rows/section (6 GPU/8 CPU/5 watchlist/3 churn) + `... N more`; matches LeasePanel/A2ATaskPanel cap; **GPU section empty on no-GPU/non-NVIDIA hosts (StubBackend) — shows `(no GPU)` rather than a broken section** | Full snapshot→correct rows; stale GPU(20s)→dim; empty→`(no data)`; own-pid badge; **StubBackend→GPU section `(no GPU)`** |

**Panel file assignment (decided now, not deferred to an open question — per completeness + YAGNI review):** `panels_secondary.py` is at 199 lines / 4 classes; adding two more multi-section panels there would breach the file split pattern and approach the file-size cap. Therefore:
- `ProcessAttributionPanel` → **new file `panels_processes.py`** (matches the one-domain-per-file split).
- `CorrelationPanel` → **new file `panels_correlation.py`** (Section 6; same rationale).
- `ContentionPanel` → **`panels_system.py`** (it is a host/system-level panel, peer to MemoryPanel/NetworkPanel).

**Reconciliation:** This cluster establishes the slow-tick/fast-tick template that 4.9 formalizes. The broker-side process collector is folded **into** the single `_machine_snapshot_loop` (4.9), not a separate redundant task — the surfaces-spine "shared `_slow_collection_task`" concern is resolved by making `_machine_snapshot_loop` that shared task. `ProcessAttributionPanel` is added to the `secondary_ids` set in `app._apply_layout()` (currently `{"a2a-tasks","leases","audit-stream"}` at app.py:239) so it is visible in `[3]` full layout, toggle `[t]`. See Section 7.1 for the layout-grid impact of growing the secondary set.

### 5.4 Inference-Native Cluster (Tier 0–2)

Six signals unique to BASTION's chokepoint. Four token-derived signals converge on **one** tap point in the proxy streaming generator. The tap is isolated into a new `inference_tap.py` so the already-complex `generate()` doesn't grow interleaved measurement logic. **These signals are model-agnostic** (Constraint #3): the tap reads whatever `model`, `eval_count`, `prompt_eval_count`, and durations Ollama returns for *any* model — no model name or size is assumed.

**Which `generate()` gets the tap (explicit — corrects rev. 1's single line cite).** `proxy.py` has **two** `generate()` closures:
- `_stream_passthrough.generate()` at **proxy.py:523** — the raw pull/push passthrough for non-inference / opaque streaming. **No tap.** It does not carry `done:true` Ollama token fields in a form the tap needs, and instrumenting it risks the buffering regression the streaming-integrity constraint forbids.
- `_stream_response.generate()` at **proxy.py:591** — the **inference** streaming path. **This is the only path that gets the tap.** `dispatch_start` is already in closure scope (proxy.py:355), and the `done:true` chunk with `prompt_eval_count`/`eval_count`/durations flows through here.

A developer reading only this spec instruments `_stream_response.generate()` (line 591) and **not** `_stream_passthrough.generate()` (line 523). Signals not measurable on the passthrough path (it carries no token accounting) are simply absent there by design — the passthrough is for non-inference traffic.

| Signal | What | Why | Source | Collection + Cadence | Files | Surface | Effort | Risks | Tests |
|---|---|---|---|---|---|---|---|---|---|
| Tokens/sec (prefill+decode) | `decode_tps = eval_count / (eval_duration / 1e9)`; `prefill_tps = prompt_eval_count / (prompt_eval_duration / 1e9)` (Ollama durations are **nanoseconds** → divide by 1e9); non-streaming reads `resp_json` directly | Single most actionable LLM number; prefill (prompt/KV pressure) vs decode (mem bandwidth) diagnose different bottlenecks; decode collapse under VRAM pressure is a leading OOM-swap indicator | Ollama `done:true` fields. **`_extract_streaming_tokens` (proxy.py:744) currently captures ONLY `prompt_eval_count`+`eval_count` — it MUST be extended to also capture `eval_duration` and `prompt_eval_duration`** (verified: those two fields are parsed nowhere in `src/`). The helper moves into `InferenceTapCollector.on_chunk` | Per-request stream tap, O(1)/chunk | `proxy.py`, `inference_tap.py` (new), `models.py`, `metrics.py`, `server.py` | TUI TracePanel `tok/s` col; Prometheus `bastion_llm_decode_tps`, `bastion_llm_prefill_tps` Histograms `{model}`; JSON `/broker/recent` | M | Tap must not buffer (parse one small obj/chunk, None for non-final); **cache-hit `eval_duration==0` → rate `None`, not divide-by-zero** (the guard is meaningless unless the duration field is actually read — now it is); model label is whatever Ollama reports (any model) | `_extract_streaming_tokens` with: non-final chunk→None; full chunk w/ `eval_duration>0`→decode_tps computed; **`eval_duration==0` (cache-hit)→decode_tps None**; ns→s conversion correct; mock httpx stream → decode_tps in `record_fn` kwargs |
| TTFT in proxy path | `time.time() - dispatch_start` to first non-empty chunk; `observe_llm_ttft(model, ttft)` | Most user-perceptible metric; a stall shows first as a TTFT spike. The helper is fully wired in A2A (`a2a.py:828`) but has **zero proxy call sites** (verified). `dispatch_start` already in closure scope (`proxy.py:355`). Smallest-effort signal | `dispatch_start` (post-grant) + first-chunk flag, inside `_stream_response.generate()` (line 591) | Per streaming request, single observation | `proxy.py`, `metrics.py` | Prometheus `bastion_llm_time_to_first_token_seconds{model}` (defined, `metrics.py:251`); TUI TTFT sparkline; Grafana alert p95>10s | S | Closure-flag is asyncio-safe; semantic is "Ollama-receive→first-token" (post-grant), document to prevent misread | Called exactly once/streaming req (mock 2-chunk stream); NOT on non-streaming; value ≈ correct with sleep |
| Context-window utilization | `prompt_eval_count / injected_num_ctx`; capture `injected_num_ctx` from `options.get('num_ctx')` at injection (`proxy.py:246`) | Only inference-time signal that predicts VRAM pressure *before* a swap/crash; >0.85 = pre-OOM (KV-cache expands under high fill) | `done:true prompt_eval_count` + closure-captured `injected_num_ctx` | Per-request | `proxy.py`, `inference_tap.py`, `models.py`, `metrics.py`, `server.py` | TUI ctx_util col (yellow>0.85, red>0.95); Prometheus `bastion_llm_ctx_utilization_ratio{model}`; JSON `/broker/recent` | M | `num_ctx` None if neither client nor config set it → skip metric, never emit misleading 0.0. **Denominator precedence chain (rev. 3):** (1) request `options.num_ctx`, (2) `injected_num_ctx` from `proxy.py:246`, (3) `request_overrides.default_num_ctx` (models.py:187, default 4096) as a fallback so the metric fires even for users without per-model `broker.yaml` entries, (4) None→skip. **Documented coverage gap:** requests relying on Ollama's own internal default with no override at any tier have no metric (we do not infer Ollama's default) | Ratio from known values; **default_num_ctx fallback used when no per-model entry**; None at all tiers→no emission; ratio>1.0 (malformed)→dropped |
| **Tier 0:** activate 2 dead metrics + correctly place swap-duration | (1) `record_cooldown_wait()` at the cooldown sleep site (scheduler.py:507); (2) `record_model_swap_duration()` wrapping **the `_dispatch_for_model` swap I/O** (see placement note below) | Spec doc calls these dead (verified: `record_cooldown_wait` and `record_model_swap_duration` defined, **zero call sites**). Cooldown counter makes the swap-rate limiter visible to alerts; swap-duration distinguishes 2-4s vs pathological >30s (thermal stress) | Existing code locations; helpers already importable | Event-driven (sleep / swap completion) | `scheduler.py`, `metrics.py` | Prometheus `/metrics`; Grafana cooldown/swap-duration panels | S | **Placement (corrected per review):** do NOT start `swap_start` *before* the `_load_semaphore` block — that would fold in semaphore-contention wait from other concurrent swap attempts (which `cooldown_waits_total` already covers) and produce a meaningless duration. Capture `swap_start = time.monotonic()` **before the `if vram_manager…/else` split (before scheduler.py:631)** so it is set on both branches, and call `record_model_swap_duration(candidate.model, time.monotonic()-swap_start)` **after `_dispatch_for_model` returns in BOTH branches** — the `async with _load_semaphore` branch (~632) AND the `else` no-semaphore branch (scheduler.py:644). Instrumenting only the semaphore branch silently under-counts when no `VRAMManager` is configured | Spy: cooldown called once per cooldown-sleep; **swap-duration called with model after each swap, including the no-semaphore (`vram_manager is None`) path**; both branches covered |
| **`update_vram_usage` REMOVED from Tier 0 (corrected per review)** | — | The Vision-C frozen `bastion_vram_used_mb` (labeled `gpu_index`) is **already emitted at vram.py:345** via `update_vram_used_mb`. `update_vram_usage` is the *bytes* gauge (no labels). Wiring it at the same site would publish a **second** Prometheus object reporting the same quantity in different units, with no consumer needing the bytes form | — | — | — | — | — | The bytes gauge `bastion_vram_used_bytes` is **not** activated here. If it is ever genuinely wanted, it is documented separately as an **additive second metric** (not a replacement) and must NOT be injected at vram.py:345 where `update_vram_used_mb` already lives | Regression: vram.py:345 still calls `update_vram_used_mb` only; no double-emit introduced |
| VRAM ledger drift + **NEW** reconcile counters | `bastion_vram_ledger_drift_mb{gpu_index}` (signed Gauge, `labelnames=['gpu_index']`) = measured − (allocated+reserved); **`bastion_vram_reconcile_stale_total`, `bastion_vram_reconcile_import_total` Counters (no labels) — NET-NEW objects, NOT activations** | Drift is BASTION-believed vs backend-reported VRAM; growing positive = ledger under-counts (unsafe). Reconcile counters meter how often Ollama auto-unloads / clients bypass the broker | drift via `_machine_snapshot_loop` (measured side from `GPUBackend.query_status`); counters incremented in `reconcile()` at the existing **`audit.emit('vram_reconciliation', …)` (vram.py:867)** and **`audit.emit('vram_import', …)` (vram.py:874)** sites | Drift **10s** (needs backend VRAM); counters event-driven | `metrics.py` (**define 2 new Counters + 2 helpers FIRST**), `vram.py`, `server.py` | Prometheus (3 metrics); TUI VRAMLedgerPanel (gauge call, no display change); Grafana alert (rev. 3 PromQL below) | M | **The reconcile counters do NOT exist in `metrics.py` (verified by grep) — vram.py:867/874 only emit audit events. Calling non-existent helpers would AttributeError. `VRAM_RECONCILE_STALE_TOTAL` / `VRAM_RECONCILE_IMPORT_TOTAL` + helpers MUST be defined in `metrics.py` before being wired in `vram.py`.** Drift needs backend VRAM → slow tick only; **backend returns None (StubBackend/non-NVIDIA) → skip drift (don't emit 0)**; both counters unlabeled (model names = unbounded) | reconcile increments stale on removal / import on new model; drift not emitted when backend VRAM None; reconcile(None) increments nothing; **counter objects exist in metrics.py before wiring** |

**VRAM-drift Grafana alert (rev. 3 — multi-GPU-safe PromQL).** The alert must not hard-code GPU 0. Replace the literal `{gpu_index="0"}` selector with an index-agnostic aggregation that fires on **any** GPU exceeding the threshold:

```promql
max by (gpu_index) (abs(bastion_vram_ledger_drift_mb)) > 2048
```

The existing `update_vram_used_mb(gpu_index, mb)` helper already supports per-index emission, so this pattern needs no metric change — only the alert expression. On single-GPU hosts this matches exactly the old behavior (`gpu_index="0"` is the only series); on multi-GPU hosts it generalizes without knowing indices in advance.

**Stream-tap architecture (reconciliation with surfaces-spine).** Both this cluster and surfaces-spine target the same `done:true` tap. Resolution: the tap lives in a single `InferenceTapCollector` dataclass (`inference_tap.py`, imports only `metrics` + stdlib — no circular import) holding `first_chunk_time`, `done_fields` (now including `eval_duration`/`prompt_eval_duration`), `injected_num_ctx`. `_stream_response.generate()` (proxy.py:591) captures one instance per request, calls `collector.on_chunk(chunk, now)` per chunk, and `collector.flush(model, dispatch_start, record_fn)` in `finally`. `flush()` calls `record_fn(...)` with the six new kwargs from Section 4.6 (`prefill_tps`, `decode_tps`, `ttft_s`, `ctx_utilization`, `eval_count`, `prompt_eval_count`); `record_fn` is `record_recent_request` (server.py:197) with its new `None`-default signature. `_extract_streaming_tokens` moves into the collector and is extended to capture the two duration fields with the ns→s conversion and the `eval_duration==0` cache-hit guard. Non-streaming calls `collector.on_complete_response(resp_json)`. This is the inline tap (not a separate middleware) because the streaming path's edge cases (NDJSON passthrough, use_mmap injection, circuit breaker) make a detached middleware risk missing cases.

### 5.5 Correlation Engine Cluster (Tier 2) — see Section 6 for the in-depth design.

### 5.6 Tier 3 — Catalogued Surfaces & Tooling (Phase 4, gated)

| Item | What | Blocked on | Files | Notes |
|---|---|---|---|---|
| `GET /broker/snapshot` (+ `?history=N`, `?include_ring=true`) | Returns latest `MachineSnapshot`; `?history` returns last N from the deque (cap N at 60 server-side); `?include_ring=true` expands the full 512-entry correlation ring (debug-only; default returns just the `recent_ring_events` tail) | Nothing — ships with the snapshot loop | `server.py` | **MUST be registered in BOTH `create_app` and `create_admin_app` (4.10)** — the rev. 1 "registered once on a shared router" claim was false. Extract `_handle_snapshot` and add it to both factories. Separate from `/broker/status` (stable narrow view). Auth via existing `verify_admin` |
| MCP `broker_snapshot_v1` | One-call correlated snapshot for AI agents (vs 5+ separate calls) | **`mcp_adapter` package (v0.5, ADR-007)** | `mcp_adapter/tools/broker_snapshot_v1.py`, `schemas/broker_snapshot_v1.json` | `_v1` suffix, committed JSON Schema, adapter validates broker response. Input `history_count` 1–60. Ship the endpoint first; tool is a wrapper. **Shipping this tool is the third operational surface → triggers ADR-005 gating event #1; deferral recorded in Section 9** |
| SSE `/broker/snapshot/stream` | Push each snapshot to web/monitoring/MCP clients; 501 if disabled | Config flag; **dedupe `_sse_wrapper`** (pre-existing debt, duplicated server.py:1359/2175) | `server.py` | Supersedes the 2026-03-13 `/broker/status/stream`. FastAPI `StreamingResponse` (external surface), **not** an in-process bus → does **not** require ADR-005-B. Cap 8 clients → 503. TUI keeps polling. Dual-registration (4.10) applies if exposed under `/broker` |
| Prometheus cardinality CI lint | `scripts/check_metric_cardinality.py` parses `metrics.py` AST, checks every `labelnames` list against the **permitted-set** ({model, resource, device, op, reason, kind, factor, xid_code, gpu_index}) and fails on any label outside it (so a planted `labelnames=['pid']` fails; `['gpu_index']` and `['device','op']` pass) | Nothing | `metrics.py`, `scripts/check_metric_cardinality.py` | Enforces Section 3 rule #2. Requires literal `labelnames` lists (existing convention). Permitted-set check, not just a blacklist. **Validates label NAMES, never label VALUES — so `device="sda"`/`device="vdb"` are as valid as `device="nvme0n1"` (rev. 3); the lint needs no NVMe-specific value regex** |
| Grafana panel catalogue | 1:1 metric↔panel mapping doc + panels in `dashboards/grafana/bastion-overview.json` | **Vision C base dashboard / `dashboards/grafana/` dir (pending)** | `docs/design/specs/2026-06-19-observability-expansion-panel-catalogue.md`, `dashboards/grafana/bastion-overview.json` | ADR-010 CI validates all JSON. If expansion ships first, catalogue is authoritative for what Vision C must include. PSI alert thresholds (io_full≥5/≥25) that are Prometheus-alert (not TUI) live here; the TUI display thresholds now live in `ObservabilityConfig` (5.2) |
| BastionClient extension | `get_snapshot(history=1)` + `get_processes()` + `get_contention()` added to the existing `asyncio.gather()` fan-out | Nothing | `client.py`, `app.py` | Follows `_get_safe` pattern (returns `{}` on failure). Phase-2 **decision gate** (not open question): snapshot eventually *replaces* the 7-endpoint gather — decide the migration path before Phase 1 ships so Grafana/MCP don't harden against the wrong target (Section 10.2) |

---

## 6. The Correlation Engine (The Moat)

A new `src/bastion/correlation.py` module: an in-memory, bounded, **purely passive** engine that reads from existing subsystems and derives net-new intelligence by joining their events on one monotonic clock. Integration is strictly unidirectional — existing subsystems never import `correlation.py`; it reads from them. It is instantiated once in `lifespan()` and its `tick()` is called at the end of each `_machine_snapshot_loop` iteration (4.9), so it adds **zero** new background tasks and **zero** new I/O — it consumes the snapshot the loop already built. **It never issues GPU subprocesses itself** — every GPU input arrives pre-collected via the `GPUBackend` seam in the `MachineSnapshot`, so the engine has no NVIDIA assumptions and degrades automatically on non-NVIDIA hosts (GPU-derived components simply read `None`).

**Dependency correction (per completeness review).** The engine depends ONLY on `_scheduler`, `_vram_tracker`, and `_vram_manager` — all **unconditionally** initialized in `lifespan()` (server.py:635/639/672). It does **NOT** depend on `_a2a_handler`, which is initialized only `if config.a2a.enabled:` (server.py:693). **A2A-disabled deployments get full correlation-engine functionality** because inference events flow through the proxy `done_fn` path (emitter C below), which is not gated on A2A. Rev. 1's lifespan dependency list incorrectly included `_a2a_handler`; it is removed. The engine is instantiated after the three required singletons exist and before `_machine_snapshot_loop` starts (both after server.py:751).

### 6.1 `CorrelationRing` — unified monotonic event timeline

A bounded `deque[CorrelationEvent]` (`maxlen=512`, ~200 KB ceiling). Each event: `ts_monotonic` (`time.monotonic()`), `ts_wall`, `domain` ∈ {gpu, system, inference, scheduler}, `kind`, `payload: dict`. Four thin unidirectional emitter paths:

- **(A) Scheduler/audit — via a PUBLIC cursor API (corrects rev. 1's private-deque reach-in).** Rev. 1 had the engine hold a reference to the module-level **private** `audit._recent_events` deque (audit.py:147) and track an integer index into it. That is wrong twice: (1) it couples to a private structure that breaks silently if audit's ring is resized/replaced; (2) integer indices into a `deque(maxlen=…)` are **not stable** across appends once the ring wraps (the left end is discarded), so a stored index drifts. **Fix:** add a public accessor to `audit.py` — `audit.get_events_since(cursor: int) -> tuple[list[dict], int]` that returns events appended since the given monotonic sequence number plus the new cursor, atomically. `audit.py` maintains a monotonically-increasing `_event_seq` counter incremented on every append (alongside the existing `_recent_events.append` at audit.py:257/306). The correlation engine stores `last_ingested_seq: int` and calls `get_events_since(last_ingested_seq)` each tick. The cursor is a **monotonic sequence number, not a deque index**, so it is stable across wraps and survives a ring resize. `audit._recent_events` is never referenced across module boundaries.
- **(B) System+GPU:** `tick(snapshot)` reads the `MachineSnapshot` already assembled — GPU throttle/clock state, PSI, block-device IO, swap — and emits domain events on threshold crossings. GPU fields are `None` on non-NVIDIA hosts; the engine guards each before emitting (no GPU event when the value is `None`).
- **(C) Inference — engine reads, record-site does not import the engine (per YAGNI review).** Rather than adding a new one-line `correlation.ingest_inference_event(...)` import path from the record site into `correlation.py`, the engine reads the per-request data the same way it reads audit (cursor pattern): on each `tick()` it consumes new entries from the `_recent_requests` deque since its own cursor, using the token/TTFT/queue-wait fields that Section 4.6 already adds to each record. This keeps `server.py`'s done-path free of a correlation import and is symmetric with emitter (A). (If a future need for synchronous per-event push appears, the explicit `ingest_inference_event` call can be added then — not now.)
- **(D) GPU throttle:** consumed from `GPUExtendedStatus.throttle_reasons` (already collected by the slow tick via the backend, 5.1), not a new subprocess. Empty list on non-NVIDIA → no throttle events.

Surfaces: the last-N ring tail rides in `CorrelationState.recent_ring_events` on `/broker/snapshot`; the full ring is reachable only via `GET /broker/snapshot?include_ring=true` (debug). **There is no standalone `/broker/correlation/ring` endpoint** (review: YAGNI — operators/Grafana use the snapshot, MCP uses `broker_snapshot_v1`). TUI `CorrelationPanel` (scrollable timeline in **`panels_correlation.py`**, new file). Prometheus aggregate counts by `domain+kind` only (no per-event metrics — cardinality).

### 6.2 Stall-reason enrichment

Pure function `enrich_stall_reason(base_reason: str, snapshot: SystemSnapshot) -> str` that appends a bracketed live-context suffix to the scheduler's existing `_last_stall_reason` (already a public property `Scheduler.stall_reason` and `BrokerStatus.stall_reason`). Example: `'swap_cooldown [NVMe write 94% util, mem-PSI some=18.3]'`. Called in the `/broker/status` handler just before building the response, so enrichment reflects snapshot age at response time (≤2s stale). **Additive only** — never replaces the base reason (existing tests asserting on `stall_reason` values keep passing); `None` snapshot returns the base unchanged; output capped ≤150 chars (TUI truncation guard). The suffix omits any signal that is `None` (e.g. no NVMe clause on a host with no matching block device, no GPU clause on non-NVIDIA), so the enrichment is correct on partial snapshots. Surfaces in `BrokerStatus.stall_reason` (richer string renders automatically in the existing SchedulerPanel — no panel change). **There is no standalone `/broker/correlation/stall` endpoint** (review: YAGNI — `stall_reason` is already in `/broker/status` and `/broker/snapshot`); rev. 1's separate endpoint is dropped.

### 6.3 `ContentionEventDetector` — discrete non-inference contention

Stateful detector comparing consecutive `SystemSnapshot`s; emits a discrete `ContentionEvent` with a human-readable attribution **only when** a threshold crossing **coincides** with an active inference stall (or a queue-wait p50 ≥2× the rolling EWMA baseline). This simultaneous-confirmation join — "IO at 94% **AND** inference stalled at the same instant" — is the moat; `htop` shows the IO alone, only BASTION shows the coincidence.

**Threshold-unit disambiguation (per all three lenses).** Rev. 1 mixed "util %" and "MB/s" in one sentence. The detector uses **two clearly-separated** named thresholds, each on its own unit, configurable in `CorrelationConfig` (4.8):
- **Block-device write throughput:** `contention_block_write_mb_s_threshold` (default **200.0 MB/s**, computed from psutil write-bytes delta ÷ elapsed). This is the throughput leg. **Device-dependent (rev. 3):** the default targets mid-range consumer NVMe; enterprise NVMe users raise it (2000+), SATA/eMMC users lower it (~100). The active value is logged at startup and dynamic idle-calibration (Tier 4) is the recommended portable default. The key is **device-generic** — the leg keys off whatever base devices `block_devices` discovered (`nvme*/sd*/vd*/mmcblk*`), not NVMe specifically; the `observability:` block is greenfield (no existing users), so the key carries no NVMe-specific name.
- **PSI:** `contention_psi_threshold` on `mem_psi_some_avg10` (default 20.0) and `contention_cpu_psi_threshold` on `cpu_psi_some_avg10` (default 60.0).

The `BlockDeviceIOStats.util_pct` field (from `busy_time` delta) is the **display** value shown in the TUI/JSON; the **detector's** disk leg keys off the MB/s throughput threshold, not `util_pct`, so configuration is single-unit per knob. Both legs use **edge detection, not level** (emit at crossing, not every tick above threshold) + **2-tick hysteresis** (`contention_hysteresis_ticks`, must exceed in two consecutive ticks) to kill transient spikes (kernel buffer flush). On a host with no PSI and/or no matching block device, the corresponding leg simply never fires (its input is `None`), and the detector degrades to whatever legs *are* available. Surfaces: `GET /broker/correlation/contentions` (last 50 from a dedicated `maxlen=50` deque — **kept**, because discrete contention events are NOT in the snapshot body; dual-registered, 4.10), TUI ContentionPanel/AlertPanel (last 5), Prometheus `bastion_contention_events_total{kind}` (4 bounded: `nvme_burst`/`mem_pressure`/`cpu_contention`/`combined`). Attribution stays **category-level** in the JSON API (process names reserved for the TUI process list) to avoid leaking process info.

### 6.4 `RiskIndex` — composite forward-looking gauge

Pure function `compute_risk_index(...) -> RiskIndexResult` folding five live signals into one `score ∈ [0,1]` + `level` ∈ {nominal, elevated, high, critical} + `component_scores` + `dominant_factor`. Default weights (config-tunable via `risk_weights`): VRAM headroom 25%, thermal headroom 20%, swap-rate level 25%, thrashing worst-verdict 20%, memory-PSI 10%; each component normalized to [0,1] before weighting. All inputs already collected in the 2s tick (`GPUStatus`, `VRAMManager.status()`, `Scheduler._swap_rate_level`, `ThrashingDetector.snapshot()`, PSI). **Each component degrades independently:** any input that is `None` (e.g. thermal headroom on a no-GPU host, PSI on an old kernel) contributes 0 to its own term and the weights of the available components still produce a meaningful score — it never crashes or reads a misleading 0 for a present-but-unmeasured signal (the term is *absent*, not *zero-risk*). It replaces the operator's mental synthesis of five separate gauges with one consistent number and one Prometheus alert target.

**Thrashing-component portability note (rev. 3).** The swap-rate / thrashing terms reflect the thresholds in `SchedulerConfig` (`swap_rate_warn_threshold=4`, `swap_rate_critical_threshold=6`) and `ThrashingDetectionConfig` (`halt_swap_ratio=0.75`), whose **defaults are derived from consumer-GPU (RTX 5090) crash forensics** (models.py docstring at line 211). Server-GPU operators (A100/H100) with very different swap-stress profiles should tune these down or up. The `ThrashingDetectionConfig` docstring at `models.py:211` MUST be updated from `"Thresholds derived from RTX 5090 crash data."` to `"Conservative defaults based on consumer-GPU crash forensics; server-GPU operators (A100/H100) should tune halt_swap_ratio and swap_rate_critical_threshold down to reflect their swap-stress profiles."` — this is a **named Phase-1 task** (Section 8, `models.py` docstring portability fix), not just a note, so it is actioned rather than silently skipped by implementors following the phase plan. The RiskIndex itself does not hard-code these — it reads the configured values.

Surfaces: `GET /broker/correlation/risk` (**kept** — the composite warrants its own alerting endpoint; dual-registered, 4.10) + `BrokerStatus.risk_index`, TUI SchedulerPanel risk row (score bar + dominant factor), Prometheus `bastion_risk_index` (Gauge, no labels) + `bastion_risk_dominant_factor_total{factor}` (5 bounded component names, +1/tick for the dominant). Communicated to users as "risk approaching, not a crash" (forward-looking by design). Property test: `score ∈ [0,1]` for any valid input; all-None → 0.0/nominal; `dominant_factor` always one of the 5 names.

### 6.5 CPU↔GPU thermal coupling

A named `ThermalCoupling` field making explicit what the TUI auto-fan logic already knows implicitly: CPU heat drives the GPU fan, so CPU heat indirectly constrains GPU throughput. Fields: `cpu_temp_c`, `gpu_temp_c`, `fan_speed_pct` (from the same extended backend `fan.speed`, 5.1), `coupling_active`, `thermal_headroom_min_c`. **All inputs are `None`-tolerant (rev. 3):** `gpu_temp_c`/`fan_speed_pct` are `None` on non-NVIDIA / no-GPU / fanless-server-GPU hosts; `cpu_temp_c` is `None` when no CPU sensor is discovered. The field is computed defensively so a missing input yields a partial `ThermalCoupling` (the present terms only), never an exception and never a misleading 0.

**`coupling_active` derives from the actual fan curve, not a duplicated constant (corrects rev. 1 + both lenses).** Rev. 1 proposed extracting the `60.0` engagement threshold to a shared constant imported by both `correlation.py` and `app.py`. Two problems: (1) `_fan_band()` (app.py:69–79) is a **multi-step escalation curve** (>85→100, ≥80→90, ≥70→50, ≥60→30, else None), so a single extracted constant silently desyncs if the curve's minimum changes; (2) having `app.py` import from `correlation.py` points the TUI layer *into* the backend engine, violating ADR-005 (TUI is a client, not a peer of broker internals) and risking a circular import. **Fix:** `coupling_active` is computed as **`cpu_temp_c is not None and _fan_band(cpu_temp_c) is not None`** — reusing the definitive curve function as the single source of truth (and `False` when CPU temp is unavailable), so any future curve change is automatically honored. To avoid the wrong import direction, `_fan_band` (and its `_AUTO_FAN_HYSTERESIS_C`) is moved to a thin shared module — **a new `constants.py`** (or `models.py`, the existing shared boundary) — and **both** `app.py` and `correlation.py` import it from there. `correlation.py` never imports from `app.py`, and `app.py` never imports from `correlation.py`.

**`thermal_headroom_min_c` formula corrected + GPU ceiling defined (per completeness review + rev. 3 portability).** Rev. 1's `min(gpu_ceiling − gpu_temp, 60 − cpu_temp)` reads **zero headroom the instant the fan first engages** (cpu_temp == 60 → second term 0), which is misleading. The CPU term uses a **configurable safety ceiling**, not the fan-engagement threshold. The formula is:

```
thermal_headroom_min_c = min(
    gpu_ceiling      - gpu_temp_c,     # GPU term — included only if both are non-None
    cpu_safe_ceiling - cpu_temp_c,     # CPU term — included only if both are non-None
)   # over the terms that are available; None only if neither term is computable
```

where:
- **`cpu_safe_ceiling` = `CorrelationConfig.cpu_safe_ceiling_c`** (default **85.0 °C**, 4.8; documented as a fallback that operators tune to their CPU's Tjmax).
- **`gpu_ceiling` = `CorrelationConfig.gpu_safe_ceiling_c` if set, else `GPUConfig.max_temperature_c`** (rev. 3 — this is the previously-**undefined** `gpu_ceiling` term; binding it to the existing `gpu.max_temperature_c` reuses the value that `resolve_gpu_defaults` auto-detects from the device's own `tlimit`/`shutdown`, 4.8, so it is device-correct on a 93 °C-shutdown server GPU and does not introduce a new hard-coded constant). On a no-GPU deployment where `max_temperature_c` is unset/0 and `gpu_temp_c` is `None`, the GPU term is **skipped** and the headroom is the CPU-only value (not 0).

The Grafana alert therefore fires on `bastion_thermal_headroom_celsius <= 5` (genuine low-headroom), not `<= 0`. Surfaces: `thermal_coupling` in `/broker/correlation/risk` + `BrokerStatus`, TUI GPUPanel/TemperaturePanel row, Prometheus `bastion_thermal_coupling_active` (0/1) + `bastion_thermal_headroom_celsius` (no labels).

### 6.6 Engine lifecycle & integration

No separate task (embedded in `_machine_snapshot_loop`). `/proc/pressure/*` handles are opened-read-closed per tick, never held. PSI absence → one-time INFO at startup (mirroring the `prometheus_client`-absence log pattern). RAPL absence and unknown-CPU-sensor → one-time INFO/DEBUG likewise (rev. 3). `CorrelationConfig` (4.8) makes thresholds/weights/`cpu_safe_ceiling_c`/`gpu_safe_ceiling_c` operator-tunable without code changes. **Lifespan dependencies are `_scheduler`, `_vram_tracker`, `_vram_manager` only — NOT `_a2a_handler`** (corrected, see Section 6 preamble); A2A-disabled deployments are fully functional. Build ordering within the cluster is strict: (1) `models.py` extensions, (2) `constants.py` (shared `_fan_band` move) + `audit.py` `get_events_since` public accessor, (3) `gpu/base.py` + `gpu/stub.py` + `gpu/nvidia.py` (the new async protocol methods + the extended `query_status`; fan.speed/throttle must land before the engine reads them, all async, StubBackend updated in the same commit), (4) `correlation.py` core, (5) `server.py` wiring (both factories per 4.10), (6) dashboard panels (`panels_correlation.py`), (7) tests.

---

## 7. Surface Mapping

Honoring cardinality rules: per-PID/per-process/per-event → TUI + JSON/MCP only; bounded labels → Prometheus.

| Signal | TUI | Prometheus (labels) | JSON / MCP | Grafana |
|---|---|---|---|---|
| GPU compute/mem util | GPUPanel | `bastion_gpu_compute_utilization_pct`, `bastion_gpu_memory_utilization_pct` (none) | `/broker/status.gpu` | panel |
| GPU clocks | GPUPanel + sparkline | — | `/broker/status.gpu` | (derive) |
| GPU fan (read) | GPUPanel color (row omitted when None for N ticks) | (via thermal_coupling) | `/broker/status.gpu` | — |
| GPU mem-junction temp | GPUPanel `mem_temp_color` | `bastion_gpu_memory_junction_temp_celsius` (none) | `/broker/status.gpu` | panel |
| GPU die temp (Tier 0) | TemperaturePanel | `bastion_gpu_temperature_celsius` (none) — **now emits, fast 2s tick** | `/broker/status.gpu` | panel |
| GPU throttle reasons | GPUPanel (red on hw_*) | `bastion_gpu_throttle_events_total{reason}` (4 fixed enum) | `/broker/gpu/extended` | alert |
| GPU PCIe gen/width | GPUPanel `DOWNGRADED` | `bastion_gpu_pcie_gen`, `bastion_gpu_pcie_width` (none) | `/broker/gpu/extended` | alert on gen drop |
| GPU Xid | AlertPanel + GPUPanel | `bastion_gpu_xid_errors_total{xid_code}` (≤16) | `/broker/gpu/extended` | page |
| PSI | ContentionPanel + sparkline | `bastion_psi_{some,full}_avg10{resource}` (3) | `/broker/contention` | panel + alert (io_full≥5/≥25 in catalogue; TUI thresholds in ObservabilityConfig) |
| Swap rate | ContentionPanel | `bastion_swap_{in,out}_mb_s` (none) | `/broker/contention` | panel |
| Block-device util/await | ContentionPanel | `bastion_block_device_util_pct{device}`, `bastion_block_device_await_ms{device,op}` (1–8, `device` = discovered base name) | `/broker/contention` | panel |
| CPU package power | ContentionPanel + sparkline | `bastion_cpu_package_watts` (none) — Intel OR AMD RAPL source | `/broker/contention` | panel |
| GPU board power | — (Tier 4) | — (deferred; backend-provided when a non-NVIDIA backend fills it) | `/broker/contention` (field present, `None` until a backend fills it) | — |
| OOM kills (Tier 0-ish) | ContentionPanel + AlertPanel | `bastion_oom_kill_total` Counter (none) | `/broker/contention` | alert |
| Process attribution (all) | ProcessAttributionPanel (`panels_processes.py`) | **NONE (by rule)** | `/broker/processes` | — |
| Tokens/sec | TracePanel | `bastion_llm_{decode,prefill}_tps{model}` | `/broker/recent` | panel |
| TTFT | sparkline | `bastion_llm_time_to_first_token_seconds{model}` | `/broker/recent` | alert p95>10s |
| Ctx utilization | TracePanel color | `bastion_llm_ctx_utilization_ratio{model}` | `/broker/recent` | panel |
| Cooldown waits (Tier 0) | — | `bastion_cooldown_waits_total` (none) — **now emits** | — | panel |
| Swap duration (Tier 0) | — | `bastion_model_swap_duration_seconds{model}` — **now emits, both swap branches** | — | histogram |
| VRAM used MB (already live) | VRAMLedgerPanel | `bastion_vram_used_mb{gpu_index}` — **already emitted at vram.py:345 (NOT a Tier 0 item; bytes gauge NOT activated)** | `/broker/status` | gauge |
| VRAM ledger drift | VRAMLedgerPanel | `bastion_vram_ledger_drift_mb{gpu_index}` Gauge + **NEW** reconcile Counters (none) | `/broker/gpu/extended` | alert `max by(gpu_index)(abs(...))>2048` (multi-GPU-safe, rev. 3) |
| Correlation ring | CorrelationPanel (`panels_correlation.py`) | aggregate `domain+kind` counts only | `/broker/snapshot.correlation.recent_ring_events` (full ring via `?include_ring=true`) | — |
| Stall enrichment | SchedulerPanel (auto) | — | `BrokerStatus.stall_reason`, `/broker/snapshot` (no separate endpoint) | — |
| Contention events | ContentionPanel/AlertPanel | `bastion_contention_events_total{kind}` (4) | `/broker/correlation/contentions` | panel |
| RiskIndex | SchedulerPanel bar | `bastion_risk_index` + `bastion_risk_dominant_factor_total{factor}` (5) | `BrokerStatus.risk_index`, `/broker/correlation/risk` | alert >0.7/2m |
| Thermal coupling | GPU/TemperaturePanel | `bastion_thermal_coupling_active`, `bastion_thermal_headroom_celsius` (none) | `/broker/correlation/risk`, `BrokerStatus` | panel (alert ≤5) |
| MachineSnapshot | (all panels via dict) | (the union above) | `/broker/snapshot`, MCP `broker_snapshot_v1` | (all) |

### 7.1 TUI layout-grid impact (resolves former open-question 5)

Adding three panels — `ContentionPanel` (system column, peer to Memory/Network), `ProcessAttributionPanel` and `CorrelationPanel` (both in the secondary toggle group) — grows `secondary_ids` from 3 to **5** (`{a2a-tasks, leases, audit-stream, processes, correlation}`). The `[3]` full layout currently renders all secondaries in one column; five secondaries make that column too tall. **Decision:** in `[3]` full layout the secondary group renders in a **two-column sub-grid** (3+2) rather than a single tall column; `ContentionPanel` joins the system column (which already holds CPU/Memory/Network/Temperature) and does not enter the secondary set. `[t]` continues to toggle the whole secondary group. This is a `app._apply_layout()` grid change, specified here rather than deferred.

---

## 8. Phased Build Order

(Mirrored in the `build_order` field.) Effort key: S ≈ 0.5 day, M ≈ 1–2 days, **L ≈ 3–4 days**. **Estimates revised upward (per YAGNI review) to account for the dual-factory route tax (every new endpoint added twice) and the true size of `correlation.py`. Rev. 3 adds the protocol-seam, device-auto-detect, and dynamic-discovery work into the relevant phases (mostly small deltas to items already present).**

**Phase 1 — Tier 0 + cheapest Tier 1 quick wins (foundation). ~7–10 days.** Establish the unified model + the one collection loop, then wake every (genuinely) dead metric and land the zero-/single-subprocess fast-path signals.
- `models.py`: add `MachineSnapshot` + all sub-models + GPUStatus fast-path field extensions (incl. `gpu_index`) + `BlockDeviceIOStats` (renamed) + `ObservabilityConfig`/`CorrelationConfig` models with all rev. 3 keys (M).
- **`models.py` docstring portability fix (rev. 3, see 6.4):** update the `ThrashingDetectionConfig` docstring at `models.py:211` from `"Thresholds derived from RTX 5090 crash data."` to `"Conservative defaults based on consumer-GPU crash forensics; server-GPU operators (A100/H100) should tune halt_swap_ratio and swap_rate_critical_threshold down to reflect their swap-stress profiles."` — docstring-only, no behavior change, but it MUST land in the build order so the developer-box reference is removed before public readers see it (S, trivial).
- `config.py`: add the `observability:` key to `BrokerConfig` parsing (default factories cover absence); **extend `resolve_gpu_defaults` to auto-detect `max_temperature_c` + `gpu_safe_ceiling_c` from `tlimit`/`shutdown` and to WARN when `max_power_watts` stays default on a non-NVIDIA host (rev. 3)**; add `gpu.gpu_index` + `gpu.backend` override (S+).
- `gpu/__init__.py`: document/implement the detection ordering + `gpu.backend` override (auto/nvidia/stub paths only; AMD/Intel are future-track stubs in the ordering doc) (S).
- `server.py`: build the net-new monotonic-anchored `_machine_snapshot_loop` + `_collect_machine_snapshot` + `_handle_snapshot`; **register `GET /broker/snapshot` in BOTH `create_app` and `create_admin_app` (4.10)** (M+, dual-factory tax).
- Tier 0 dead-metric wiring: `GPU_TEMPERATURE` (fast tick, skip on None), `COOLDOWN_WAITS_TOTAL`, `MODEL_SWAP_DURATION` (both swap branches). **`update_vram_usage`/bytes-gauge is NOT wired** (S).
- TTFT into `_stream_response.generate()` (proxy.py:591) (S).
- Extend the backend status query to 12 fields in `NvidiaBackend.query_status` (util, clocks, fan-read, mem-junction-temp, PCIe gen/width) + per-field parsing + `lines[gpu_index]` selection; `StubBackend` unchanged (still returns `GPUStatus()`) (M).
- PSI + swap-rate + OOM (`/proc` reads, shared `_read_vmstat`); PSI display thresholds read from `ObservabilityConfig` (S each).

**Phase 2 — remaining Tier 1 + token tap (correlation inputs complete). ~9–12 days.**
- `inference_tap.py` + `InferenceTapCollector` + extend `_extract_streaming_tokens` to capture `eval_duration`/`prompt_eval_duration` (ns→s, cache-hit guard) + tokens/sec + ctx-utilization tap (with `default_num_ctx` denominator fallback, rev. 3) in `_stream_response.generate()`; extend `record_recent_request` signature with 6 None-default kwargs (M).
- Block-device util/await via psutil perdisk with the **portable base-device regex** + `storage_device_filter` override; **TUI mount-label discovery** replacing the `/mnt/nvme_data` hard-code (S+); **CPU package power via RAPL with Intel+AMD probe order** + `rapl_domain_path` override (GPU-board hwmon path is Tier 4) (S+).
- GPU slow-path as **new async protocol methods** (`query_throttle_reasons`, `query_xid_errors`, `query_pcie_throughput`) added to `gpu/base.py` + `gpu/stub.py` (returns `[]`/`None`) + `NvidiaBackend` impls (M).
- `metrics.py`: **define NEW `VRAM_RECONCILE_STALE_TOTAL`/`VRAM_RECONCILE_IMPORT_TOTAL` Counters + helpers**; then wire them in `vram.py` reconcile() at 867/874; VRAM ledger drift gauge (slow tick; skip on backend-None) (M).
- `GET /broker/contention`, `/broker/gpu/extended` endpoints (**both factories, 4.10**) + `ContentionPanel` (`panels_system.py`) + `BastionClient.get_snapshot/get_contention` (M+, dual-factory tax).

**Phase 3 — Tier 2 correlation + process attribution (the moat). ~10–14 days.**
- **Async prerequisite:** convert `query_processes()` to `async def` on the **Protocol (`gpu/base.py`)**, in `NvidiaBackend` (`nvidia.py:71`), and in `StubBackend` — same commit, CHANGELOG-noted breaking change — and add the new async `query_process_utilization` (pmon) method likewise **before** they enter `_machine_snapshot_loop` (S, blocking gate).
- Process attribution: own-PID registry, top-N+IO, watchlist, churn, pmon, `GET /broker/processes` (**both factories**), `ProcessAttributionPanel` (`panels_processes.py`, new file; GPU section `(no GPU)` on StubBackend), modal refactor (M+).
- `constants.py`: move `_fan_band`/`_AUTO_FAN_HYSTERESIS_C` out of `app.py`; `audit.py`: add `get_events_since(cursor)` public accessor + `_event_seq` counter (S).
- `correlation.py`: `CorrelationRing` (cursor-based audit + recent-requests ingest) + `enrich_stall_reason` (None-omitting suffix) + `ContentionEventDetector` (two-unit thresholds — disk leg keys off `contention_block_write_mb_s_threshold`, PSI leg off `contention_psi_threshold`/`contention_cpu_psi_threshold`; PSI/disk legs degrade independently) + `compute_risk_index` (per-component None-tolerant) + `ThermalCoupling` (`_fan_band`-derived, configurable cpu ceiling, `gpu_ceiling`=`gpu.max_temperature_c`, all inputs None-tolerant) (**L — 3–4 days**, ~400+ line module).
- Wire engine into `_machine_snapshot_loop` (deps: scheduler/vram_tracker/vram_manager only); add `/broker/correlation/risk` + `/broker/correlation/contentions` (**both factories**; ring/stall folded into snapshot) + `CorrelationPanel` (`panels_correlation.py`, new file); new Prometheus metrics; `CorrelationConfig` (M+).

**Phase 4 — Tier 3 surfaces & governance (gated). ~4–6 days (parallelizable; several items blocked).**
- `scripts/check_metric_cardinality.py` permitted-set AST lint (label-NAME check, value-agnostic) + CI wiring (S).
- SSE `/broker/snapshot/stream` + dedupe `_sse_wrapper` (both factories if `/broker`-scoped) (M).
- MCP `broker_snapshot_v1` — **blocked on `mcp_adapter` (v0.5, ADR-007); shipping it triggers ADR-005 gating event #1 — draft ADR-005-B (Section 9)** (M).
- Grafana panel catalogue + `dashboards/grafana/` panels — **gated on Vision C base** (M, ADR-010 CI).
- `broker.yaml.example`: document the new `observability:` block with per-key tuning notes — **NVMe/block threshold (~50–70% of the drive's sustained write), `cpu_safe_ceiling_c` (your CPU Tjmax), `gpu.max_power_watts`/`max_temperature_c` (auto-detected on NVIDIA, set manually on other GPUs), the per-model `vram_gb` measurement caveat (rev. 3)** (S).
- Spec doc + ADR-009 baseline note (S).

---

## 9. Relationship to the 2026-03-13 Observability Spec and ADR-005/007/009/010

This spec **extends** the 2026-03-13 observability-first design; it does not duplicate it.

- **What 2026-03-13 already owns (do NOT re-implement):** the `BrokerStatus` field wiring (`swap_rate_level`, `stall_reason`, `inflight_models`, `circuit_breaker`, `gpu_is_safe`, `max_vram_gb` — T1-03…T1-10); the 12 plumbing endpoints in its Section 4; the RequestID middleware; the OTel span wiring; the base Grafana artifacts. Any expansion endpoint is checked against that list first — none collide.
- **Critical correction inherited from 2026-03-13:** that spec proposed a `_gauge_update_loop` ("Periodic Gauge Updater", Section 2) that was **never built** (verified absent across `src/` and `tests/`). This spec **builds it** as `_machine_snapshot_loop` and makes it the single collection authority. The dead Prometheus metrics genuinely activated **here** (Phase 1) are `gpu_temperature_celsius`, `cooldown_waits_total`, and `model_swap_duration_seconds`. **Correction vs rev. 1:** `vram_used_bytes` is NOT activated — the Vision-C frozen `bastion_vram_used_mb` is already emitted at vram.py:345, and the bytes gauge at the same site would be a redundant second object. Our SSE `/broker/snapshot/stream` **supersedes** its `/broker/status/stream`.
- **ADR-005 (direct-accessor contract) — governance note added.** Panels keep `render_data(data: dict)` — no subscriber bus. The new panels (`ContentionPanel`, `ProcessAttributionPanel`, `CorrelationPanel`) are `BastionPanel` subclasses accepting the snapshot dict. The TUI keeps polling `/broker/status` (+ `/broker/snapshot`); it is never converted to SSE; SSE is an external-surface `StreamingResponse`, distinct from in-process pub/sub, so it does not trip ADR-005-B. **However — explicit governance record (per completeness review):** shipping the MCP adapter's `broker_snapshot_v1` (Tier 3, Phase 4) IS the "third operational surface beyond TUI and Grafana," which is **ADR-005 gating event #1**. Per the ADR, that event reopens ADR-005 to draft **ADR-005-B**. **This spec records the deferral explicitly:** ADR-005-B is deferred at v0.5 because (a) Vision E (event-driven policy) is still not the chosen architecture, (b) the direct-accessor contract is sufficient for every panel currently in or scoped by this spec (the MCP surface consumes the HTTP snapshot endpoint, not an in-process bus, so it imposes no subscriber requirement), and (c) the subscriber-pattern cost still exceeds its value at the current panel count. **Action:** when `broker_snapshot_v1` actually ships, draft ADR-005-B as a separate document recording either this same deferral with updated reasoning or a decision to build the bus — the trigger must not be left in an implicitly-fired, unrecorded state. The trigger is the adapter shipping (Phase 4), not this spec; this spec ships only the HTTP endpoint the adapter will wrap.
- **ADR-007 (MCP versioning):** `broker_snapshot_v1` follows the `_v<N>` suffix + committed JSON Schema + adapter-side validation convention exactly; it is Tier 3, blocked on the `mcp_adapter` package (v0.5). The endpoint ships first; the tool wraps it.
- **ADR-009 (TUI deprecation instrumentation):** Orthogonal but not skipped — the new panels measure from the same `tui_session_start` baseline. No new TUI-deprecation signals are added here.
- **ADR-010 (Grafana CI compatibility):** Every new Prometheus metric must have a matching panel in `dashboards/grafana/*.json`, validated by the ADR-010 multi-version CI. Expansion metric names are **not** schema-frozen (the freeze covers only the Vision C set: `bastion_request_queue_wait_seconds`, `bastion_vram_used_mb`, `bastion_thrashing_detector_halt_total`, `bastion_concurrent_requests_active`, `bastion_model_swap_total`); new names are proposed in the panel-catalogue doc for cardinality/naming review before code, freezing at the v0.6 tag.

---

## 10. Risks, Open Questions, and Test Strategy

### 10.1 Cross-cutting risks (resolved here)

- **Phantom `_gauge_update_loop`** — five sections assumed it existed; resolved by building it once as the monotonic-anchored `_machine_snapshot_loop` (4.9).
- **Dual-factory route trap (CORRECTED — rev. 1 was wrong).** `create_app` (broker_router at server.py:836) and `create_admin_app` (separate broker_router at server.py:1707) are **independent**; each has its own `include_router` (server.py:1570 / 2325). Every new `/broker/*` route MUST be registered in **both** factories or it 404s in the admin-only deployment. Rev. 1's claim that routes "need adding once" was false (4.10).
- **NVIDIA-only assumptions leaking above the backend (rev. 3).** nvidia-smi field names and the `NVRM: Xid` literal previously appeared in spec prose for signal-table rows; resolved by routing **every** GPU signal through new/extended `GPUBackend` protocol methods (`query_throttle_reasons`, `query_xid_errors`, `query_pcie_throughput`, `query_process_utilization`, extended `query_status`), each with a `StubBackend` `[]`/`None` return, so non-NVIDIA / no-GPU hosts inherit the correct empty contract and no higher layer parses vendor strings (5.1).
- **Static GPU safety ceilings / TDP not device-detected (rev. 3).** `max_temperature_c`/`max_power_watts`/`gpu_safe_ceiling_c` were static constants (83 °C / 300 W) that cause premature cutoffs or false alarms on server GPUs; resolved by extending `resolve_gpu_defaults` to read `tlimit`/`shutdown`/`power.limit` from the device at startup (the existing VRAM auto-detect pattern), falling back to the constants with an INFO/WARNING log (4.8, 6.5).
- **Intel-RAPL-only CPU power (rev. 3).** `cpu_package_watts` previously named only `intel-rapl:0`, producing silent `None` on AMD hosts; resolved by a probe order (Intel → AMD `amd_energy` → None) + `rapl_domain_path` override (5.2).
- **Hard-coded block device / mount / CPU sensor (rev. 3).** `nvme0n1`, `/mnt/nvme_data`, and the `(k10temp, coretemp)` allowlist were developer-box specific; resolved by a portable base-device regex (`nvme*/sd*/vd*/mmcblk*`) + `storage_device_filter`, `psutil.disk_partitions` discovery + `disk_mount_labels`, and a CPU-sensor priority list + any-`temp*_input` fallback + `cpu_sensor_name` (3#7d, 5.2).
- **Single-GPU scalar assumptions (rev. 3).** `lines[0]` and `{gpu_index="0"}` hard-coded GPU 0; resolved by `GPUStatus.gpu_index` + `gpu.gpu_index` selector (list-extensible schema) and the multi-GPU-safe drift alert `max by(gpu_index)(abs(...))>2048` — without building full multi-GPU iteration (scope guardrail) (4.2, 5.1, 5.4).
- **Two tap claims on one stream** — resolved by the single `InferenceTapCollector` on `_stream_response.generate()` (proxy.py:591), not the passthrough (proxy.py:523) (5.4).
- **Missing duration fields for tokens/sec** — `_extract_streaming_tokens` did not parse `eval_duration`/`prompt_eval_duration`; resolved by extending the helper + ns→s + cache-hit guard (5.4).
- **Non-existent reconcile counters** — `bastion_vram_reconcile_*` did not exist in `metrics.py`; resolved by defining them as NEW Counters + helpers before wiring (5.4).
- **Wrong Tier-0 vram target** — `update_vram_usage` (bytes) would double-emit against the live `update_vram_used_mb` (MB); removed from scope (5.4).
- **Swap-duration boundary** — resolved by capturing `swap_start` before the if/else split and recording after `_dispatch_for_model` in **both** branches (5.4).
- **Audit private-deque coupling + unstable cursor** — resolved with a public `audit.get_events_since(cursor)` + monotonic `_event_seq` (6.1).
- **Sync `query_processes` on the event loop** — would block up to 5s/tick; resolved by an async-conversion prerequisite for the Protocol, `NvidiaBackend`, and `StubBackend` together (5.3).
- **Thermal-coupling constant desync + wrong import direction + undefined `gpu_ceiling`** — resolved by deriving `coupling_active` from `_fan_band()` (moved to a shared `constants.py`), and binding `gpu_ceiling` to `gpu.max_temperature_c` (device-auto-detected) (6.5).
- **`_a2a_handler` false dependency** — engine depends only on scheduler/vram_tracker/vram_manager; A2A-disabled is fully functional (6.6).
- **GPU-board-power gold-plating** — amdgpu/RAPL-on-AMD hwmon deferred to Tier 4 as a future **backend** track (board power is backend-provided, `None` until a backend fills it — not "skipped because the dev box is NVIDIA"); only Intel/AMD host CPU RAPL ships (2, 5.2, 4.8).
- **API-surface bloat** — the 4 rev.-1 `/broker/correlation/*` endpoints reduced to 2 (`/risk`, `/contentions`); `/ring` and `/stall` folded into `/broker/snapshot` (6.1, 6.2, 5.6).
- **Slow-tick partial-failure** — each sub-collector is `try/except`+timeout-wrapped; partial snapshots are valid (Constraint #4/#7e).

### 10.2 Open questions (residual)

1. **Single 12-field nvidia-smi call vs split 5+7?** Recommend single (one subprocess/tick) with per-field `_safe_int` guards; validated against the live driver column order before Phase 1 lands. (Lives inside `NvidiaBackend`; no portability impact above the backend.)
2. **Throttle Prometheus: Counter (rising-edge) vs per-reason Gauge?** Recommend Counter for Alertmanager parity (matches thrashing); expose "currently throttling" via TUI/JSON only.
3. **Xid `dmesg --since` fallback** (util-linux <2.29 lacks `--since`): the last-100-lines fallback + rising-edge dedup prevents double-counting; confirm the iso time-format is present on the target distro.
4. **`/broker/snapshot` consolidation — Phase-2 DECISION GATE.** Whether `/broker/snapshot` eventually replaces `/broker/status` as the dashboard poll target must be decided **before Phase 1 ships** (Grafana/MCP harden against whichever ships first). Recommended: keep both for v0.5; commit the migration decision in writing at the Phase-1 gate.
5. **GPU detection ordering for future AMD/Intel backends (rev. 3).** The ordering and `gpu.backend` override are documented now (5.1); the actual AMD/Intel backends are a future track. Open: whether to ship a *minimal* read-only AMD temp/util backend (rocm-smi) in v0.6, or keep StubBackend for all non-NVIDIA until a fuller backend lands. Recommend StubBackend-only for v0.5 (scope guardrail).
6. **`cpu_safe_ceiling_c` default (85 °C)** — reasonable conservative fallback; operators tune to their CPU's Tjmax (logged at startup with the key name). Not auto-detected from `temp*_crit` (absent on many drivers).
7. **Block-device contention default (200 MB/s write)** — config-driven and explicitly device-dependent; confirm it fires sensibly on the operator's drives; dynamic idle-calibration remains the recommended portable default (Tier 4).
8. **ECC opt-in** — designed-but-disabled `observability.ecc_enabled` (Tier 4), on the `GPUBackend` protocol; justified by server GPUs in the wild, near-zero cost if enabled.
9. **Per-model `vram_gb` values in `broker.yaml` (rev. 3).** The shipped values were measured on the developer's specific GPU at specific quant/num_ctx and will differ on other hardware (page size, ECC overhead, KV-cache allocation). Architecture is already correct (`vram_gb` is per-model, user-configurable); resolved by a documentation caveat in `broker.yaml.example` directing users to re-measure via `bastion --detect-models`. Not a code change.

(Rev.-1/2 open questions resolved in the body — separate extended endpoint (4.3), `panels_processes.py` (5.3), signed Gauge (5.4/7), model-only label (5.4), amdgpu→Tier 4 (2), system-column placement + two-column secondary grid (7.1) — and removed from this list.)

### 10.3 Test strategy

- **Per-collector unit tests** with mocked sources are mandatory and the **degradation path is the tested default**: `dmesg_restrict` PermissionError→`[]`; `dmesg` rc=1+empty stdout→`[]`; PSI `exists()=False`→all None; RAPL absent (Intel **and** AMD paths missing)→None; **unknown CPU sensor (neither k10temp nor coretemp, no `temp1_input`)→None**; psutil `AccessDenied`→None-but-kept; `[N/A]` fields→None; `eval_duration==0`→rate None; first-read deltas→None. No path may emit a misleading `0`.
- **"Other hardware" is an explicit tested matrix (rev. 3):**
  - **No-GPU / non-NVIDIA:** with the active backend = `StubBackend`, assert `query_status()` → all-`None` `GPUStatus`, `query_throttle_reasons()`/`query_xid_errors()`/`query_process_utilization()` → `[]`, `query_pcie_throughput()` → `(None, None)`; the snapshot is still produced; `pcie_downgraded` is False; the GPU-temp gauge is skipped (not 0); the ProcessAttributionPanel GPU section shows `(no GPU)`; the RiskIndex/ThermalCoupling compute from CPU/host terms only without error.
  - **Multi-GPU:** `gpu.gpu_index=1` selects the 2nd nvidia-smi line; the drift alert PromQL `max by(gpu_index)(abs(...))` fires per index.
  - **AMD RAPL:** with `intel-rapl` absent and `amd_energy` present, `read_package_power()` returns a value from the AMD path (not None).
  - **Block devices:** `sda`/`vdb`/`mmcblk0` perdisk keys produce `BlockDeviceIOStats` rows; partitions/loop/dm keys are excluded.
  - **No PSI:** `/proc/pressure` absent → all PSI fields None, ContentionPanel degrades, no exception.
  - **CPU sensor discovery:** a `nct6775`/`zenpower` hwmon (via the priority list or any-`temp*_input` fallback) yields a temp; `cpu_sensor_name` override pins a specific one.
  - **Device-auto-detect:** `resolve_gpu_defaults` sets `max_temperature_c` from a mocked `tlimit`/`shutdown`, and falls back to the constant + WARNING when nvidia-smi is absent.
- **Stream-tap integration**: mock httpx stream on `_stream_response.generate()`; assert (a) TTFT observed exactly once per streaming request and never on non-streaming/passthrough, (b) decode_tps/prefill_tps/ctx_util appear in `record_fn` kwargs with ns→s conversion, (c) `eval_duration==0` cache-hit yields `decode_tps=None`, (d) ctx_util uses the `default_num_ctx` fallback when no per-model entry, (e) chunks still yielded immediately (no buffering regression).
- **`record_recent_request` regression**: existing callers omitting the six new kwargs still work (None defaults); the new kwargs land in the `_recent_requests` dict.
- **Tier 0 spies**: cooldown called once per cooldown-sleep; swap-duration called with model post-swap in **both** the semaphore and no-semaphore branches; gpu_temperature called with the value on the fast tick, skipped on None; **assert vram.py:345 still calls only `update_vram_used_mb` (no bytes double-emit)**.
- **Reconcile counters**: assert `VRAM_RECONCILE_STALE_TOTAL`/`VRAM_RECONCILE_IMPORT_TOTAL` exist in `metrics.py` before wiring; `reconcile()` increments stale on removal / import on new model; `reconcile(None)` increments nothing.
- **Prometheus**: label-set assertions (bounded enums only); rising-edge counters do not double-emit; the CI cardinality lint fails on a planted `labelnames=['pid']` and passes on `labelnames=['gpu_index']` / `['device','op']` (permitted-set, **label-name** check — a `device="sda"` value is never rejected).
- **Audit cursor**: `get_events_since(cursor)` returns only newer events + a stable monotonic cursor; cursor survives a ring wrap without re-emitting or skipping; private `_recent_events` is never imported by `correlation.py`.
- **Backend protocol conformance (rev. 3)**: a test instantiates `StubBackend` and asserts it implements every new async protocol method with the empty contract (`[]`/`None`/`(None,None)`); a test asserts `query_processes` is `async` on the Protocol, `NvidiaBackend`, and `StubBackend` (no sync `subprocess.run` reachable from the loop).
- **Correlation engine**: ring caps at maxlen + ingests all four domains; `enrich_stall_reason(None)` returns base unchanged + length ≤150 and omits None clauses; ContentionEventDetector — single tick → no event; **no event when stall_reason is empty even if write > threshold** (the coincidence join is the contract); 2-tick hysteresis; **PSI/disk legs degrade independently when one input is None**; RiskIndex property test (`score ∈ [0,1]` for any input incl. all-None→nominal, dominant_factor always one of the 5 names, a None component contributes 0 without crashing); `coupling_active == (cpu_temp is not None and _fan_band(cpu_temp) is not None)`; `thermal_headroom_min_c` uses `cpu_safe_ceiling_c` (not 60) and `gpu_ceiling=gpu.max_temperature_c`, skips the GPU term when gpu_temp is None.
- **Endpoints (dual-factory)**: each new route returns 200 + correct shape when populated, empty-lists (not 404) before first collection, auth enforced, `?history=N` capped, `?include_ring=true` expands the ring; **and is present in BOTH `create_app` and `create_admin_app`** (a test that builds both apps and asserts the route exists in each).
- **A2A-disabled**: with `config.a2a.enabled=False`, the correlation engine still initializes and inference events still flow (proxy done-path), proving no `_a2a_handler` dependency.
- **Regression**: all existing tests pass; new fields are optional/None-default so existing `BrokerStatus`/`/broker/status` consumers are unaffected.
- Runs go through the Tier-0 wrapper (`the project test suite`), escalating to the the test runner only on verbose failures; the destructive e2e suite stays `BASTION_E2E=1`-gated.

**Recommended doc path:** `docs/design/specs/2026-06-19-observability-expansion.md` (verified: design specs live in `docs/design/specs/`; ADRs in `docs/adrs/`; there is no `.doc-config.yaml`; the prior observability spec lives at `docs/design/specs/2026-03-13-observability-first-design.md`).