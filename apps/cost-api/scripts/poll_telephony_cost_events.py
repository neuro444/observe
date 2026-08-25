"""Polls telephony's GET /cost/calls and forwards every cost-relevant
record into our real cost-api ingestion endpoint. Handles both record
types that endpoint returns (no type filtering on their side, so both
show up in the same response):

  - "call_ended": Plivo's real call duration -> one telephony/voice event.
  - "llm_turn": chat_manager's /chat response data (tokens, tts_chars),
    forwarded by telephony since 2026-08-25 -- one llm event + one tts
    event per turn. Was the last real gap in this integration; telephony
    had this data every turn but never forwarded it anywhere before.

Deliberately drops the raw caller phone number on call_ended records --
telephony's local file stores it in plaintext (a known issue there), but
our cost events only need call_id + duration, so there's no reason to
carry PII any further than it already goes.

Safe to run repeatedly / on a schedule: cost-api's own idempotency
(event_id derived from call_uuid + turn_seq) means re-polling the same
call/turn is a no-op, not a double-count.

Run: python3 scripts/poll_telephony_cost_events.py [--telephony-url URL] [--once]
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


def build_events(record: dict) -> list[dict]:
    """Dispatches on record["event"] -- returns however many cost-api
    events one telephony record maps to (0, 1, or 2)."""
    kind = record.get("event")
    now = datetime.now(timezone.utc).isoformat()
    occurred_at = record.get("emitted_at") or now

    if kind == "call_ended":
        duration = record.get("duration_seconds")
        call_uuid = record.get("call_uuid")
        if not duration or not call_uuid:
            return []
        return [{
            "event_id": f"{call_uuid}-voice",
            "call_id": call_uuid,
            "restaurant_id": 1,
            "stage": "telephony",
            "provider": "plivo",
            "model": "voice",
            "billing_unit": "minute",
            "quantity": str(round(duration / 60, 2)),
            "occurred_at": occurred_at,
        }]

    if kind == "llm_turn":
        call_uuid = record.get("call_uuid")
        turn_seq = record.get("turn_seq")
        if not call_uuid or turn_seq is None:
            return []
        events = [{
            "event_id": f"{call_uuid}-llm-turn{turn_seq}",
            "call_id": call_uuid,
            "restaurant_id": 1,
            "stage": "llm",
            "provider": "openai",
            "model": record.get("model", "gpt-5.6-luna"),
            "billing_unit": "million_tokens",
            "input_tokens": record.get("input_tokens", 0),
            "output_tokens": record.get("output_tokens", 0),
            "latency_ms": int(record.get("latency_ms") or 0),
            "occurred_at": occurred_at,
        }]
        tts_chars = record.get("tts_chars")
        if tts_chars:  # a turn with no spoken reply (e.g. a silent tool-only turn) has nothing to bill TTS for
            events.append({
                "event_id": f"{call_uuid}-tts-turn{turn_seq}",
                "call_id": call_uuid,
                "restaurant_id": 1,
                "stage": "tts",
                "provider": "elevenlabs",
                "model": "eleven_turbo_v2_5",
                "billing_unit": "1k_characters",
                "characters": tts_chars,
                "occurred_at": occurred_at,
            })
        return events

    return []  # unknown/future record type -- ignore rather than crash the poll


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
    events = [e for r in records for e in build_events(r)]
    if not events:
        print("nothing to forward")
        return
    result = ingest(events)
    print(f"forwarded {len(events)} event(s) from {len(records)} record(s): {result}")


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
