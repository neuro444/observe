"""
Nightly call-review anomaly detection — Section 5 of the plan: one row per
call so an unusually long or token-heavy call (possible abuse, or a runaway
tool-call loop) can be spotted by eye. This is a manual-review surface,
separate from and in addition to real-time budget-threshold alerting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras


@dataclass(frozen=True)
class AnomalyThresholds:
    long_call_seconds: Decimal
    high_tokens_per_minute: Decimal
    high_cost_usd: Decimal

    @classmethod
    def from_env(cls) -> "AnomalyThresholds":
        return cls(
            long_call_seconds=Decimal(os.getenv("ANOMALY_LONG_CALL_SECONDS", "600")),
            high_tokens_per_minute=Decimal(os.getenv("ANOMALY_HIGH_TOKENS_PER_MINUTE", "3000")),
            high_cost_usd=Decimal(os.getenv("ANOMALY_HIGH_COST_USD", "1.00")),
        )


def detect_anomalies(
    *,
    duration_seconds: Decimal,
    total_tokens: int,
    total_cost_usd: Decimal,
    thresholds: AnomalyThresholds,
) -> list[tuple[str, str]]:
    """Pure function: given one call's aggregated stats, return
    (anomaly_reason, severity) pairs. No I/O, so it's cheap to unit test every
    threshold boundary directly."""
    found: list[tuple[str, str]] = []

    if duration_seconds > thresholds.long_call_seconds:
        severity = "critical" if duration_seconds > thresholds.long_call_seconds * 2 else "warning"
        found.append(("long_call", severity))

    if duration_seconds > 0:
        tokens_per_minute = (Decimal(total_tokens) / duration_seconds) * Decimal(60)
        if tokens_per_minute > thresholds.high_tokens_per_minute:
            severity = "critical" if tokens_per_minute > thresholds.high_tokens_per_minute * 2 else "warning"
            found.append(("high_tokens_per_minute", severity))

    if total_cost_usd > thresholds.high_cost_usd:
        severity = "critical" if total_cost_usd > thresholds.high_cost_usd * 2 else "warning"
        found.append(("high_cost", severity))

    return found


def scan_and_record(dsn: str, review_date: date, thresholds: Optional[AnomalyThresholds] = None) -> int:
    """Aggregate every call that started on `review_date`, run detection, and
    upsert findings into daily_call_reviews. ON CONFLICT DO NOTHING (backed by
    the unique constraint in migration 003) means a rerun for the same date
    never duplicates rows or overwrites a review a human already did. Returns
    the number of new rows actually inserted."""
    thresholds = thresholds or AnomalyThresholds.from_env()
    inserted = 0

    with psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.call_id,
                COALESCE(MAX(ue.audio_seconds) FILTER (WHERE ue.stage = 'stt'), 0) AS duration_seconds,
                COALESCE(SUM(ue.input_tokens + ue.output_tokens) FILTER (WHERE ue.stage = 'llm'), 0) AS total_tokens,
                COALESCE(SUM(ue.calculated_cost_usd), 0) AS total_cost_usd
            FROM calls c
            JOIN usage_events ue ON ue.call_id = c.call_id
            WHERE c.started_at >= %s AND c.started_at < %s
            GROUP BY c.call_id
            """,
            (review_date, review_date + timedelta(days=1)),
        )
        rows = cur.fetchall()

        for row in rows:
            findings = detect_anomalies(
                duration_seconds=row["duration_seconds"],
                total_tokens=int(row["total_tokens"]),
                total_cost_usd=row["total_cost_usd"],
                thresholds=thresholds,
            )
            for reason, severity in findings:
                cur.execute(
                    """
                    INSERT INTO daily_call_reviews (review_date, call_id, anomaly_reason, severity)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (review_date, call_id, anomaly_reason) DO NOTHING
                    """,
                    (review_date, row["call_id"], reason, severity),
                )
                inserted += cur.rowcount
        conn.commit()

    return inserted
