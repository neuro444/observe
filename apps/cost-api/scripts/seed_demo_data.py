"""
Seeds two realistic calls for a live demo: one normal, one deliberately
abusive (long + token-heavy) so the anomaly scan has something real to catch
on camera. Safe to re-run — cleans up its own rows first.

Run from apps/cost-api:
    PYTHONPATH="app:../../packages/cost-engine" ../../.venv/bin/python scripts/seed_demo_data.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "app")
os.environ.setdefault("COST_INGEST_SECRET", "test-secret-do-not-use-in-prod")

import psycopg2  # noqa: E402
import requests  # noqa: E402

import main  # noqa: E402

SECRET = os.environ["COST_INGEST_SECRET"]
BASE_URL = os.environ.get("COST_API_URL", "http://127.0.0.1:8000")
NOW = datetime.now(timezone.utc).isoformat()


def sign(body: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def cleanup():
    conn = psycopg2.connect(main.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_call_reviews WHERE call_id LIKE 'demo-%'")
    cur.execute("DELETE FROM usage_events WHERE call_id LIKE 'demo-%'")
    cur.execute("DELETE FROM calls WHERE call_id LIKE 'demo-%'")
    conn.commit()
    conn.close()


def ingest(events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    resp = requests.post(
        f"{BASE_URL}/internal/cost-events",
        data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, SECRET)},
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()


def main_run():
    cleanup()

    normal_call = [
        dict(event_id="demo-normal-stt", call_id="demo-call-normal", restaurant_id=1,
             stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
             audio_seconds="145", occurred_at=NOW),
        dict(event_id="demo-normal-llm", call_id="demo-call-normal", restaurant_id=1,
             stage="llm", provider="openai", model="gpt-5.6-luna", billing_unit="million_tokens",
             input_tokens=1855, output_tokens=77, latency_ms=740, occurred_at=NOW),
        dict(event_id="demo-normal-tts", call_id="demo-call-normal", restaurant_id=1,
             stage="tts", provider="elevenlabs", model="eleven_flash_v2", billing_unit="1k_characters",
             characters=430, occurred_at=NOW),
    ]

    abusive_call = [
        dict(event_id="demo-abuse-stt", call_id="demo-call-abuse", restaurant_id=1,
             stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
             audio_seconds="920", occurred_at=NOW),
        dict(event_id="demo-abuse-llm", call_id="demo-call-abuse", restaurant_id=1,
             stage="llm", provider="openai", model="gpt-4o", billing_unit="million_tokens",
             input_tokens=210000, output_tokens=8500, latency_ms=2100, occurred_at=NOW),
    ]

    result = ingest(normal_call + abusive_call)
    print(f"Ingested: {result}")
    print("\nDemo data ready. Call IDs: demo-call-normal, demo-call-abuse")


if __name__ == "__main__":
    main_run()
