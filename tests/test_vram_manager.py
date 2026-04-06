"""Tests for VRAMManager: reserve/commit/release lifecycle, double-release, semaphore, convergence.

Covers D1 coverage gaps:
  - reserve/commit/release lifecycle
  - double-release safety
  - semaphore serialization
  - convergence polling timeout
  - expired reservation reclamation
  - VRAM reserve under contention
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import BrokerConfig, GPUConfig, ModelInfo
from bastion.vram import VRAMManager, VRAMReservation, VRAMTracker


@pytest.fixture
def vram_config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={"test:7b": ModelInfo(vram_gb=5.0)},
    )


@pytest.fixture
def tracker(vram_config: BrokerConfig) -> VRAMTracker:
    return VRAMTracker(vram_config)


@pytest.fixture
def manager(tracker: VRAMTracker) -> VRAMManager:
    total_bytes = 32 * 1024 * 1024 * 1024  # 32 GB
    return VRAMManager(tracker, total_bytes, safety_margin_pct=10.0)


# ---------------------------------------------------------------------------
# Reserve / Commit / Release lifecycle
# ---------------------------------------------------------------------------


class TestReserveCommitRelease:
    @pytest.mark.asyncio
    async def test_reserve_deducts_from_available(self, manager: VRAMManager) -> None:
        initial = manager.available_vram
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        assert manager.available_vram == initial - 1_000_000_000
        assert manager.reserved_bytes == 1_000_000_000
        assert manager.allocated_bytes == 0
        await manager.release(reservation)

    @pytest.mark.asyncio
    async def test_commit_moves_reserved_to_allocated(self, manager: VRAMManager) -> None:
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        await manager.commit(reservation)
        assert manager.reserved_bytes == 0
        assert manager.allocated_bytes == 1_000_000_000
        assert reservation.committed is True
        await manager.release(reservation)

    @pytest.mark.asyncio
    async def test_release_pending_reservation(self, manager: VRAMManager) -> None:
        initial = manager.available_vram
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        await manager.release(reservation)
        assert manager.available_vram == initial
        assert manager.reserved_bytes == 0

    @pytest.mark.asyncio
    async def test_release_committed_reservation(self, manager: VRAMManager) -> None:
        initial = manager.available_vram
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        await manager.commit(reservation)
        await manager.release(reservation)
        assert manager.available_vram == initial
        assert manager.allocated_bytes == 0

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, manager: VRAMManager) -> None:
        """reserve -> commit -> release full cycle."""
        initial = manager.available_vram
        r = await manager.reserve("test:7b", 500_000_000)
        assert manager.reserved_bytes == 500_000_000
        await manager.commit(r)
        assert manager.allocated_bytes == 500_000_000
        assert manager.reserved_bytes == 0
        await manager.release(r)
        assert manager.allocated_bytes == 0
        assert manager.available_vram == initial

    @pytest.mark.asyncio
    async def test_insufficient_vram_raises(self, manager: VRAMManager) -> None:
        huge = manager.available_vram + 1
        with pytest.raises(ValueError, match="Insufficient VRAM"):
            await manager.reserve("test:7b", huge)

    @pytest.mark.asyncio
    async def test_multiple_reservations(self, manager: VRAMManager) -> None:
        r1 = await manager.reserve("model-a", 1_000_000_000)
        r2 = await manager.reserve("model-b", 2_000_000_000)
        assert manager.reserved_bytes == 3_000_000_000
        await manager.commit(r1)
        assert manager.allocated_bytes == 1_000_000_000
        assert manager.reserved_bytes == 2_000_000_000
        await manager.release(r1)
        await manager.release(r2)


# ---------------------------------------------------------------------------
# Double-release safety
# ---------------------------------------------------------------------------


class TestDoubleRelease:
    @pytest.mark.asyncio
    async def test_double_release_pending_is_safe(self, manager: VRAMManager) -> None:
        """Releasing a pending reservation twice should not corrupt state."""
        initial = manager.available_vram
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        await manager.release(reservation)
        # Second release: reservation already removed, should be a no-op
        await manager.release(reservation)
        assert manager.reserved_bytes == 0
        assert manager.allocated_bytes == 0
        assert manager.available_vram == initial

    @pytest.mark.asyncio
    async def test_double_release_committed_is_safe(self, manager: VRAMManager) -> None:
        """Releasing a committed reservation twice should not go negative."""
        initial = manager.available_vram
        reservation = await manager.reserve("test:7b", 1_000_000_000)
        await manager.commit(reservation)
        await manager.release(reservation)
        await manager.release(reservation)
        assert manager.allocated_bytes == 0
        assert manager.available_vram == initial

    @pytest.mark.asyncio
    async def test_commit_unknown_reservation_is_safe(self, manager: VRAMManager) -> None:
        """Committing an unknown reservation should not crash."""
        fake = VRAMReservation("fake-id", "test:7b", 1_000_000_000)
        await manager.commit(fake)  # Should log warning but not crash
        assert manager.allocated_bytes == 0


# ---------------------------------------------------------------------------
# Semaphore serialization
# ---------------------------------------------------------------------------


class TestSemaphoreSerialization:
    @pytest.mark.asyncio
    async def test_load_semaphore_serializes_access(self, manager: VRAMManager) -> None:
        """Only one caller should hold the load semaphore at a time."""
        acquired_order: list[int] = []

        async def acquire_and_record(index: int) -> None:
            async with manager._load_semaphore:
                acquired_order.append(index)
                await asyncio.sleep(0.02)

        await asyncio.gather(
            acquire_and_record(0),
            acquire_and_record(1),
            acquire_and_record(2),
        )
        # All three should have completed
        assert sorted(acquired_order) == [0, 1, 2]
        # Semaphore value should be 1 (released)
        assert manager._load_semaphore._value == 1

    @pytest.mark.asyncio
    async def test_semaphore_prevents_concurrent_access(self, manager: VRAMManager) -> None:
        """Verify that no two tasks hold the semaphore simultaneously."""
        max_concurrent = 0
        current_count = 0

        async def guarded_section(index: int) -> None:
            nonlocal max_concurrent, current_count
            async with manager._load_semaphore:
                current_count += 1
                max_concurrent = max(max_concurrent, current_count)
                await asyncio.sleep(0.01)
                current_count -= 1

        await asyncio.gather(*(guarded_section(i) for i in range(5)))
        assert max_concurrent == 1


# ---------------------------------------------------------------------------
# Expired reservation reclamation
# ---------------------------------------------------------------------------


class TestExpiredReclamation:
    @pytest.mark.asyncio
    async def test_expired_reservations_reclaimed_on_reserve(self, manager: VRAMManager) -> None:
        """Expired reservations should be reclaimed before checking available VRAM."""
        await manager.reserve("test:7b", 1_000_000_000, ttl=0.01)
        await asyncio.sleep(0.05)  # Let TTL expire
        # The next reserve should reclaim the expired one
        r2 = await manager.reserve("test:7b", 500_000_000)
        # Expired reservation freed, new one created
        assert manager.reserved_bytes == 500_000_000
        await manager.release(r2)

    def test_reservation_expired_property(self) -> None:
        r = VRAMReservation("test", "model", 1000, ttl=0.01)
        assert r.expired is False
        # Manually set created_at to the past
        r.created_at = time.monotonic() - 1.0
        assert r.expired is True

    def test_reservation_not_expired_within_ttl(self) -> None:
        r = VRAMReservation("test", "model", 1000, ttl=60.0)
        assert r.expired is False


# ---------------------------------------------------------------------------
# Convergence polling
# ---------------------------------------------------------------------------


class TestConvergencePolling:
    @pytest.mark.asyncio
    async def test_convergence_returns_true_when_stable(self, manager: VRAMManager) -> None:
        """wait_for_vram_convergence returns True when VRAM is stable."""
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=20.0)):
            result = await manager.wait_for_vram_convergence(timeout=1.0, interval=0.1)
        assert result is True

    @pytest.mark.asyncio
    async def test_convergence_returns_false_on_timeout(self, manager: VRAMManager) -> None:
        """wait_for_vram_convergence returns False when VRAM keeps changing."""
        call_count = 0

        def fluctuating_vram() -> float:
            nonlocal call_count
            call_count += 1
            return 20.0 + call_count  # Always changing

        with patch("bastion.vram.get_vram_free_gb", AsyncMock(side_effect=fluctuating_vram)):
            result = await manager.wait_for_vram_convergence(timeout=0.5, interval=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_convergence_handles_none_gracefully(self, manager: VRAMManager) -> None:
        """wait_for_vram_convergence handles nvidia-smi returning None."""
        with patch("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None)):
            result = await manager.wait_for_vram_convergence(timeout=0.5, interval=0.1)
        assert result is False


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


class TestVRAMManagerStatus:
    @pytest.mark.asyncio
    async def test_status_empty(self, manager: VRAMManager) -> None:
        status = manager.status()
        assert status["active_reservations"] == 0
        assert status["allocated_bytes"] == 0
        assert status["reserved_bytes"] == 0

    @pytest.mark.asyncio
    async def test_status_with_reservation(self, manager: VRAMManager) -> None:
        r = await manager.reserve("test:7b", 1_000_000_000)
        status = manager.status()
        assert status["active_reservations"] == 1
        assert status["reserved_bytes"] == 1_000_000_000
        assert len(status["reservations"]) == 1
        assert status["reservations"][0]["model"] == "test:7b"
        await manager.release(r)


# ---------------------------------------------------------------------------
# VRAM reserve under contention (D2)
# ---------------------------------------------------------------------------


class TestVRAMContention:
    @pytest.mark.asyncio
    async def test_concurrent_reserves_do_not_over_allocate(self, manager: VRAMManager) -> None:
        """Multiple concurrent reserves should not exceed available VRAM."""
        avail = manager.available_vram
        chunk = avail // 3

        reservations: list[VRAMReservation] = []
        errors: list[int] = []

        async def try_reserve(i: int) -> None:
            try:
                r = await manager.reserve(f"model-{i}", chunk)
                reservations.append(r)
            except ValueError:
                errors.append(i)

        await asyncio.gather(*(try_reserve(i) for i in range(5)))

        # At most 3 should succeed (3 * chunk <= avail)
        assert len(reservations) <= 3
        total_reserved = sum(r.vram_bytes for r in reservations)
        assert total_reserved <= avail

        # Clean up
        for r in reservations:
            await manager.release(r)
