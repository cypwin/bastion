"""T5 — VRAM reconcile counters + ledger-drift gauge (spec Section 5.4 row 431).

Three NET-NEW Prometheus objects (not Tier-0 activations):

  - ``VRAM_RECONCILE_STALE_TOTAL`` — label-less Counter
    (``bastion_vram_reconcile_stale_total``); incremented in ``reconcile()`` at
    the stale-removal site.
  - ``VRAM_RECONCILE_IMPORT_TOTAL`` — label-less Counter
    (``bastion_vram_reconcile_import_total``); incremented in ``reconcile()`` at
    the import site.
  - ``VRAM_LEDGER_DRIFT_MB`` — Gauge ``labelnames=['gpu_index']``
    (``bastion_vram_ledger_drift_mb``) = measured − (allocated+reserved),
    emitted on the SLOW snapshot tick. SKIPPED (never published as 0) when the
    backend returns ``None`` for measured VRAM (StubBackend / non-NVIDIA).

The reconcile counters MUST be defined in ``metrics.py`` *before* being wired
in ``vram.py`` — calling non-existent helpers would ``AttributeError`` (spec
risk note). When ``prometheus_client`` is absent the helpers are no-ops, so the
value-introspection assertions are guarded behind ``PROMETHEUS_AVAILABLE`` and
the wiring assertions fall back to spying the helper.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
)
from bastion.vram import VRAMManager, VRAMTracker

GB = 1024 ** 3


def _lm(name: str, vram_gb: float) -> LoadedModel:
    return LoadedModel(name=name, size_bytes=int(vram_gb * GB), vram_gb=vram_gb, details={})


@pytest.fixture
def config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        models={
            "tracked:7b": ModelInfo(vram_gb=5.0),
            "ext:13b": ModelInfo(vram_gb=9.0),
            "embed:v1": ModelInfo(vram_gb=7.0, always_allowed=True),
        },
    )


@pytest.fixture
def manager(config: BrokerConfig) -> VRAMManager:
    return VRAMManager(VRAMTracker(config), 32 * GB, safety_margin_pct=10.0)


# ---------------------------------------------------------------------------
# metrics.py — objects + helpers exist (defined BEFORE wiring)
# ---------------------------------------------------------------------------


class TestMetricObjectsExist:
    def test_counters_and_gauge_are_importable(self):
        from bastion.metrics import (
            VRAM_LEDGER_DRIFT_MB,
            VRAM_RECONCILE_IMPORT_TOTAL,
            VRAM_RECONCILE_STALE_TOTAL,
        )

        assert VRAM_RECONCILE_STALE_TOTAL is not None
        assert VRAM_RECONCILE_IMPORT_TOTAL is not None
        assert VRAM_LEDGER_DRIFT_MB is not None

    def test_helpers_are_importable(self):
        from bastion.metrics import (
            record_vram_reconcile_import,
            record_vram_reconcile_stale,
            update_vram_ledger_drift,
        )

        # No-op stubs accept these calls too; must not raise.
        record_vram_reconcile_stale()
        record_vram_reconcile_stale(2)
        record_vram_reconcile_import()
        record_vram_reconcile_import(3)
        update_vram_ledger_drift(gpu_index="0", mb=-128.0)

    def test_exported_in_all(self):
        from bastion import metrics

        for name in (
            "VRAM_RECONCILE_STALE_TOTAL",
            "VRAM_RECONCILE_IMPORT_TOTAL",
            "VRAM_LEDGER_DRIFT_MB",
            "record_vram_reconcile_stale",
            "record_vram_reconcile_import",
            "update_vram_ledger_drift",
        ):
            assert name in metrics.__all__

    def test_drift_gauge_has_single_gpu_index_label(self):
        from bastion.metrics import PROMETHEUS_AVAILABLE, VRAM_LEDGER_DRIFT_MB

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op gauge")
        assert VRAM_LEDGER_DRIFT_MB._labelnames == ("gpu_index",)

    def test_counters_are_label_less(self):
        from bastion.metrics import (
            PROMETHEUS_AVAILABLE,
            VRAM_RECONCILE_IMPORT_TOTAL,
            VRAM_RECONCILE_STALE_TOTAL,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op counter")
        assert VRAM_RECONCILE_STALE_TOTAL._labelnames == ()
        assert VRAM_RECONCILE_IMPORT_TOTAL._labelnames == ()


class TestHelperValues:
    def test_stale_counter_increments(self):
        from bastion.metrics import (
            PROMETHEUS_AVAILABLE,
            VRAM_RECONCILE_STALE_TOTAL,
            record_vram_reconcile_stale,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op counter")
        before = VRAM_RECONCILE_STALE_TOTAL._value.get()
        record_vram_reconcile_stale(2)
        assert VRAM_RECONCILE_STALE_TOTAL._value.get() == before + 2

    def test_import_counter_increments(self):
        from bastion.metrics import (
            PROMETHEUS_AVAILABLE,
            VRAM_RECONCILE_IMPORT_TOTAL,
            record_vram_reconcile_import,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op counter")
        before = VRAM_RECONCILE_IMPORT_TOTAL._value.get()
        record_vram_reconcile_import()
        assert VRAM_RECONCILE_IMPORT_TOTAL._value.get() == before + 1

    def test_drift_gauge_sets_signed_value(self):
        from bastion.metrics import (
            PROMETHEUS_AVAILABLE,
            VRAM_LEDGER_DRIFT_MB,
            update_vram_ledger_drift,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed — no-op gauge")
        update_vram_ledger_drift(gpu_index="0", mb=-256.0)
        assert VRAM_LEDGER_DRIFT_MB.labels(gpu_index="0")._value.get() == -256.0


# ---------------------------------------------------------------------------
# vram.py reconcile() wiring
# ---------------------------------------------------------------------------


class TestReconcileCounterWiring:
    @pytest.mark.asyncio
    async def test_stale_removal_increments_stale_counter(self, manager):
        # tracked:7b committed, then no longer resident -> stale removal.
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        with patch("bastion.vram.record_vram_reconcile_stale") as stale_spy, patch(
            "bastion.vram.record_vram_reconcile_import"
        ) as import_spy:
            await manager.reconcile(set())
        stale_spy.assert_called_once()
        import_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_increments_import_counter(self, manager):
        # ext:13b resident but untracked -> import.
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("ext:13b", 9.0)]
        )
        with patch("bastion.vram.record_vram_reconcile_stale") as stale_spy, patch(
            "bastion.vram.record_vram_reconcile_import"
        ) as import_spy:
            await manager.reconcile({"ext:13b"})
        import_spy.assert_called_once()
        stale_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_and_import_both_increment(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("ext:13b", 9.0)]
        )
        with patch("bastion.vram.record_vram_reconcile_stale") as stale_spy, patch(
            "bastion.vram.record_vram_reconcile_import"
        ) as import_spy:
            await manager.reconcile({"ext:13b"})
        stale_spy.assert_called_once()
        import_spy.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_none_increments_nothing(self, manager):
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        with patch("bastion.vram.record_vram_reconcile_stale") as stale_spy, patch(
            "bastion.vram.record_vram_reconcile_import"
        ) as import_spy:
            await manager.reconcile(None)
        stale_spy.assert_not_called()
        import_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_change_increments_nothing(self, manager):
        # tracked:7b stays resident and already tracked -> no stale, no import.
        res = await manager.reserve("tracked:7b", 5 * GB)
        await manager.commit(res)
        manager._tracker.residency_cache.get_resident_loaded_models = AsyncMock(
            return_value=[_lm("tracked:7b", 5.0)]
        )
        with patch("bastion.vram.record_vram_reconcile_stale") as stale_spy, patch(
            "bastion.vram.record_vram_reconcile_import"
        ) as import_spy:
            await manager.reconcile({"tracked:7b"})
        stale_spy.assert_not_called()
        import_spy.assert_not_called()


# ---------------------------------------------------------------------------
# server.py — drift gauge on the SLOW tick
# ---------------------------------------------------------------------------


class TestDriftGaugeOnSlowTick:
    @pytest.mark.asyncio
    async def test_drift_emitted_when_measured_present(self):
        """Slow tick + measured VRAM in hand -> drift gauge set (signed)."""
        from bastion import server

        gpu = GPUStatus(gpu_index=0, vram_used_mb=8192)
        fake_mgr = AsyncMock()
        # allocated 5 GB + reserved 1 GB = 6 GB tracked = 6144 MB
        fake_mgr.status = AsyncMock(
            return_value={"allocated_bytes": 5 * GB, "reserved_bytes": 1 * GB}
        )

        with patch.object(
            server, "query_gpu_status", AsyncMock(return_value=gpu)
        ), patch.object(
            server, "_collect_broker_status_lite", AsyncMock(return_value=None)
        ), patch.object(
            server, "_collect_contention", AsyncMock(return_value=None)
        ), patch.object(
            server, "_vram_manager", fake_mgr
        ), patch.object(
            server, "update_vram_ledger_drift"
        ) as spy:
            await server._collect_machine_snapshot(tick=0)  # tick%5==0 → slow

        spy.assert_called_once()
        _args, kwargs = spy.call_args
        # measured 8192 − tracked 6144 = +2048
        assert float(kwargs.get("mb", _args[1] if len(_args) > 1 else None)) == 2048.0
        assert str(kwargs.get("gpu_index", _args[0] if _args else None)) == "0"

    @pytest.mark.asyncio
    async def test_drift_skipped_when_measured_none(self):
        """StubBackend / non-NVIDIA: measured None -> gauge NOT set (no 0)."""
        from bastion import server

        gpu = GPUStatus(gpu_index=0, vram_used_mb=None)  # StubBackend
        fake_mgr = AsyncMock()
        fake_mgr.status = AsyncMock(
            return_value={"allocated_bytes": 5 * GB, "reserved_bytes": 0}
        )

        with patch.object(
            server, "query_gpu_status", AsyncMock(return_value=gpu)
        ), patch.object(
            server, "_collect_broker_status_lite", AsyncMock(return_value=None)
        ), patch.object(
            server, "_collect_contention", AsyncMock(return_value=None)
        ), patch.object(
            server, "_vram_manager", fake_mgr
        ), patch.object(
            server, "update_vram_ledger_drift"
        ) as spy:
            await server._collect_machine_snapshot(tick=0)

        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_skipped_on_fast_tick(self):
        """Drift is a slow-tick (10s) signal; a non-slow tick must not emit it."""
        from bastion import server

        gpu = GPUStatus(gpu_index=0, vram_used_mb=8192)
        fake_mgr = AsyncMock()
        fake_mgr.status = AsyncMock(
            return_value={"allocated_bytes": 5 * GB, "reserved_bytes": 0}
        )

        with patch.object(
            server, "query_gpu_status", AsyncMock(return_value=gpu)
        ), patch.object(
            server, "_collect_broker_status_lite", AsyncMock(return_value=None)
        ), patch.object(
            server, "_collect_contention", AsyncMock(return_value=None)
        ), patch.object(
            server, "_vram_manager", fake_mgr
        ), patch.object(
            server, "update_vram_ledger_drift"
        ) as spy:
            await server._collect_machine_snapshot(tick=1)  # tick%5!=0 → fast

        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_skipped_when_no_vram_manager(self):
        """No VRAMManager configured -> nothing to compare; skip, never 0."""
        from bastion import server

        gpu = GPUStatus(gpu_index=0, vram_used_mb=8192)

        with patch.object(
            server, "query_gpu_status", AsyncMock(return_value=gpu)
        ), patch.object(
            server, "_collect_broker_status_lite", AsyncMock(return_value=None)
        ), patch.object(
            server, "_collect_contention", AsyncMock(return_value=None)
        ), patch.object(
            server, "_vram_manager", None
        ), patch.object(
            server, "update_vram_ledger_drift"
        ) as spy:
            await server._collect_machine_snapshot(tick=0)

        spy.assert_not_called()
