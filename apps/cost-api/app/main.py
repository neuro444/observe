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

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from cost_engine import PriceBookLookup, RateNotFoundError, calculate_cost

app = FastAPI(title="NeuroHeart Cost API", version="0.1.0")

COST_INGEST_SECRET = os.getenv("COST_INGEST_SECRET", "")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger",
)
_lookup = PriceBookLookup(DATABASE_URL)


def _db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
