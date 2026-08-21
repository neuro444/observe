"""Polls telephony's GET /cost/calls and forwards Plivo call minutes into
our real cost-api ingestion endpoint. The other half of the chat_manager/
telephony integration -- LLM/TTS still needs Rakshitha's side, but this half
needs nothing from anyone: /cost/calls already exists and works today.

Deliberately drops the raw caller phone number -- telephony's local file
stores it in plaintext (a known issue there), but our cost events only need
call_id + duration, so there's no reason to carry PII any further than it
already goes.

Safe to run repeatedly / on a schedule: cost-api's own idempotency
(event_id = call_uuid-derived) means re-polling the same call is a no-op,
not a double-count.

Run: python3 scripts/poll_telephony_plivo_minutes.py [--telephony-url URL] [--once]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import requests

COST_API_URL = "http://127.0.0.1:8000"
COST_INGEST_SECRET = "test-secret-do-not-use-in-prod"


def sign(body: bytes) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(COST_INGEST_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def fetch_telephony_calls(telephony_url: str, limit: int = 50) -> list[dict]:
    r = requests.get(f"{telephony_url}/cost/calls", params={"limit": limit}, timeout=10)
    r.raise_for_status()
    return r.json().get("calls", [])


def build_event(record: dict) -> dict | None:
    duration = record.get("duration_seconds")
    call_uuid = record.get("call_uuid")
    if not duration or not call_uuid:
        return None
    return {
        "event_id": f"{call_uuid}-voice",
        "call_id": call_uuid,
        "restaurant_id": 1,
        "stage": "telephony",
        "provider": "plivo",
        "model": "voice",
        "billing_unit": "minute",
        "quantity": str(round(duration / 60, 2)),
        "occurred_at": record.get("emitted_at") or datetime.now(timezone.utc).isoformat(),
    }


def ingest(events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    r = requests.post(
        f"{COST_API_URL}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body)},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def poll_once(telephony_url: str) -> None:
    records = fetch_telephony_calls(telephony_url)
    events = [e for e in (build_event(r) for r in records) if e is not None]
    if not events:
        print("nothing to forward")
        return
    result = ingest(events)
    print(f"forwarded {len(events)} call(s): {result}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telephony-url", default="http://127.0.0.1:8200")
    parser.add_argument("--once", action="store_true", help="poll once and exit (default: every 60s)")
    args = parser.parse_args()

    poll_once(args.telephony_url)
    if args.once:
        return
    while True:
        time.sleep(60)
        poll_once(args.telephony_url)


if __name__ == "__main__":
    main()
