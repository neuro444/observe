"""End-to-end tests against a REAL Postgres -- not mocked. Converted from
the original standalone test_ingestion.py script into real pytest tests so
each behavior is individually visible (was previously one script that ran
top to bottom and either fully passed or died partway through).

All call/event ids are prefixed 'pytest-' -- see conftest.py's autouse
clean_test_rows fixture, which wipes anything with that prefix before and
after every test."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal

SECRET = "test-secret-do-not-use-in-prod"


def sign(body: bytes, secret: str = SECRET) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def make_event(event_id: str, call_id: str, **overrides) -> dict:
    event = dict(
        event_id=event_id, call_id=call_id, restaurant_id=1,
        stage="llm", provider="openai", model="gpt-5.6-luna", billing_unit="million_tokens",
        input_tokens=9510, cached_input_tokens=0, output_tokens=401,
        occurred_at="2026-08-07T12:00:00+00:00",
    )
    event.update(overrides)
    return event


def post_events(client, events: list[dict], *, secret: str = SECRET):
    body = json.dumps({"events": events}).encode()
    return client.post(
        "/internal/cost-events", content=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, secret)},
    )


def test_valid_signed_request_inserts_and_computes_real_cost(client, db_connection):
    event = make_event("pytest-evt-1", "pytest-call-1")
    resp = post_events(client, [event])

    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1

    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT calculated_cost_usd, price_version_id FROM usage_events WHERE event_id = 'pytest-evt-1'"
        )
        row = cur.fetchone()
    assert row is not None
    cost, price_version_id = row
    # gpt-5.6-luna: 9510 in * 0.20/1M + 401 out * 1.20/1M = 0.001902 + 0.0004812 = 0.0023832
    assert abs(Decimal(cost) - Decimal("0.002383")) < Decimal("0.000001")
    assert price_version_id is not None


def test_valid_request_creates_parent_call_row(client, db_connection):
    post_events(client, [make_event("pytest-evt-2", "pytest-call-2")])

    with db_connection.cursor() as cur:
        cur.execute("SELECT status FROM calls WHERE call_id = 'pytest-call-2'")
        row = cur.fetchone()
    assert row is not None


def test_duplicate_event_id_is_skipped_not_double_inserted(client):
    event = make_event("pytest-evt-3", "pytest-call-3")
    first = post_events(client, [event])
    assert first.json()["inserted"] == 1

    second = post_events(client, [event])
    assert second.json()["skipped_duplicates"] == 1
    assert second.json().get("inserted", 0) == 0


def test_cached_tokens_produce_lower_cost_than_uncached(client, db_connection):
    post_events(client, [make_event("pytest-evt-4a", "pytest-call-4")])
    post_events(client, [make_event("pytest-evt-4b", "pytest-call-4", cached_input_tokens=5000)])

    with db_connection.cursor() as cur:
        cur.execute("SELECT calculated_cost_usd FROM usage_events WHERE event_id = 'pytest-evt-4a'")
        uncached_cost = cur.fetchone()[0]
        cur.execute("SELECT calculated_cost_usd FROM usage_events WHERE event_id = 'pytest-evt-4b'")
        cached_cost = cur.fetchone()[0]

    assert cached_cost < uncached_cost


def test_wrong_signature_rejected_with_403(client):
    event = make_event("pytest-evt-5", "pytest-call-5")
    body = json.dumps({"events": [event]}).encode()
    resp = client.post(
        "/internal/cost-events", content=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, "wrong-secret")},
    )
    assert resp.status_code == 403


def test_unknown_model_rejected_cleanly_not_a_500(client):
    event = make_event("pytest-evt-6", "pytest-call-6", model="totally-made-up-model")
    resp = post_events(client, [event])

    assert resp.status_code == 200  # never a 500 -- an unpriced event is a clean per-event rejection
    body = resp.json()
    assert len(body.get("rejected", [])) == 1


def test_batch_with_mixed_valid_and_invalid_events(client, db_connection):
    good = make_event("pytest-evt-7a", "pytest-call-7")
    bad = make_event("pytest-evt-7b", "pytest-call-7", model="totally-made-up-model")
    resp = post_events(client, [good, bad])

    body = resp.json()
    assert body["inserted"] == 1
    assert len(body["rejected"]) == 1

    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM usage_events WHERE call_id = 'pytest-call-7'")
        count = cur.fetchone()[0]
    assert count == 1
