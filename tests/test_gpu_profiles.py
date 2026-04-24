"""Tests for GPU profile table and lookup."""

from __future__ import annotations

from bastion.gpu_profiles import GPUProfile, lookup_profile


class TestLookupProfile:
    """Test GPU profile lookup by name."""

    def test_exact_match_rtx_4090(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 4090")
        assert profile.name == "RTX 4090"
        assert profile.vram_total_mb == 24576
        assert profile.safe_swap_rate == 5

    def test_exact_match_rtx_3060(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 3060")
        assert profile.name == "RTX 3060"
        assert profile.vram_total_mb == 12288

    def test_partial_match(self) -> None:
        """nvidia-smi may report just 'RTX 4090' without 'GeForce'."""
        profile = lookup_profile("RTX 4090")
        assert profile.name == "RTX 4090"

    def test_unknown_gpu_returns_default(self) -> None:
        profile = lookup_profile("Some Future GPU 9999")
        assert profile.name == "Unknown GPU"
        assert profile.safe_swap_rate == 3
        assert profile.vram_headroom_mb == 4096
        assert profile.thermal_ceiling_c == 80
        assert profile.cooldown_seconds == 3

    def test_case_insensitive(self) -> None:
        profile = lookup_profile("nvidia geforce rtx 4090")
        assert profile.name == "RTX 4090"

    def test_rtx_5090_has_mmap_note(self) -> None:
        profile = lookup_profile("NVIDIA GeForce RTX 5090")
        assert profile.notes is not None
        assert "use_mmap" in profile.notes


class TestGPUProfile:
    """Test GPUProfile model."""

    def test_profile_fields(self) -> None:
        profile = GPUProfile(
            name="Test GPU",
            vram_total_mb=8192,
            safe_swap_rate=3,
            vram_headroom_mb=2048,
            thermal_ceiling_c=83,
            cooldown_seconds=3,
        )
        assert profile.name == "Test GPU"
        assert profile.vram_total_mb == 8192

    def test_profile_optional_notes(self) -> None:
        profile = GPUProfile(
            name="Test",
            vram_total_mb=8192,
            safe_swap_rate=3,
            vram_headroom_mb=2048,
            thermal_ceiling_c=83,
            cooldown_seconds=3,
        )
        assert profile.notes is None
