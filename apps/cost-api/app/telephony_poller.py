"""Polls telephony's GET /cost/calls and forwards every cost-relevant
record into this same service's own /internal/cost-events endpoint.

Shared by two callers so there's one source of truth:
  - main.py's scheduled job (the real, ongoing path once telephony is
    deployed somewhere reachable).
  - scripts/poll_telephony_cost_events.py (manual/debugging use).

Handles both record types telephony's /cost/calls returns (no type
filtering on their side, so both show up in the same response):
  - "call_ended": Plivo's real call duration -> one telephony/voice event.
  - "llm_turn": chat_manager's /chat response data (tokens, tts_chars),
    forwarded by telephony since 2026-08-25 -- one llm event + one tts
    event per turn.

Deliberately drops the raw caller phone number on call_ended records --
telephony's local file stores it in plaintext (a known issue there), but
our cost events only need call_id + duration, so there's no reason to
carry PII any further than it already goes.

Safe to run repeatedly / on a schedule: cost-api's own idempotency
(event_id derived from call_uuid + turn_seq) means re-polling the same
call/turn is a no-op, not a double-count.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


def sign(body: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
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
        if tts_chars:  # a silent/tool-only turn has nothing to bill TTS for
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


def ingest(events: list[dict], *, cost_api_url: str, secret: str) -> dict:
    body = json.dumps({"events": events}).encode()
    r = requests.post(
        f"{cost_api_url}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, secret)},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def poll_once(*, telephony_url: str, cost_api_url: str, secret: str) -> dict:
    """Returns a small summary dict -- callers log it however fits their context."""
    records = fetch_telephony_calls(telephony_url)
    events = [e for r in records for e in build_events(r)]
    if not events:
        return {"records_seen": len(records), "events_forwarded": 0}
    result = ingest(events, cost_api_url=cost_api_url, secret=secret)
    return {"records_seen": len(records), "events_forwarded": len(events), **result}
