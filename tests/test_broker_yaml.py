"""Validate the shipped config template documents the swap-brake blocks (D2).

Parses ``config/broker.example.yaml`` (the tracked template) directly into
``BrokerConfig`` — bypassing ``load_config``'s GPU auto-detection and calibrated
profile application so the assertions are deterministic in CI.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bastion.models import BrokerConfig, ModelInfo, PinDetectionConfig, SwapBrakeConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "broker.example.yaml"


def _load_example() -> BrokerConfig:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
    if isinstance(raw.get("models"), dict):
        raw["models"] = {
            n: ModelInfo(**i) if isinstance(i, dict) else i
            for n, i in raw["models"].items()
        }
    return BrokerConfig(**raw)


def test_example_documents_swap_brake_block() -> None:
    sb = _load_example().scheduler.swap_brake
    assert isinstance(sb, SwapBrakeConfig)
    assert sb.enabled is True
    assert sb.min_spacing_seconds == 8.0
    assert sb.bucket_capacity == 3.0
    assert sb.refill_per_minute == 5.0
    assert sb.count_evictions is True


def test_example_documents_pin_detection_block() -> None:
    pd = _load_example().scheduler.pin_detection
    assert isinstance(pd, PinDetectionConfig)
    assert pd.enabled is True
    assert pd.expires_horizon_seconds == 3600.0


def test_example_template_has_no_rtx5090_crash_numerics() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").lower()
    assert "5090" not in text
    assert "crash zone" not in text
