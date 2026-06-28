"""GPU profile table -- known-safe defaults for common NVIDIA GPUs.

Maps GPU names (from nvidia-smi) to safe operating parameters. Used by
``bastion validate`` for pre-flight checks and ``bastion stress-test``
as initial estimates before calibration.

Unknown GPUs receive conservative defaults. Users can contribute profiles
for their hardware via pull request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GPUProfile:
    """Safe operating parameters for a specific GPU model."""

    name: str
    vram_total_mb: int
    safe_swap_rate: int          # Max safe model swaps per minute
    vram_headroom_mb: int        # VRAM to reserve for OS/CUDA/display
    thermal_ceiling_c: int       # Max temp before pausing scheduling
    cooldown_seconds: int        # Minimum seconds between model swaps
    notes: str | None = None     # Hardware-specific warnings


# Default profile for unknown GPUs -- a conservative floor for an unknown card;
# calibrate the real ceiling for your hardware via --stress-test.
_DEFAULT_PROFILE = GPUProfile(
    name="Unknown GPU",
    vram_total_mb=0,             # 0 = must be detected at runtime
    safe_swap_rate=3,
    vram_headroom_mb=4096,
    thermal_ceiling_c=80,
    cooldown_seconds=3,
)

# Known GPU profiles -- keyed by substring that appears in nvidia-smi output.
# Order matters: first match wins, so put specific names before general ones.
_PROFILES: list[tuple[str, GPUProfile]] = [
    ("RTX 5090", GPUProfile(
        name="RTX 5090",
        vram_total_mb=32768,
        safe_swap_rate=4,
        vram_headroom_mb=8192,
        thermal_ceiling_c=80,
        cooldown_seconds=2,
        notes="use_mmap: false mandatory -- memory-mapped loading causes instability",
    )),
    ("RTX 4090", GPUProfile(
        name="RTX 4090",
        vram_total_mb=24576,
        safe_swap_rate=5,
        vram_headroom_mb=6144,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4080", GPUProfile(
        name="RTX 4080",
        vram_total_mb=16384,
        safe_swap_rate=4,
        vram_headroom_mb=4096,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4070", GPUProfile(
        name="RTX 4070",
        vram_total_mb=12288,
        safe_swap_rate=4,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 4060", GPUProfile(
        name="RTX 4060",
        vram_total_mb=8192,
        safe_swap_rate=3,
        vram_headroom_mb=2048,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("RTX 3090", GPUProfile(
        name="RTX 3090",
        vram_total_mb=24576,
        safe_swap_rate=4,
        vram_headroom_mb=6144,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 3080", GPUProfile(
        name="RTX 3080",
        vram_total_mb=10240,
        safe_swap_rate=4,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("RTX 3070", GPUProfile(
        name="RTX 3070",
        vram_total_mb=8192,
        safe_swap_rate=3,
        vram_headroom_mb=2048,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("RTX 3060", GPUProfile(
        name="RTX 3060",
        vram_total_mb=12288,
        safe_swap_rate=3,
        vram_headroom_mb=3072,
        thermal_ceiling_c=83,
        cooldown_seconds=3,
    )),
    ("A100", GPUProfile(
        name="A100",
        vram_total_mb=81920,
        safe_swap_rate=6,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("A6000", GPUProfile(
        name="A6000",
        vram_total_mb=49152,
        safe_swap_rate=5,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("L40", GPUProfile(
        name="L40",
        vram_total_mb=49152,
        safe_swap_rate=5,
        vram_headroom_mb=8192,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
    ("L4", GPUProfile(
        name="L4",
        vram_total_mb=24576,
        safe_swap_rate=5,
        vram_headroom_mb=4096,
        thermal_ceiling_c=83,
        cooldown_seconds=2,
    )),
]


def lookup_profile(gpu_name: str) -> GPUProfile:
    """Look up a GPU profile by name from nvidia-smi output.

    Matches by substring (case-insensitive). Returns the default
    conservative profile for unknown GPUs.

    Parameters
    ----------
    gpu_name : str
        GPU name as reported by nvidia-smi (e.g. "NVIDIA GeForce RTX 4090").

    Returns
    -------
    GPUProfile
        Matching profile, or conservative defaults for unknown hardware.
    """
    name_lower = gpu_name.lower()
    for key, profile in _PROFILES:
        if key.lower() in name_lower:
            return profile
    return _DEFAULT_PROFILE
