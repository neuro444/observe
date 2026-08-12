"""
Full mock session, meant to be run live (and recorded) as a demo of the
whole cost-monitoring pipeline working end to end: a normal phone call, a
WhatsApp order, a suspicious call, then the nightly anomaly scan, the daily
cost total, and the weekly price-reconciliation check — all against the
real running cost-api and real Postgres, not simulated math.

Prerequisites (see README.md):
  1. Postgres running: docker compose up -d (from infra/docker/)
  2. The API running in another terminal:
     COST_INGEST_SECRET=test-secret-do-not-use-in-prod \
     PYTHONPATH="app:../../packages/cost-engine" \
     uvicorn main:app --app-dir app --host 127.0.0.1 --port 8000

Run: PYTHONPATH="app:../../packages/cost-engine" python3 scripts/run_mock_session.py
Cleans up all mock data it creates at the end, every time — safe to re-run.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import psycopg2
import requests

SECRET = "test-secret-do-not-use-in-prod"
BASE = "http://127.0.0.1:8000"
DSN = "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger"


def sign(body: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def ingest(events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    resp = requests.post(
        f"{BASE}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, SECRET)},
    )
    resp.raise_for_status()
    return resp.json()


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def cleanup() -> None:
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_call_reviews WHERE call_id LIKE 'mock-%'")
    cur.execute("DELETE FROM usage_events WHERE call_id LIKE 'mock-%' OR call_id LIKE 'whatsapp-%'")
    cur.execute("DELETE FROM calls WHERE call_id LIKE 'mock-%' OR call_id LIKE 'whatsapp-%'")
    conn.commit()
    conn.close()


def main() -> None:
    cleanup()
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    section("1. A NORMAL PHONE CALL — customer orders a cake")
    print(ingest([
        dict(event_id="mock-call1-stt", call_id="mock-call-normal", restaurant_id=1,
             stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
             audio_seconds="165", occurred_at=now),
        dict(event_id="mock-call1-llm", call_id="mock-call-normal", restaurant_id=1,
             stage="llm", provider="openai", model="gpt-4o", billing_unit="million_tokens",
             input_tokens=1650, output_tokens=85, latency_ms=680, occurred_at=now),
        dict(event_id="mock-call1-tts", call_id="mock-call-normal", restaurant_id=1,
             stage="tts", provider="elevenlabs", model="eleven_flash_v2", billing_unit="1k_characters",
             characters=340, occurred_at=now),
        dict(event_id="mock-call1-voice", call_id="mock-call-normal", restaurant_id=1,
             stage="telephony", provider="plivo", model="voice", billing_unit="minute",
             quantity="2.75", occurred_at=now),
    ]))

    section("2. A WHATSAPP ORDER — should cost $0 per message (Service category)")
    print(ingest([
        dict(event_id="mock-wa1-llm", call_id="whatsapp-llm:mock-1", restaurant_id=1,
             stage="llm", provider="openai", model="gpt-4o-mini", billing_unit="million_tokens",
             input_tokens=920, output_tokens=65, latency_ms=710, occurred_at=now),
        dict(event_id="mock-wa1-msg", call_id="whatsapp-msg:mock-1", restaurant_id=1,
             stage="telephony", provider="plivo", model="whatsapp", billing_unit="message",
             quantity="1", occurred_at=now),
    ]))

    section("3. A SUSPICIOUS CALL — long and token-heavy, should get flagged")
    print(ingest([
        dict(event_id="mock-call2-stt", call_id="mock-call-suspicious", restaurant_id=1,
             stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
             audio_seconds="950", occurred_at=now),
        dict(event_id="mock-call2-llm", call_id="mock-call-suspicious", restaurant_id=1,
             stage="llm", provider="openai", model="gpt-4o", billing_unit="million_tokens",
             input_tokens=195000, output_tokens=7200, latency_ms=1900, occurred_at=now),
    ]))

    section("4. REAL COMPUTED COST — per call")
    for call_id in ("mock-call-normal", "mock-call-suspicious"):
        r = requests.get(f"{BASE}/internal/calls/{call_id}").json()
        print(f"\n{call_id}: total = ${r['total_cost_usd']}")
        for e in r["events"]:
            print(f"  {e['stage']:10s} {e['provider']:10s} {e['model']:20s} -> ${e['calculated_cost_usd']}")

    section("5. NIGHTLY ANOMALY SCAN — normally automatic, triggered here to demo it")
    print(requests.post(f"{BASE}/internal/reviews/run", params={"review_date": today}).json())
    print("\nFlagged:")
    for review in requests.get(f"{BASE}/internal/reviews", params={"review_date": today}).json()["reviews"]:
        print(f"  {review['call_id']}: {review['anomaly_reason']} ({review['severity']})")

    section("6. TODAY'S TOTAL COST — phone + WhatsApp + flat server cost")
    for k, v in requests.get(f"{BASE}/internal/costs/daily", params={"restaurant_id": 1}).json().items():
        print(f"  {k}: {v}")

    section("7. WEEKLY PRICE-RECONCILIATION CHECK — against real live vendor pages")
    print(requests.post(f"{BASE}/internal/prices/reconcile/run").json())

    cleanup()
    section("DONE — all mock data cleaned up, database back to empty")


if __name__ == "__main__":
    main()
