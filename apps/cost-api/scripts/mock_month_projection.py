"""Mock a full month of phone-call volume through the real cost pipeline.

For the team lead's ask after seeing the demo video: project a month's cost
at 150-200 calls/day. Generates randomized-but-realistic per-call usage
(STT audio seconds, LLM tokens, TTS characters, telephony minutes) for each
of 30 days, ingests it through the real /internal/cost-events endpoint (real
HMAC signing, real Postgres, real Decimal cost-engine math from price_book —
nothing here computes cost itself), then reads back each day's real total
via /internal/costs/daily and sums them into a monthly projection.

Cleans up all mock rows at the end, every time — safe to re-run.

Prerequisites (see README.md):
  1. Postgres running: docker compose up -d (from infra/docker/)
  2. The API running in another terminal:
     COST_INGEST_SECRET=test-secret-do-not-use-in-prod \
     PYTHONPATH="app:../../packages/cost-engine" \
     uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000

Run: PYTHONPATH="app:../../packages/cost-engine" python3 scripts/mock_month_projection.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
import requests

SECRET = "test-secret-do-not-use-in-prod"
BASE = "http://127.0.0.1:8000"
DSN = "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger"

NUM_DAYS = 30
CALLS_PER_DAY_RANGE = (150, 200)
RESTAURANT_ID = 1

# Realistic per-call ranges for a short takeaway/catering order call.
STT_AUDIO_SECONDS_RANGE = (75, 240)  # 1.25-4 min
LLM_INPUT_TOKENS_RANGE = (1100, 2400)
LLM_OUTPUT_TOKENS_RANGE = (55, 130)
LLM_LATENCY_MS_RANGE = (450, 950)
TTS_CHARACTERS_RANGE = (220, 480)


def sign(body: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def ingest(events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    resp = requests.post(
        f"{BASE}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, SECRET)},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def cleanup() -> None:
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_call_reviews WHERE call_id LIKE 'monthproj-%'")
    cur.execute("DELETE FROM usage_events WHERE call_id LIKE 'monthproj-%'")
    cur.execute("DELETE FROM calls WHERE call_id LIKE 'monthproj-%'")
    conn.commit()
    conn.close()


def random_time_on(day: date) -> str:
    seconds_into_day = random.randint(8 * 3600, 21 * 3600)  # spread across "business hours" 8am-9pm UTC
    start_of_day = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return (start_of_day + timedelta(seconds=seconds_into_day)).isoformat()


def build_day_events(day: date, num_calls: int) -> list[dict]:
    events = []
    for i in range(num_calls):
        call_id = f"monthproj-{day.isoformat()}-{i}"
        occurred_at = random_time_on(day)
        audio_seconds = random.randint(*STT_AUDIO_SECONDS_RANGE)
        events.append(dict(
            event_id=f"{call_id}-stt", call_id=call_id, restaurant_id=RESTAURANT_ID,
            stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
            audio_seconds=str(audio_seconds), occurred_at=occurred_at,
        ))
        events.append(dict(
            event_id=f"{call_id}-llm", call_id=call_id, restaurant_id=RESTAURANT_ID,
            stage="llm", provider="openai", model="gpt-4o", billing_unit="million_tokens",
            input_tokens=random.randint(*LLM_INPUT_TOKENS_RANGE),
            output_tokens=random.randint(*LLM_OUTPUT_TOKENS_RANGE),
            latency_ms=random.randint(*LLM_LATENCY_MS_RANGE), occurred_at=occurred_at,
        ))
        events.append(dict(
            event_id=f"{call_id}-tts", call_id=call_id, restaurant_id=RESTAURANT_ID,
            stage="tts", provider="elevenlabs", model="eleven_flash_v2", billing_unit="1k_characters",
            characters=random.randint(*TTS_CHARACTERS_RANGE), occurred_at=occurred_at,
        ))
        events.append(dict(
            event_id=f"{call_id}-voice", call_id=call_id, restaurant_id=RESTAURANT_ID,
            stage="telephony", provider="plivo", model="voice", billing_unit="minute",
            quantity=str(round(audio_seconds / 60, 2)), occurred_at=occurred_at,
        ))
    return events


def main() -> None:
    cleanup()
    random.seed(42)  # reproducible run

    start_day = date.today() - timedelta(days=NUM_DAYS)
    daily_results = []
    total_calls = 0

    print(f"Mocking {NUM_DAYS} days, {CALLS_PER_DAY_RANGE[0]}-{CALLS_PER_DAY_RANGE[1]} calls/day...")
    for offset in range(NUM_DAYS):
        day = start_day + timedelta(days=offset)
        num_calls = random.randint(*CALLS_PER_DAY_RANGE)
        total_calls += num_calls

        events = build_day_events(day, num_calls)
        # Chunk to keep individual requests reasonably sized.
        for chunk_start in range(0, len(events), 400):
            ingest(events[chunk_start:chunk_start + 400])

        day_cost = requests.get(
            f"{BASE}/internal/costs/daily",
            params={"restaurant_id": RESTAURANT_ID, "target_date": day.isoformat()},
            timeout=30,
        ).json()
        daily_results.append({"day": day.isoformat(), "calls": num_calls, **day_cost})
        print(f"  {day.isoformat()}  {num_calls:3d} calls  ->  ${day_cost['total_cost_usd']}")

    variable_total = sum(Decimal(r["variable_cost_usd"]) for r in daily_results)
    fixed_total = sum(Decimal(r["fixed_cost_usd"]) for r in daily_results)
    grand_total = variable_total + fixed_total
    costs = [Decimal(r["total_cost_usd"]) for r in daily_results]

    print()
    print("=" * 70)
    print(f"MONTH PROJECTION ({NUM_DAYS} days, {CALLS_PER_DAY_RANGE[0]}-{CALLS_PER_DAY_RANGE[1]} calls/day)")
    print("=" * 70)
    print(f"  Total calls simulated:     {total_calls}")
    print(f"  Avg calls/day:             {total_calls / NUM_DAYS:.1f}")
    print(f"  Variable cost (phone):     ${variable_total:.2f}")
    print(f"  Fixed cost (server, prorated across the {NUM_DAYS} mocked days): ${fixed_total:.2f}")
    print(f"  TOTAL projected cost:      ${grand_total:.2f}")
    print(f"  Avg cost / call:           ${(variable_total / total_calls):.4f}")
    print(f"  Cheapest day:              ${min(costs):.2f}")
    print(f"  Most expensive day:        ${max(costs):.2f}")
    print()
    print("All numbers above are real cost-engine output (Decimal math against")
    print("the live price_book) run against real Postgres — nothing here computes")
    print("cost itself, it only generates realistic input volume.")

    cleanup()
    print("\nAll mock data cleaned up.")


if __name__ == "__main__":
    main()
