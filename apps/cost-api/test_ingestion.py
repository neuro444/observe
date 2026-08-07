"""
End-to-end test against the REAL dockerized Postgres — not a mock.
Run: PYTHONPATH="app:../../packages/cost-engine" ../../.venv/bin/python test_ingestion.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, "app")
os.environ["COST_INGEST_SECRET"] = "test-secret-do-not-use-in-prod"

import psycopg2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

SECRET = os.environ["COST_INGEST_SECRET"]
DATABASE_URL = main.DATABASE_URL


def sign(body: bytes, secret: str, *, timestamp: str | None = None) -> str:
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def cleanup():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("DELETE FROM usage_events WHERE event_id LIKE 'test-evt-%'")
    cur.execute("DELETE FROM calls WHERE call_id LIKE 'test-call-%'")
    conn.commit()
    conn.close()


def make_event(event_id: str, call_id: str = "test-call-e2e-001", **overrides) -> dict:
    event = dict(
        event_id=event_id, call_id=call_id, restaurant_id=1,
        stage="llm", provider="openai", model="gpt-5.6-luna", billing_unit="million_tokens",
        input_tokens=9510, cached_input_tokens=0, output_tokens=401,
        occurred_at="2026-08-07T12:00:00+00:00",
    )
    event.update(overrides)
    return event


def run():
    cleanup()
    client = TestClient(main.app)
    passed, failed = [], []

    def check(name, cond):
        (passed if cond else failed).append(name)
        print(("PASS: " if cond else "FAIL: ") + name)

    # 1. Real, signed, valid request against real Postgres.
    body = json.dumps({"events": [make_event("test-evt-1")]}).encode()
    resp = client.post("/internal/cost-events", content=body,
                        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, SECRET)})
    check("valid request returns 200", resp.status_code == 200)
    check("inserted 1 row", resp.json().get("inserted") == 1)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT calculated_cost_usd, price_version_id FROM usage_events WHERE event_id = 'test-evt-1'")
    row = cur.fetchone()
    check("row exists in real Postgres with a cost", row is not None and row[0] > 0)
    # gpt-5.6-luna: 9510 in * 0.20/1M + 401 out * 1.20/1M = 0.001902 + 0.0004812 = 0.0023832
    check("cost matches expected Decimal calculation ($0.002383)",
          row is not None and abs(Decimal(row[0]) - Decimal("0.002383")) < Decimal("0.000001"))
    check("price_version_id references a real price_book row", row is not None and row[1] is not None)

    cur.execute("SELECT status FROM calls WHERE call_id = 'test-call-e2e-001'")
    call_row = cur.fetchone()
    check("parent 'calls' row was created via FK-safe upsert", call_row is not None)

    # 2. Duplicate event_id -> skipped.
    resp2 = client.post("/internal/cost-events", content=body,
                         headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, SECRET)})
    check("duplicate event_id skipped", resp2.json().get("skipped_duplicates") == 1)

    # 3. Cached tokens actually reduce cost vs. no cache.
    body_cached = json.dumps({"events": [make_event("test-evt-2", cached_input_tokens=5000)]}).encode()
    client.post("/internal/cost-events", content=body_cached,
                headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body_cached, SECRET)})
    cur.execute("SELECT calculated_cost_usd FROM usage_events WHERE event_id = 'test-evt-2'")
    cached_cost = cur.fetchone()[0]
    check("caching 5000 tokens produces a LOWER cost than uncached", cached_cost < Decimal(row[0]))

    # 4. Bad signature -> 403.
    resp4 = client.post("/internal/cost-events", content=body,
                         headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, "wrong")})
    check("wrong secret rejected with 403", resp4.status_code == 403)

    # 5. Unknown model -> reported as rejected, not a crash.
    bad = json.dumps({"events": [make_event("test-evt-3", model="totally-made-up-model")]}).encode()
    resp5 = client.post("/internal/cost-events", content=bad,
                         headers={"Content-Type": "application/json", "X-Cost-Signature": sign(bad, SECRET)})
    r5 = resp5.json()
    check("unknown model rejected cleanly (RateNotFoundError), not a 500", resp5.status_code == 200)
    check("unknown model reported in 'rejected'", len(r5.get("rejected", [])) == 1)

    conn.close()
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    cleanup()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
