"""Tests for the unified MachineSnapshot observability data model (T1-models).

Covers spec ``docs/design/specs/2026-06-19-observability-expansion.md`` Section 4
(4.1-4.8): the unified ``MachineSnapshot`` container plus sub-models, the eleven
new fast-path ``GPUStatus`` fields, the computed ``pcie_downgraded`` field, and
the ``ObservabilityConfig``/``CorrelationConfig`` config models with their
documented defaults.
"""

from __future__ import annotations

from bastion.models import (
    BlockDeviceIOStats,
    BrokerConfig,
    ContentionEvent,
    ContentionSnapshot,
    CorrelationConfig,
    CorrelationEvent,
    CorrelationState,
    GPUExtendedStatus,
    GPUStatus,
    InferenceThroughputState,
    MachineSnapshot,
    ObservabilityConfig,
    ProcessChurnEvent,
    ProcessGPURow,
    ProcessRow,
    ProcessSnapshot,
    RiskIndexResult,
    ThermalCoupling,
    XidEvent,
)

# ---------------------------------------------------------------------------
# GPUStatus fast-path extensions (4.2)
# ---------------------------------------------------------------------------

ELEVEN_NEW_FIELDS = (
    "compute_utilization_pct",
    "memory_bandwidth_utilization_pct",
    "sm_clock_mhz",
    "gr_clock_mhz",
    "mem_clock_mhz",
    "fan_speed_pct",
    "memory_junction_temp_c",
    "pcie_link_gen_current",
    "pcie_link_gen_max",
    "pcie_link_width_current",
    "pcie_link_width_max",
)


class TestGPUStatusExtensions:
    def test_eleven_new_fields_default_none(self):
        s = GPUStatus()
        for field in ELEVEN_NEW_FIELDS:
            assert getattr(s, field) is None, f"{field} should default to None"

    def test_gpu_index_defaults_zero(self):
        assert GPUStatus().gpu_index == 0

    def test_existing_fields_unchanged(self):
        # The pre-existing fast-path fields keep their None defaults.
        s = GPUStatus()
        assert s.temperature_c is None
        assert s.vram_used_mb is None
        assert s.power_draw_watts is None

    def test_new_fields_assignable(self):
        s = GPUStatus(compute_utilization_pct=73, sm_clock_mhz=2100, fan_speed_pct=87)
        assert s.compute_utilization_pct == 73
        assert s.sm_clock_mhz == 2100
        assert s.fan_speed_pct == 87

    # -- pcie_downgraded computed field ------------------------------------

    def test_pcie_downgraded_false_when_all_none(self):
        # Non-NVIDIA / StubBackend: all four link fields None -> never a false alarm.
        assert GPUStatus().pcie_downgraded is False

    def test_pcie_downgraded_false_at_full_link(self):
        s = GPUStatus(
            pcie_link_gen_current=4,
            pcie_link_gen_max=4,
            pcie_link_width_current=16,
            pcie_link_width_max=16,
        )
        assert s.pcie_downgraded is False

    def test_pcie_downgraded_true_on_gen_downgrade(self):
        s = GPUStatus(
            pcie_link_gen_current=1,
            pcie_link_gen_max=4,
            pcie_link_width_current=16,
            pcie_link_width_max=16,
        )
        assert s.pcie_downgraded is True

    def test_pcie_downgraded_true_on_width_downgrade(self):
        s = GPUStatus(
            pcie_link_gen_current=4,
            pcie_link_gen_max=4,
            pcie_link_width_current=8,
            pcie_link_width_max=16,
        )
        assert s.pcie_downgraded is True

    def test_pcie_downgraded_false_on_partial_data(self):
        # Partial-data guard: a downgrade is present but one field is None.
        s = GPUStatus(
            pcie_link_gen_current=1,
            pcie_link_gen_max=4,
            pcie_link_width_current=16,
            pcie_link_width_max=None,
        )
        assert s.pcie_downgraded is False

    def test_pcie_downgraded_serializes(self):
        # Computed field must be present in model_dump() (JSON round-trip).
        assert "pcie_downgraded" in GPUStatus().model_dump()


# ---------------------------------------------------------------------------
# Sub-models construct with defaults (4.3-4.7)
# ---------------------------------------------------------------------------

class TestSubModelDefaults:
    def test_gpu_extended_status(self):
        e = GPUExtendedStatus()
        assert e.throttle_reasons == []
        assert e.pcie_tx_kb_s is None
        assert e.pcie_rx_kb_s is None
        assert e.recent_xids == []
        assert e.xid_count_since_start == 0
        assert e.last_polled_at == 0.0

    def test_xid_event(self):
        x = XidEvent(timestamp="2026-06-19T00:00:00", xid_code=79, raw_message="fell off the bus")
        assert x.xid_code == 79

    def test_contention_snapshot(self):
        c = ContentionSnapshot()
        assert c.psi_cpu_some_avg10 is None
        assert c.psi_io_full_avg10 is None
        assert c.swap_in_rate_mb_s is None
        assert c.block_devices == []
        assert c.cpu_package_watts is None
        assert c.gpu_board_watts is None
        assert c.oom_kill_total is None
        assert c.oom_kill_rate is None
        assert isinstance(c.sampled_at, float)

    def test_block_device_io_stats(self):
        b = BlockDeviceIOStats(
            device="nvme0n1", util_pct=12.5, read_rate_mb_s=100.0, write_rate_mb_s=50.0
        )
        assert b.device == "nvme0n1"
        assert b.read_await_ms is None
        assert b.write_await_ms is None

    def test_process_snapshot(self):
        p = ProcessSnapshot(collected_at=1.0)
        assert p.top_processes == []
        assert p.gpu_processes == []
        assert p.own_pids == {}
        assert p.watchlist_hits == []
        assert p.recent_churn_events == []
        assert p.gpu_collected_at is None

    def test_process_row(self):
        r = ProcessRow(pid=123, name="ollama")
        assert r.cpu_pct is None
        assert r.is_inference_owned is False
        assert r.role is None
        assert r.watchlisted is False
        assert r.gpu_row is None

    def test_process_gpu_row(self):
        g = ProcessGPURow(pid=123, name="ollama")
        assert g.vram_mb is None
        assert g.sm_pct is None
        assert g.is_inference_owned is False

    def test_process_churn_event(self):
        e = ProcessChurnEvent(timestamp=1.0, new_count=2, exited_count=1, new_names=["x"])
        assert e.new_names == ["x"]

    def test_inference_throughput_state(self):
        i = InferenceThroughputState()
        assert i.decode_tps_p50 is None
        assert i.prefill_tps_p50 is None
        assert i.ttft_p50_s is None
        assert i.ctx_utilization_p50 is None
        assert i.last_model is None
        assert isinstance(i.sampled_at, float)

    def test_correlation_state(self):
        c = CorrelationState()
        assert c.risk_index is None
        assert c.thermal_coupling is None
        assert c.recent_contentions == []
        assert c.enriched_stall_reason is None
        assert c.ring_size == 0
        assert c.recent_ring_events == []

    def test_risk_index_result(self):
        r = RiskIndexResult(
            score=0.42, level="elevated", component_scores={"vram": 0.5}, dominant_factor="vram"
        )
        assert r.score == 0.42
        assert r.level == "elevated"

    def test_thermal_coupling(self):
        t = ThermalCoupling()
        assert t.cpu_temp_c is None
        assert t.gpu_temp_c is None
        assert t.fan_speed_pct is None
        assert t.coupling_active is False
        assert t.thermal_headroom_min_c is None

    def test_correlation_event(self):
        e = CorrelationEvent(ts_monotonic=1.0, ts_wall=2.0, domain="gpu", kind="xid", payload={})
        assert e.domain == "gpu"

    def test_contention_event_extends_correlation_event(self):
        e = ContentionEvent(
            ts_monotonic=1.0, ts_wall=2.0, domain="system", kind="io", payload={},
            attribution="NVMe burst",
        )
        assert e.attribution == "NVMe burst"
        assert e.inference_was_stalled is False
        assert e.stall_reason_at_time is None
        assert e.latency_spike_ratio is None
        # ContentionEvent must carry the base CorrelationEvent fields.
        assert e.domain == "system"


# ---------------------------------------------------------------------------
# MachineSnapshot top-level container (4.1)
# ---------------------------------------------------------------------------

class TestMachineSnapshot:
    def test_minimal_construction(self):
        # Only the two required fields; everything else None-default.
        m = MachineSnapshot(snapshot_ts=1.0)
        assert m.snapshot_ts == 1.0
        assert isinstance(m.gpu, GPUStatus)
        assert m.gpu_extended is None
        assert m.contention is None
        assert m.process is None
        assert m.inference is None
        assert m.correlation is None

    def test_full_construction_with_all_submodels(self):
        m = MachineSnapshot(
            snapshot_ts=1.0,
            gpu=GPUStatus(),
            gpu_extended=GPUExtendedStatus(),
            contention=ContentionSnapshot(),
            process=ProcessSnapshot(collected_at=1.0),
            inference=InferenceThroughputState(),
            correlation=CorrelationState(),
        )
        assert m.gpu_extended is not None
        assert m.contention is not None
        assert m.process is not None
        assert m.inference is not None
        assert m.correlation is not None

    def test_round_trips_through_model_dump(self):
        m = MachineSnapshot(snapshot_ts=1.0)
        dumped = m.model_dump()
        assert dumped["snapshot_ts"] == 1.0
        # gpu nested dump includes the computed pcie_downgraded field.
        assert dumped["gpu"]["pcie_downgraded"] is False
        # Re-validates from its own dump.
        again = MachineSnapshot.model_validate(dumped)
        assert again.snapshot_ts == 1.0


# ---------------------------------------------------------------------------
# ObservabilityConfig / CorrelationConfig defaults (4.8)
# ---------------------------------------------------------------------------

class TestObservabilityConfig:
    def test_defaults_match_spec(self):
        o = ObservabilityConfig()
        assert o.process_watchlist == []
        assert o.churn_threshold == 5
        assert o.ecc_enabled is False
        assert o.cpu_sensor_name is None
        assert o.rapl_domain_path is None
        assert o.storage_device_filter is None
        assert o.disk_mount_labels is None
        assert o.psi_io_full_warn_pct == 5.0
        assert o.psi_io_full_crit_pct == 25.0

    def test_nested_correlation_default_factory(self):
        o = ObservabilityConfig()
        assert isinstance(o.correlation, CorrelationConfig)

    def test_present_block_parses(self):
        o = ObservabilityConfig(
            process_watchlist=["python", "pid:42"],
            churn_threshold=10,
            ecc_enabled=True,
            cpu_sensor_name="zenpower",
            storage_device_filter=["nvme0n1", "sda"],
            psi_io_full_crit_pct=40.0,
        )
        assert o.process_watchlist == ["python", "pid:42"]
        assert o.churn_threshold == 10
        assert o.ecc_enabled is True
        assert o.cpu_sensor_name == "zenpower"
        assert o.storage_device_filter == ["nvme0n1", "sda"]
        assert o.psi_io_full_crit_pct == 40.0


class TestCorrelationConfig:
    def test_defaults_match_spec(self):
        c = CorrelationConfig()
        assert c.ring_maxlen == 512
        assert c.ring_tail_in_snapshot == 32
        assert c.contention_block_write_mb_s_threshold == 200.0
        assert c.contention_psi_threshold == 20.0
        assert c.contention_cpu_psi_threshold == 60.0
        assert c.contention_hysteresis_ticks == 2
        assert c.cpu_safe_ceiling_c == 85.0
        assert c.gpu_safe_ceiling_c is None
        # risk_weights documented in 6.4: five components summing to 1.0.
        assert c.risk_weights == {
            "vram_headroom": 0.25,
            "thermal_headroom": 0.20,
            "swap_rate": 0.25,
            "thrashing": 0.20,
            "memory_psi": 0.10,
        }
        assert abs(sum(c.risk_weights.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# BrokerConfig wiring (4.8 — observability optional with default factory)
# ---------------------------------------------------------------------------

class TestBrokerConfigObservability:
    def test_observability_default_factory(self):
        c = BrokerConfig()
        assert isinstance(c.observability, ObservabilityConfig)
        assert isinstance(c.observability.correlation, CorrelationConfig)

    def test_present_observability_block_not_ignored(self):
        c = BrokerConfig(observability={"churn_threshold": 9})
        assert c.observability.churn_threshold == 9


# ---------------------------------------------------------------------------
# Docstring portability fix (Section 6.4 — models.py:211)
# ---------------------------------------------------------------------------

class TestThrashingDocstringPortability:
    def test_docstring_is_portability_aware(self):
        from bastion.models import ThrashingDetectionConfig

        doc = ThrashingDetectionConfig.__doc__ or ""
        # Whitespace-normalize so line-wrapping inside the docstring does not
        # break substring checks of the spec's required wording.
        flat = " ".join(doc.split())
        # Old wording removed; new portability-aware wording present.
        assert "Thresholds derived from RTX 5090 crash data." not in flat
        assert "consumer-GPU crash forensics" in flat
        assert "halt_swap_ratio" in flat
