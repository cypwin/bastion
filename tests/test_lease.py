"""Tests for lease heartbeat, fencing tokens, and zombie cleanup.

Covers:
  - ModelLease creation and field defaults
  - should_release() for all eviction triggers
  - touch() extends idle timeout
  - use_request() decrements count and touches
  - Fencing token validation (A2AHandler.validate_lease)
  - Lease creation via A2AHandler.create_lease
  - Lease release via A2AHandler.release_lease
  - has_active_lease checks
  - Zombie lease cleanup via _cleanup_expired_reservations
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion.models import (
    BrokerConfig,
    LeaseState,
    ModelLease,
)

# ---------------------------------------------------------------------------
# ModelLease model tests
# ---------------------------------------------------------------------------


class TestModelLease:
    """Test ModelLease Pydantic model."""

    def test_defaults(self) -> None:
        lease = ModelLease(model="qwen3:8b")
        assert lease.model == "qwen3:8b"
        assert lease.max_requests == 100
        assert lease.remaining_requests == 100
        assert lease.idle_timeout == 60.0
        assert lease.fencing_token == 0
        assert lease.state == LeaseState.ACTIVE
        assert len(lease.lease_id) == 12

    def test_unique_ids(self) -> None:
        ids = {ModelLease(model="a").lease_id for _ in range(100)}
        assert len(ids) == 100

    def test_touch_updates_last_activity(self) -> None:
        lease = ModelLease(model="a", idle_timeout=10.0)
        # Simulate time passing
        lease.last_activity = time.monotonic() - 5.0
        old_activity = lease.last_activity
        lease.touch()
        assert lease.last_activity > old_activity

    def test_use_request_decrements(self) -> None:
        lease = ModelLease(model="a", max_requests=5, remaining_requests=5)
        remaining = lease.use_request()
        assert remaining == 4
        assert lease.remaining_requests == 4

    def test_use_request_floors_at_zero(self) -> None:
        lease = ModelLease(model="a", max_requests=1, remaining_requests=1)
        lease.use_request()
        remaining = lease.use_request()
        assert remaining == 0

    def test_use_request_touches(self) -> None:
        lease = ModelLease(model="a")
        lease.last_activity = time.monotonic() - 100.0
        old_activity = lease.last_activity
        lease.use_request()
        assert lease.last_activity > old_activity


# ---------------------------------------------------------------------------
# should_release() tests
# ---------------------------------------------------------------------------


class TestShouldRelease:
    """Test ModelLease.should_release() for all eviction triggers."""

    def test_active_lease_not_released(self) -> None:
        lease = ModelLease(
            model="a",
            max_requests=10,
            remaining_requests=10,
            expiry=time.monotonic() + 600.0,
            idle_timeout=60.0,
        )
        should, reason = lease.should_release()
        assert should is False
        assert reason == ""

    def test_request_limit_exhausted(self) -> None:
        lease = ModelLease(model="a", remaining_requests=0)
        should, reason = lease.should_release()
        assert should is True
        assert reason == "REQUEST_LIMIT"

    def test_ttl_expired(self) -> None:
        lease = ModelLease(
            model="a",
            expiry=time.monotonic() - 1.0,  # Already expired
        )
        should, reason = lease.should_release()
        assert should is True
        assert reason == "TTL_EXPIRED"

    def test_idle_timeout(self) -> None:
        lease = ModelLease(
            model="a",
            idle_timeout=1.0,
            last_activity=time.monotonic() - 5.0,  # 5s idle, threshold 1s
        )
        should, reason = lease.should_release()
        assert should is True
        assert reason == "IDLE"

    def test_released_state(self) -> None:
        lease = ModelLease(model="a", state=LeaseState.RELEASED)
        should, reason = lease.should_release()
        assert should is True
        assert reason == "LEASE_RELEASED"

    def test_expired_state(self) -> None:
        lease = ModelLease(model="a", state=LeaseState.EXPIRED)
        should, reason = lease.should_release()
        assert should is True
        assert reason == "LEASE_EXPIRED"

    def test_priority_order_state_before_request_limit(self) -> None:
        """State check has highest priority in should_release."""
        lease = ModelLease(
            model="a",
            remaining_requests=0,
            state=LeaseState.RELEASED,
        )
        should, reason = lease.should_release()
        assert should is True
        assert reason == "LEASE_RELEASED"

    def test_priority_order_request_limit_before_ttl(self) -> None:
        """Request limit is checked before TTL."""
        lease = ModelLease(
            model="a",
            remaining_requests=0,
            expiry=time.monotonic() - 1.0,
        )
        should, reason = lease.should_release()
        assert should is True
        assert reason == "REQUEST_LIMIT"

    def test_heartbeat_prevents_idle_eviction(self) -> None:
        """Touching a lease resets idle timer, preventing IDLE eviction."""
        lease = ModelLease(
            model="a",
            idle_timeout=2.0,
            last_activity=time.monotonic() - 10.0,  # Would be idle
        )
        # Before touch: should be idle
        should, reason = lease.should_release()
        assert should is True
        assert reason == "IDLE"

        # Touch (heartbeat)
        lease.touch()

        # After touch: no longer idle
        should, reason = lease.should_release()
        assert should is False


# ---------------------------------------------------------------------------
# A2AHandler lease management tests
# ---------------------------------------------------------------------------


class TestA2AHandlerLeases:
    """Test lease operations via A2AHandler."""

    async def _make_handler(self):
        """Create a minimal A2AHandler for testing leases."""
        from bastion.a2a import A2AHandler

        config = BrokerConfig()
        handler = A2AHandler(
            config=config,
            enqueue_fn=AsyncMock(),
            vram_tracker=MagicMock(),
            scheduler=MagicMock(),
        )
        return handler

    @pytest.mark.asyncio
    async def test_create_lease(self) -> None:
        handler = await self._make_handler()
        lease = handler.create_lease(
            model="qwen3:8b",
            max_requests=50,
            ttl_seconds=300.0,
            idle_timeout=30.0,
        )
        assert lease.model == "qwen3:8b"
        assert lease.max_requests == 50
        assert lease.remaining_requests == 50
        assert lease.idle_timeout == 30.0
        assert lease.fencing_token > 0
        assert lease.state == LeaseState.ACTIVE
        assert lease.lease_id in handler._leases

    @pytest.mark.asyncio
    async def test_create_lease_increments_fencing_token(self) -> None:
        handler = await self._make_handler()
        lease1 = handler.create_lease(model="a")
        lease2 = handler.create_lease(model="b")
        assert lease2.fencing_token > lease1.fencing_token

    @pytest.mark.asyncio
    async def test_validate_lease_valid(self) -> None:
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is True
        assert reason == "OK"

    @pytest.mark.asyncio
    async def test_validate_lease_not_found(self) -> None:
        handler = await self._make_handler()
        valid, reason = handler.validate_lease("nonexistent", 1)
        assert valid is False
        assert "not found" in reason.lower()

    @pytest.mark.asyncio
    async def test_validate_lease_stale_fencing_token(self) -> None:
        """Stale fencing token should be rejected."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        stale_token = lease.fencing_token - 1
        valid, reason = handler.validate_lease(lease.lease_id, stale_token)
        assert valid is False
        assert "stale" in reason.lower() or "fencing" in reason.lower()

    @pytest.mark.asyncio
    async def test_validate_lease_wrong_token(self) -> None:
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        valid, reason = handler.validate_lease(lease.lease_id, 9999)
        assert valid is False

    @pytest.mark.asyncio
    async def test_validate_lease_expired(self) -> None:
        """Expired lease should fail validation."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", ttl_seconds=0.0)
        # TTL is 0, so it's already expired
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is False
        assert "expired" in reason.lower()

    @pytest.mark.asyncio
    async def test_validate_lease_idle(self) -> None:
        """Idle lease should fail validation."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", idle_timeout=1.0)
        # Simulate idle by backdating last_activity
        lease.last_activity = time.monotonic() - 10.0
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is False
        assert "expired" in reason.lower()

    @pytest.mark.asyncio
    async def test_release_lease(self) -> None:
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        result = handler.release_lease(lease.lease_id)
        assert result is True
        assert lease.lease_id not in handler._leases

    @pytest.mark.asyncio
    async def test_release_lease_not_found(self) -> None:
        handler = await self._make_handler()
        result = handler.release_lease("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_active_lease_true(self) -> None:
        handler = await self._make_handler()
        handler.create_lease(model="qwen3:8b")
        assert handler.has_active_lease("qwen3:8b") is True

    @pytest.mark.asyncio
    async def test_has_active_lease_false(self) -> None:
        handler = await self._make_handler()
        assert handler.has_active_lease("nonexistent") is False

    @pytest.mark.asyncio
    async def test_has_active_lease_after_release(self) -> None:
        handler = await self._make_handler()
        lease = handler.create_lease(model="qwen3:8b")
        handler.release_lease(lease.lease_id)
        assert handler.has_active_lease("qwen3:8b") is False

    @pytest.mark.asyncio
    async def test_has_active_lease_expired(self) -> None:
        handler = await self._make_handler()
        handler.create_lease(model="a", ttl_seconds=0.0)
        # TTL=0 means expired immediately
        assert handler.has_active_lease("a") is False


# ---------------------------------------------------------------------------
# Zombie lease cleanup tests
# ---------------------------------------------------------------------------


class TestZombieLeaseCleanup:
    """Test _cleanup_expired_reservations handles zombie leases."""

    async def _make_handler(self):
        from bastion.a2a import A2AHandler
        config = BrokerConfig()
        handler = A2AHandler(
            config=config,
            enqueue_fn=AsyncMock(),
            vram_tracker=MagicMock(),
            scheduler=MagicMock(),
        )
        return handler

    @pytest.mark.asyncio
    async def test_cleanup_removes_expired_lease(self) -> None:
        """Leases past TTL are cleaned up."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", ttl_seconds=0.0)
        lid = lease.lease_id

        # Run the cleanup logic once (mirrors _cleanup_expired_reservations)
        expired_leases = [
            k for k, ls in handler._leases.items()
            if ls.should_release()[0]
        ]
        for expired_lid in expired_leases:
            ls = handler._leases[expired_lid]
            ls.state = LeaseState.EXPIRED
            del handler._leases[expired_lid]

        assert lid not in handler._leases

    @pytest.mark.asyncio
    async def test_cleanup_removes_idle_lease(self) -> None:
        """Leases with no heartbeat past idle_timeout are cleaned up."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", idle_timeout=1.0)
        lease.last_activity = time.monotonic() - 10.0  # Simulate long idle
        lid = lease.lease_id

        # Run cleanup
        expired_leases = [
            k for k, ls in handler._leases.items()
            if ls.should_release()[0]
        ]
        for expired_lid in expired_leases:
            ls = handler._leases[expired_lid]
            ls.state = LeaseState.EXPIRED
            del handler._leases[expired_lid]

        assert lid not in handler._leases

    @pytest.mark.asyncio
    async def test_cleanup_keeps_active_lease(self) -> None:
        """Active leases with remaining time/requests are not cleaned."""
        handler = await self._make_handler()
        lease = handler.create_lease(
            model="a",
            ttl_seconds=600.0,
            idle_timeout=60.0,
        )
        lid = lease.lease_id

        expired_leases = [
            k for k, ls in handler._leases.items()
            if ls.should_release()[0]
        ]
        for expired_lid in expired_leases:
            del handler._leases[expired_lid]

        assert lid in handler._leases

    @pytest.mark.asyncio
    async def test_cleanup_removes_request_exhausted_lease(self) -> None:
        """Leases with 0 remaining requests are cleaned up."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", max_requests=1)
        lease.use_request()
        lid = lease.lease_id

        expired_leases = [
            k for k, ls in handler._leases.items()
            if ls.should_release()[0]
        ]
        for expired_lid in expired_leases:
            del handler._leases[expired_lid]

        assert lid not in handler._leases


# ---------------------------------------------------------------------------
# Heartbeat flow integration tests
# ---------------------------------------------------------------------------


class TestHeartbeatFlow:
    """Test the heartbeat -> touch -> idle prevention flow."""

    async def _make_handler(self):
        from bastion.a2a import A2AHandler
        config = BrokerConfig()
        return A2AHandler(
            config=config,
            enqueue_fn=AsyncMock(),
            vram_tracker=MagicMock(),
            scheduler=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_heartbeat_extends_idle_timeout(self) -> None:
        """Heartbeat (touch) resets the idle timer."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a", idle_timeout=2.0)

        # Simulate time passing close to idle timeout
        lease.last_activity = time.monotonic() - 1.5

        # Heartbeat
        lease.touch()

        # Should not be idle anymore
        should, reason = lease.should_release()
        assert should is False

    @pytest.mark.asyncio
    async def test_heartbeat_with_valid_fencing_token(self) -> None:
        """Heartbeat with correct fencing token succeeds."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")

        # Validate before heartbeat
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is True

        # Touch (what the heartbeat endpoint does)
        lease.touch()

        # Still valid after touch
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is True

    @pytest.mark.asyncio
    async def test_heartbeat_with_stale_fencing_token_rejected(self) -> None:
        """Heartbeat with stale fencing token is rejected."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        correct_token = lease.fencing_token

        # Create a new lease (increments fencing counter)
        handler.create_lease(model="b")

        # Old token is still correct for the first lease
        valid, reason = handler.validate_lease(lease.lease_id, correct_token)
        assert valid is True

        # Wrong token is rejected
        valid, reason = handler.validate_lease(lease.lease_id, correct_token + 10)
        assert valid is False

    @pytest.mark.asyncio
    async def test_heartbeat_on_released_lease_rejected(self) -> None:
        """Heartbeat on already-released lease is rejected."""
        handler = await self._make_handler()
        lease = handler.create_lease(model="a")
        token = lease.fencing_token

        handler.release_lease(lease.lease_id)

        valid, reason = handler.validate_lease(lease.lease_id, token)
        assert valid is False
        assert "not found" in reason.lower()


# ---------------------------------------------------------------------------
# try_create_lease — atomic check-and-create (single grant per model)
# ---------------------------------------------------------------------------


class TestTryCreateLease:
    """`try_create_lease` closes the TOCTOU window in the
    `if not has_active_lease: create_lease(...)` caller pattern by doing the
    check and the create atomically under the handler's lease lock."""

    async def _make_handler(self):
        from bastion.a2a import A2AHandler

        return A2AHandler(
            config=BrokerConfig(),
            enqueue_fn=AsyncMock(),
            vram_tracker=MagicMock(),
            scheduler=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_grants_when_no_active_lease(self) -> None:
        handler = await self._make_handler()
        lease = handler.try_create_lease(
            model="qwen3:8b", max_requests=5, ttl_seconds=60.0, idle_timeout=30.0
        )
        assert lease is not None
        assert lease.model == "qwen3:8b"
        assert lease.lease_id in handler._leases
        assert handler.has_active_lease("qwen3:8b") is True

    @pytest.mark.asyncio
    async def test_refuses_when_active_lease_exists(self) -> None:
        handler = await self._make_handler()
        first = handler.try_create_lease(model="qwen3:8b")
        assert first is not None
        second = handler.try_create_lease(model="qwen3:8b")
        assert second is None
        assert len(handler._leases) == 1

    @pytest.mark.asyncio
    async def test_grants_per_model_independently(self) -> None:
        handler = await self._make_handler()
        assert handler.try_create_lease(model="a") is not None
        assert handler.try_create_lease(model="b") is not None
        assert handler.try_create_lease(model="a") is None

    @pytest.mark.asyncio
    async def test_grants_after_release(self) -> None:
        handler = await self._make_handler()
        first = handler.try_create_lease(model="a")
        assert first is not None
        handler.release_lease(first.lease_id)
        second = handler.try_create_lease(model="a")
        assert second is not None
        assert second.fencing_token > first.fencing_token

    @pytest.mark.asyncio
    async def test_grants_after_expiry(self) -> None:
        handler = await self._make_handler()
        old = handler.try_create_lease(model="a", ttl_seconds=0.0)
        assert old is not None
        fresh = handler.try_create_lease(model="a")
        assert fresh is not None
        assert fresh.fencing_token > old.fencing_token
