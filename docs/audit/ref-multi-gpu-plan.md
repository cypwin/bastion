
# Phase XYZ — Aspirational: (Multi-GPU & Scaling)

> **This phase is OPTIONAL.** Only proceed if you have multi-GPU hardware to test against.
> is architecturally significant but cannot be properly validated on a single GPU.

## If proceeding

Read `ROADMAP.md` section  and Appendix A (dependency graph). The key insight: start with the **per-GPU VRAM tracker** and **GPU-aware scheduler** only. The distributed broker protocol (cluster.py, loadbalancer.py, migration.py) is a separate milestone that requires multi-machine infrastructure.

**Minimum viable S9:**
1. Extend `VRAMTracker` and `GPUStatus` for N GPUs (nvidia-smi multi-GPU query)
2. Extend scheduler for per-GPU `_current_model` and per-GPU cooldown
3. Add `placement.py` for model-to-GPU assignment
4. Config: `gpu_affinity` in `broker.yaml`
5. Tests must pass on single-GPU (default behavior unchanged)

**Defer to S9b:**
- `cluster.py` (distributed broker)
- `loadbalancer.py`
- `migration.py`

```
git commit -m "feat(): per-GPU VRAM tracking and GPU-aware scheduling"
```
