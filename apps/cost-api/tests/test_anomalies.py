"""Pure-function tests for anomalies.detect_anomalies — every threshold
boundary exercised directly, no DB."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from anomalies import AnomalyThresholds, detect_anomalies  # noqa: E402

THRESHOLDS = AnomalyThresholds(
    long_call_seconds=Decimal("600"),
    high_tokens_per_minute=Decimal("3000"),
    high_cost_usd=Decimal("1.00"),
)


def test_nothing_flagged_when_everything_under_threshold():
    found = detect_anomalies(
        duration_seconds=Decimal("120"), total_tokens=1000,
        total_cost_usd=Decimal("0.05"), thresholds=THRESHOLDS,
    )
    assert found == []


def test_long_call_warning_just_over_threshold():
    found = detect_anomalies(
        duration_seconds=Decimal("601"), total_tokens=0,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert ("long_call", "warning") in found


def test_long_call_critical_over_double_threshold():
    found = detect_anomalies(
        duration_seconds=Decimal("1201"), total_tokens=0,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert ("long_call", "critical") in found


def test_long_call_exactly_at_threshold_not_flagged():
    found = detect_anomalies(
        duration_seconds=Decimal("600"), total_tokens=0,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert found == []


def test_high_tokens_per_minute_warning():
    # 3001 tokens/min just over 3000 threshold: 60s duration, 3001 tokens/min = 3001 tokens.
    found = detect_anomalies(
        duration_seconds=Decimal("60"), total_tokens=3001,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert ("high_tokens_per_minute", "warning") in found


def test_high_tokens_per_minute_critical():
    found = detect_anomalies(
        duration_seconds=Decimal("60"), total_tokens=6001,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert ("high_tokens_per_minute", "critical") in found


def test_zero_duration_skips_tokens_per_minute_without_crashing():
    found = detect_anomalies(
        duration_seconds=Decimal("0"), total_tokens=999999,
        total_cost_usd=Decimal("0"), thresholds=THRESHOLDS,
    )
    assert all(reason != "high_tokens_per_minute" for reason, _ in found)


def test_high_cost_warning():
    found = detect_anomalies(
        duration_seconds=Decimal("0"), total_tokens=0,
        total_cost_usd=Decimal("1.01"), thresholds=THRESHOLDS,
    )
    assert ("high_cost", "warning") in found


def test_high_cost_critical():
    found = detect_anomalies(
        duration_seconds=Decimal("0"), total_tokens=0,
        total_cost_usd=Decimal("2.01"), thresholds=THRESHOLDS,
    )
    assert ("high_cost", "critical") in found


def test_multiple_anomalies_flagged_together():
    found = detect_anomalies(
        # 1500s > 600 (long_call); 100000 tokens / 1500s * 60 = 4000/min > 3000 (high_tokens_per_minute)
        duration_seconds=Decimal("1500"), total_tokens=100000,
        total_cost_usd=Decimal("5.00"), thresholds=THRESHOLDS,
    )
    reasons = {reason for reason, _ in found}
    assert reasons == {"long_call", "high_tokens_per_minute", "high_cost"}


def test_thresholds_from_env_uses_defaults(monkeypatch):
    monkeypatch.delenv("ANOMALY_LONG_CALL_SECONDS", raising=False)
    monkeypatch.delenv("ANOMALY_HIGH_TOKENS_PER_MINUTE", raising=False)
    monkeypatch.delenv("ANOMALY_HIGH_COST_USD", raising=False)
    t = AnomalyThresholds.from_env()
    assert t.long_call_seconds == Decimal("600")
    assert t.high_tokens_per_minute == Decimal("3000")
    assert t.high_cost_usd == Decimal("1.00")


def test_thresholds_from_env_respects_overrides(monkeypatch):
    monkeypatch.setenv("ANOMALY_LONG_CALL_SECONDS", "300")
    t = AnomalyThresholds.from_env()
    assert t.long_call_seconds == Decimal("300")
