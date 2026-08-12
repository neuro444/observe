"""
Internal-only cost-event ingestion API — apps/cost-api.

Same security model as the earlier client-repo proof of concept (HMAC-signed,
5-minute replay window), but now: real Postgres, server-side Decimal cost
calculation via the cost-engine package reading the DB-backed price_book, and
the fuller schema (calls, cached/reasoning tokens, stage, token_source).

Security note, same as before: this handles application-level auth only
(signature + timestamp check). Physical network isolation — binding this to
loopback-only, or a proxy rule that never exposes it publicly — is a
deployment step for wherever this actually gets hosted, not something this
code can enforce on its own.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from cost_engine import PriceBookLookup, RateNotFoundError, calculate_cost

from anomalies import scan_and_record
from price_check import reconcile_prices

logger = logging.getLogger(__name__)

COST_INGEST_SECRET = os.getenv("COST_INGEST_SECRET", "")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger",
)
ANOMALY_SCAN_HOUR_UTC = int(os.getenv("ANOMALY_SCAN_HOUR_UTC", "6"))
PRICE_CHECK_DAY_OF_WEEK = os.getenv("PRICE_CHECK_DAY_OF_WEEK", "mon")
PRICE_CHECK_HOUR_UTC = int(os.getenv("PRICE_CHECK_HOUR_UTC", "5"))

# Phase 1, per the lead: keep this simple — a flat monthly constant, not a
# fixed-costs table. Revisit if more than just the server needs tracking.
FIXED_SERVER_COST_MONTHLY_USD = Decimal(os.getenv("FIXED_SERVER_COST_MONTHLY_USD", "28.85"))
_lookup = PriceBookLookup(DATABASE_URL)
_scheduler = BackgroundScheduler()


def _run_nightly_scan() -> None:
    """Scans yesterday's calls — run once a day, always a day after the calls
    it's reviewing so a call spanning midnight is fully captured first."""
    review_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    try:
        inserted = scan_and_record(DATABASE_URL, review_date)
        logger.info("nightly anomaly scan for %s: %d new findings", review_date, inserted)
    except Exception:
        logger.exception("nightly anomaly scan failed for %s", review_date)


def _run_weekly_price_check() -> None:
    try:
        counts = reconcile_prices(DATABASE_URL)
        logger.info("weekly price check: %s", counts)
    except Exception:
        logger.exception("weekly price check failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _scheduler.add_job(_run_nightly_scan, "cron", hour=ANOMALY_SCAN_HOUR_UTC, id="nightly_anomaly_scan")
    _scheduler.add_job(
        _run_weekly_price_check, "cron",
        day_of_week=PRICE_CHECK_DAY_OF_WEEK, hour=PRICE_CHECK_HOUR_UTC, id="weekly_price_check",
    )
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="NeuroHeart Cost API", version="0.1.0", lifespan=lifespan)


def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _json_safe(value: Any) -> Any:
    """Decimal -> str (never float, so a displayed cost never re-introduces the
    drift the DECIMAL columns exist to avoid) and datetime -> isoformat."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_json_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in row.items()}


def _parse_signature_header(header: str) -> tuple[Optional[str], Optional[str]]:
    timestamp = signature = None
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v0":
            signature = value
    return timestamp, signature


def verify_signature(raw_body: bytes, signature_header: str, secret: str,
                      *, tolerance_seconds: int = 5 * 60, now: Optional[float] = None) -> bool:
    if not secret:
        return False
    timestamp, provided = _parse_signature_header(signature_header)
    if not timestamp or not provided:
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if abs(current - ts_int) > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


class UsageEventIn(BaseModel):
    """Raw usage only — no cost, no price fields. Matches the client repo's
    thin telemetry adapter (plivo_agent/cost_capture.py): it reports what
    happened, never what it should cost."""
    event_id: str
    call_id: str
    restaurant_id: int
    stage: str  # stt | llm | tts | telephony
    provider: str
    model: str
    billing_unit: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    characters: int = 0
    audio_seconds: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")  # generic minutes/messages for telephony
    latency_ms: Optional[int] = None
    token_source: str = "provider_reported"  # provider_reported | tiktoken_estimate
    had_empty_response: bool = False
    occurred_at: datetime


class UsageEventBatch(BaseModel):
    events: list[UsageEventIn] = Field(default_factory=list)


@app.post("/internal/cost-events")
async def ingest(request: Request, response: Response) -> dict[str, Any]:
    raw_body = await request.body()
    signature_header = request.headers.get("X-Cost-Signature", "")
    if not verify_signature(raw_body, signature_header, COST_INGEST_SECRET):
        response.status_code = 403
        return {"status": "rejected"}

    batch = UsageEventBatch.model_validate_json(raw_body)
    inserted = 0
    skipped = 0
    rejected: list[dict[str, str]] = []

    with _db() as conn, conn.cursor() as cur:
        for event in batch.events:
            cur.execute("SELECT 1 FROM usage_events WHERE event_id = %s", (event.event_id,))
            if cur.fetchone():
                skipped += 1
                continue

            try:
                cost, price_version_id = calculate_cost(
                    _lookup,
                    stage=event.stage,
                    provider=event.provider,
                    model=event.model,
                    billing_unit=event.billing_unit,
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    characters=event.characters,
                    audio_seconds=event.audio_seconds,
                    quantity=event.quantity,
                )
            except (RateNotFoundError, ValueError) as exc:
                rejected.append({"event_id": event.event_id, "reason": str(exc)})
                continue

            # Ensure the parent call row exists (FK) without clobbering it if
            # a later event for the same call already created it.
            cur.execute(
                """
                INSERT INTO calls (call_id, restaurant_id, started_at, status)
                VALUES (%s, %s, %s, 'in_progress')
                ON CONFLICT (call_id) DO NOTHING
                """,
                (event.call_id, event.restaurant_id, event.occurred_at),
            )

            cur.execute(
                """
                INSERT INTO usage_events (
                    event_id, call_id, restaurant_id, stage, provider, model,
                    input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
                    characters, audio_seconds, billable_minutes, latency_ms,
                    token_source, price_version_id, calculated_cost_usd,
                    had_empty_response, occurred_at, recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id, event.call_id, event.restaurant_id, event.stage,
                    event.provider, event.model,
                    event.input_tokens, event.cached_input_tokens, event.output_tokens,
                    event.reasoning_tokens, event.characters, event.audio_seconds,
                    event.quantity if event.stage == "telephony" else Decimal("0"),
                    event.latency_ms, event.token_source, price_version_id, cost,
                    event.had_empty_response, event.occurred_at, datetime.now(timezone.utc),
                ),
            )
            inserted += 1
        conn.commit()

    return {"status": "ok", "inserted": inserted, "skipped_duplicates": skipped, "rejected": rejected}


@app.get("/internal/calls")
async def list_calls(
    restaurant_id: Optional[int] = None,
    provider: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Per-call summary for the dashboard's call table: total cost, token
    totals, average LLM latency. `provider` filters to calls with at least one
    usage_event from that provider — the Section 4 provider dropdown."""
    where = []
    params: list[Any] = []
    if restaurant_id is not None:
        where.append("c.restaurant_id = %s")
        params.append(restaurant_id)
    if provider is not None:
        where.append(
            "EXISTS (SELECT 1 FROM usage_events ue WHERE ue.call_id = c.call_id AND ue.provider = %s)"
        )
        params.append(provider)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                c.call_id, c.restaurant_id, c.started_at, c.ended_at, c.status,
                COALESCE(SUM(ue.calculated_cost_usd), 0) AS total_cost_usd,
                COALESCE(SUM(ue.input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(ue.output_tokens), 0) AS total_output_tokens,
                AVG(ue.latency_ms) FILTER (WHERE ue.latency_ms IS NOT NULL) AS avg_latency_ms,
                ARRAY_AGG(DISTINCT ue.provider) FILTER (WHERE ue.provider IS NOT NULL) AS providers
            FROM calls c
            LEFT JOIN usage_events ue ON ue.call_id = c.call_id
            {where_clause}
            GROUP BY c.call_id, c.restaurant_id, c.started_at, c.ended_at, c.status
            ORDER BY c.started_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return {"calls": [_row_json_safe(dict(row)) for row in rows]}


@app.get("/internal/calls/{call_id}")
async def get_call(call_id: str) -> dict[str, Any]:
    """Full per-call breakdown: every usage event (stage, provider, model,
    tokens, latency, cost) plus the summed total — the drill-down behind a row
    in the calls table."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT call_id, restaurant_id, started_at, ended_at, status FROM calls WHERE call_id = %s",
            (call_id,),
        )
        call = cur.fetchone()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")

        cur.execute(
            """
            SELECT stage, provider, model, input_tokens, cached_input_tokens,
                   output_tokens, reasoning_tokens, characters, audio_seconds, billable_minutes,
                   latency_ms, calculated_cost_usd, occurred_at
            FROM usage_events
            WHERE call_id = %s
            ORDER BY occurred_at
            """,
            (call_id,),
        )
        events = cur.fetchall()

    total_cost = sum((event["calculated_cost_usd"] for event in events), Decimal("0"))
    return {
        "call": _row_json_safe(dict(call)),
        "events": [_row_json_safe(dict(event)) for event in events],
        "total_cost_usd": str(total_cost),
    }


@app.get("/internal/reviews")
async def list_reviews(review_date: Optional[date] = None, status: Optional[str] = None) -> dict[str, Any]:
    """The end-of-day anomaly table (Section 5): flagged calls for manual
    review, not an automated alert. Defaults to today."""
    target_date = review_date or datetime.now(timezone.utc).date()
    where = ["r.review_date = %s"]
    params: list[Any] = [target_date]
    if status is not None:
        where.append("r.status = %s")
        params.append(status)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.id, r.review_date, r.call_id, r.anomaly_reason, r.severity,
                   r.status, r.reviewer, r.notes, r.created_at,
                   c.restaurant_id, c.started_at
            FROM daily_call_reviews r
            JOIN calls c ON c.call_id = r.call_id
            WHERE {' AND '.join(where)}
            ORDER BY r.severity DESC, r.created_at DESC
            """,
            params,
        )
        rows = cur.fetchall()

    return {"review_date": target_date.isoformat(), "reviews": [_row_json_safe(dict(row)) for row in rows]}


VALID_REVIEW_STATUSES = {"reviewed", "legitimate", "abuse_suspected", "follow_up_required"}


class ReviewUpdateIn(BaseModel):
    status: str
    reviewer: Optional[str] = None
    notes: Optional[str] = None


@app.patch("/internal/reviews/{review_id}")
async def update_review(review_id: int, body: ReviewUpdateIn) -> dict[str, Any]:
    """Close the review workflow: a human marks a flagged call
    reviewed/legitimate/abuse_suspected. reviewer/notes are optional and
    never overwrite existing values with blank ones when omitted."""
    if body.status not in VALID_REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(VALID_REVIEW_STATUSES)}")

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE daily_call_reviews
            SET status = %s,
                reviewer = COALESCE(%s, reviewer),
                notes = COALESCE(%s, notes)
            WHERE id = %s
            RETURNING id, review_date, call_id, anomaly_reason, severity, status, reviewer, notes, created_at
            """,
            (body.status, body.reviewer, body.notes, review_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        conn.commit()

    return _row_json_safe(dict(row))


@app.post("/internal/reviews/run")
async def run_review_scan(review_date: Optional[date] = None) -> dict[str, Any]:
    """Manually trigger the anomaly scan for a given date (defaults to
    yesterday) — same logic the nightly scheduled job runs, exposed for
    testing/demo and for catching up a missed run."""
    target_date = review_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    inserted = scan_and_record(DATABASE_URL, target_date)
    return {"review_date": target_date.isoformat(), "inserted": inserted}


@app.get("/internal/costs/daily")
async def daily_cost(restaurant_id: int = 1, target_date: Optional[date] = None) -> dict[str, Any]:
    """Phase 1's actual ask: one basic daily cost total. Variable cost is the
    real sum of that day's usage_events; fixed cost is the flat monthly
    server cost prorated to a day. Channel is inferred from call_id — every
    WhatsApp event this codebase creates uses a "whatsapp-..." call_id (see
    utils/whatsapp_cost_capture.py); everything else is the phone line."""
    day = target_date or datetime.now(timezone.utc).date()
    range_start = day
    range_end = day + timedelta(days=1)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                CASE WHEN call_id LIKE 'whatsapp-%%' THEN 'whatsapp' ELSE 'phone' END AS channel,
                COALESCE(SUM(calculated_cost_usd), 0) AS cost_usd
            FROM usage_events
            WHERE restaurant_id = %s AND occurred_at >= %s AND occurred_at < %s
            GROUP BY channel
            """,
            (restaurant_id, range_start, range_end),
        )
        rows = {row["channel"]: row["cost_usd"] for row in cur.fetchall()}

    phone_cost = rows.get("phone", Decimal("0"))
    whatsapp_cost = rows.get("whatsapp", Decimal("0"))
    variable_cost = phone_cost + whatsapp_cost

    days_in_month = calendar.monthrange(day.year, day.month)[1]
    fixed_cost = (FIXED_SERVER_COST_MONTHLY_USD / Decimal(days_in_month)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )

    return {
        "date": day.isoformat(),
        "restaurant_id": restaurant_id,
        "phone_cost_usd": str(phone_cost),
        "whatsapp_cost_usd": str(whatsapp_cost),
        "variable_cost_usd": str(variable_cost),
        "fixed_cost_usd": str(fixed_cost),
        "total_cost_usd": str(variable_cost + fixed_cost),
    }


@app.post("/internal/prices/reconcile/run")
async def run_price_check() -> dict[str, Any]:
    """Manually trigger the weekly price-reconciliation check — same logic
    the scheduled job runs, exposed for testing/demo and catching up a
    missed run."""
    counts = reconcile_prices(DATABASE_URL)
    return {"result": counts}


@app.get("/internal/price-checks")
async def list_price_checks(limit: int = 50, outcome: Optional[str] = None) -> dict[str, Any]:
    """Recent price-check history — every rate checked, whether it matched,
    changed normally, or needs review."""
    where = []
    params: list[Any] = []
    if outcome is not None:
        where.append("outcome = %s")
        params.append(outcome)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, provider, model, field, old_value, new_value, outcome, reason, checked_at
            FROM price_check_runs
            {where_clause}
            ORDER BY checked_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    return {"checks": [_row_json_safe(dict(row)) for row in rows]}


@app.get("/internal/price-flags")
async def list_price_flags(status: str = "pending") -> dict[str, Any]:
    """Price changes that looked abnormal enough to need a human look,
    instead of being auto-applied — see price_check.py's threshold logic."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.status, f.resolver, f.resolution_notes, f.resolved_at, f.created_at,
                   r.provider, r.model, r.field, r.old_value, r.new_value, r.reason
            FROM price_review_flags f
            JOIN price_check_runs r ON r.id = f.check_run_id
            WHERE f.status = %s
            ORDER BY f.created_at DESC
            """,
            (status,),
        )
        rows = cur.fetchall()
    return {"flags": [_row_json_safe(dict(row)) for row in rows]}


class PriceFlagResolveIn(BaseModel):
    resolver: str
    resolution_notes: Optional[str] = None
    apply_new_value: bool = False


@app.patch("/internal/price-flags/{flag_id}")
async def resolve_price_flag(flag_id: int, body: PriceFlagResolveIn) -> dict[str, Any]:
    """Close out a flagged price change. `apply_new_value=true` versions
    price_book with the flagged new value (same effective-dating as an
    auto-update, just applied by a human instead); false just dismisses the
    flag (e.g. the old value was actually still correct)."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.status, r.provider, r.model, r.field, r.new_value, r.id as check_run_id
            FROM price_review_flags f JOIN price_check_runs r ON r.id = f.check_run_id
            WHERE f.id = %s
            """,
            (flag_id,),
        )
        flag = cur.fetchone()
        if flag is None:
            raise HTTPException(status_code=404, detail="flag not found")

        if body.apply_new_value:
            cur.execute(
                """
                SELECT id, provider, model, billing_unit, input_rate, cached_input_rate, output_rate, flat_rate,
                       pricing_source_url
                FROM price_book WHERE provider = %s AND model = %s AND effective_to IS NULL
                """,
                (flag["provider"], flag["model"]),
            )
            row = cur.fetchone()
            if row is not None:
                now = datetime.now(timezone.utc)
                cur.execute("UPDATE price_book SET effective_to = %s WHERE id = %s", (now, row["id"]))
                new_values = {
                    "input_rate": row["input_rate"], "cached_input_rate": row["cached_input_rate"],
                    "output_rate": row["output_rate"], "flat_rate": row["flat_rate"],
                }
                new_values[flag["field"]] = flag["new_value"]
                cur.execute(
                    """
                    INSERT INTO price_book (provider, model, billing_unit, input_rate, cached_input_rate,
                                             output_rate, flat_rate, effective_from, pricing_source_url,
                                             approval_status, last_verified_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)
                    """,
                    (row["provider"], row["model"], row["billing_unit"], new_values["input_rate"],
                     new_values["cached_input_rate"], new_values["output_rate"], new_values["flat_rate"],
                     now, row["pricing_source_url"], now),
                )

        cur.execute(
            """
            UPDATE price_review_flags
            SET status = 'resolved', resolver = %s, resolution_notes = %s, resolved_at = %s
            WHERE id = %s
            RETURNING id, status, resolver, resolution_notes, resolved_at
            """,
            (body.resolver, body.resolution_notes, datetime.now(timezone.utc), flag_id),
        )
        result = cur.fetchone()
        conn.commit()
    return _row_json_safe(dict(result))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
