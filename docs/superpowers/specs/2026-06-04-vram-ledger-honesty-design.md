# VRAM Ledger Honesty + Admission-Gate Backstop — Design

- **Date:** 2026-06-04
- **Status:** Approved (brainstorm) → ready for implementation plan
- **Branch:** `feat/vram-ledger-honesty`
- **Scope:** Items "B" (dashboard honesty) + "C" (admission-gate hole) from the GPU-vs-VRAM discrepancy investigation.

## Context

The dashboard shows three VRAM figures from three different sources, and they disagree:

1. **GPU panel** (`/broker/status.gpu`) — `nvidia-smi memory.used/total` (`gpu/nvidia.py:31`). Whole-card physical
   bytes across *all* processes, including Ollama runtime overhead (KV cache, CUDA context, compute buffers) and
   non-Ollama consumers (compositor, browser).
2. **Models panel** — Ollama `/api/ps` sizes / config estimates.
3. **VRAM Ledger panel** (`/broker/vram` → `VRAMManager.status()`) — a *logical* estimate: sum of static
   `config.models[*].vram_gb` for models the scheduler swapped in, plus a 10% safety margin counted as "used".

Most of the divergence is by design (three instruments measuring different things). Two findings are genuine
problems worth fixing:

- **The logical ledger is the *sole* admission gate.** In the normal `VRAMManager`-active path the scheduler calls
  `vram_manager.reserve()` and **never** consults the nvidia-smi free-VRAM hard gate; that hardware check lives only
  in `can_load_model()` (`vram.py:278-290`), which runs solely in the `else` fallback when there is no `VRAMManager`
  (`scheduler.py:514-540`). So if the ledger is wrong, nothing catches an over-commit — the exact crash class
  BASTION exists to prevent.
- **The ledger systematically under-counts.** It only *adds* allocations on a scheduler swap; `reconcile()`
  (`vram.py:619`) is one-directional (removes stale entries, never imports). Models that became resident without a
  BASTION swap (loaded before startup, or by a client reaching Ollama directly) are invisible to the ledger.

## Goals

1. **C-safety:** Ground the admission gate in hardware truth — add an nvidia-smi free-VRAM cross-check at
   `reserve()` time, in addition to the logical check.
2. **C-accuracy:** Stop the ledger under-counting — make `reconcile()` bidirectional (import resident-but-untracked
   models).
3. **B-display:** Make the measured-vs-reserved gap self-explanatory in the VRAM Ledger panel (Measured + Δ rows;
   stop counting the safety margin as "used").

## Non-goals

- No change to `/broker/*` API shapes or `config/broker.yaml` schema.
- No change to `always_allowed` (embedding) budget semantics (see Decision D2).
- Not attempting to make the ledger *equal* nvidia-smi — they are different instruments. We make the gap honest
  and the gate safe.

## Design

### Section 1 — C-safety: nvidia-smi backstop at `reserve()`

- New helper on `VRAMManager`:
  ```
  HARDWARE_MARGIN_GB = 2.0  # module constant; matches existing can_load_model margin

  async def _hardware_admits(self, vram_bytes: int, margin_gb: float = HARDWARE_MARGIN_GB)
          -> tuple[bool, float | None]:
      free_gb = await get_vram_free_gb()           # already imported, vram.py:22
      if free_gb is None:
          return True, None                        # FAIL-OPEN (decision D3)
      required = vram_bytes / (1024**3) + margin_gb
      return free_gb >= required, free_gb
  ```
- `reserve()` calls `_hardware_admits()` **before acquiring `self._lock`** (the await must not enter the atomic
  critical section that the existing invariant at `vram.py:489-491` protects). Inside the lock, after the logical
  `available_vram` check, evaluate the hardware verdict and raise `ValueError` (rejection) if the hardware says no.
  Reservation deduction stays synchronous and atomic.
- `can_load_model()` is refactored to call the same `_hardware_admits()` helper, removing the duplicated
  free-VRAM logic at `vram.py:278-290` (one source of truth for the margin).

**Rejected alternative:** running the nvidia-smi `await` inside `self._lock` — breaks the atomic reserve critical
section and serializes all reservations behind a GPU query. The pre-lock query keeps a tiny TOCTOU window that the
synchronous logical deduction still closes.

### Section 2 — C-accuracy: bidirectional `reconcile()`

- Signature change: `reconcile(loaded_model_names: set[str] | None)` → `reconcile(loaded_models: list[LoadedModel] | None)`.
  `None` still means "tracker state unknown → no-op" (preserves outage-safety).
- `ResidencyCache` gains `get_resident_loaded_models() -> list[LoadedModel] | None`, exposing its already-cached
  `LoadedModel` list (`vram.py:47`) — **no extra `/api/ps` query**.
- `reconcile()` behavior:
  - Derive `loaded_names = {m.name for m in loaded_models}`.
  - **Removal (existing):** drop `_model_allocations` whose model ∉ `loaded_names`.
  - **Import (new):** for each loaded model not already tracked, set
    `_model_allocations[name] = int(m.vram_gb * 1024**3)` and increase `_allocated`.
  - **Double-count guards:** skip a model that (a) has an active reservation in `_reservations`, or
    (b) is already in `_model_allocations`, or (c) is `always_allowed` (decision D2).
  - Emit a `vram_import` audit event when imports occur (forensics parity with `vram_reconciliation`).
- Call-site updates: `scheduler.py:295` passes the cached `LoadedModel` list; `tests/test_vram_state_unknown_extra.py`
  `reconcile(set())` → `reconcile([])`.

### Section 3 — B-display: Measured + Δ rows, margin split out

- `VRAMLedgerPanel.render_data` (`panels_gpu.py:118`) signature:
  `render_data(self, ledger, measured_used_mb: float | None = None)`.
- `app.py:448` passes `measured_used_mb = data.get("gpu", {}).get("vram_used_mb")` — `/broker/status` is already
  fetched each refresh, so no new API call and no endpoint change.
- New panel rows:
  - **Measured** — nvidia-smi `vram_used_mb` (cyan): "what the card actually holds".
  - **Δ overhead** — `measured − (allocated + reserved)` (dim/yellow), labeled *runtime overhead + untracked*;
    rendered signed so a persistently negative Δ honestly signals config estimates running high.
  - Both rows render only when `measured_used_mb is not None`.
- **Usage bar** (`panels_gpu.py:143`): `used = (allocated or 0) + (reserved or 0)` — the safety margin is **no
  longer** part of "used" (the existing "Safety" row continues to show it separately). Bar % = `used / total`.

## Decisions

- **D1 — Defense-in-depth (both fixes).** Reconcile import (accuracy) *and* nvidia-smi backstop (safety), not one
  or the other.
- **D2 — `always_allowed` excluded from reconcile import.** Matches the existing budget convention
  (`vram.py:264-266` and scheduler exclude `always_allowed` from budget math). Their physical footprint appears in
  the Δ row, which is honest, and we avoid regressing embedding eviction/budget behavior. The nvidia-smi backstop is
  what prevents over-commit regardless.
- **D3 — Backstop fails open.** When nvidia-smi returns no reading, skip the hardware check and trust the
  (now bidirectionally-reconciled) ledger. Matches existing `can_load_model` precedent (`vram.py:278` only checks
  `if free_gb is not None`) and avoids a flaky nvidia-smi blocking all loads.

## File-by-file changes

| File | Change |
|------|--------|
| `src/bastion/vram.py` | `HARDWARE_MARGIN_GB`; `_hardware_admits()`; backstop in `reserve()`; refactor `can_load_model()`; `reconcile()` bidirectional + new signature; `ResidencyCache.get_resident_loaded_models()` |
| `src/bastion/scheduler.py` | `reconcile()` call at `:295` passes cached `LoadedModel` list |
| `src/bastion/dashboard/panels_gpu.py` | `VRAMLedgerPanel.render_data` Measured/Δ rows; Usage bar excludes margin |
| `src/bastion/dashboard/app.py` | pass `measured_used_mb` into `render_data` at `:448` |
| `tests/test_vram_state_unknown_extra.py` | update `reconcile()` call signature |
| `tests/` (new) | backstop, bidirectional reconcile, panel-render tests |

## Test plan (TDD — tests written first)

- **Backstop:** reserve rejects when `get_vram_free_gb()` < requested + margin; accepts when sufficient; fail-open
  when `None`; `_hardware_admits` math correct; reserve's atomic critical section unaffected (no await inside lock).
- **Reconcile:** imports an untracked resident model (allocated rises by its estimate); skips models with an active
  reservation (no double-count); skips `always_allowed`; still removes stale; `None` is a no-op; `[]` clears stale.
- **Panel:** Measured + Δ rows present when `measured_used_mb` given and absent when `None`; Δ sign correct; Usage
  bar uses `allocated + reserved` (margin excluded).
- Pytest command is **printed for the user** (per CLAUDE.md — never auto-run the suite).

## Risks & mitigations

- **TOCTOU between free-VRAM read and reservation** — mitigated: logical deduction stays atomic; backstop is an
  additional best-effort guard, not the only one.
- **Import/commit race double-count** — mitigated by the active-reservation guard in reconcile.
- **Δ noise from non-Ollama consumers** — acceptable and intended; the row is labeled as overhead/untracked.
- **Signature change ripple** — only two call sites (`scheduler.py`, one test), both updated here.

## Rollout

Single branch `feat/vram-ledger-honesty`, sequenced C-safety → C-accuracy → B-display so the gate is hardened
before bookkeeping/display. No migration; in-memory state only.
