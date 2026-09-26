"""Polls 11agent_repo's GET /cost/calls and forwards every record into this
same service's own /internal/cost-events endpoint.

11agent_repo's dashboard feeds (including /cost/calls) require an X-API-Key
matching its own ELEVENLABS_AGENT_API_KEY -- ELEVENLABS_AGENT_API_KEY here is
that same value, sent on every poll. Deliberately a distinct secret from
COST_INGEST_SECRET, same reasoning as telephony_poller.py: one authenticates
us to 11agent_repo, the other authenticates 11agent_repo's data to us.

Shared by two callers so there's one source of truth:
  - main.py's scheduled job (the real, ongoing path).
  - scripts/poll_elevenlabs_cost_events.py (manual/debugging use).

Each /cost/calls record already carries ElevenLabs' own real, final billed
total (metadata.cost_fiat -- LLM + TTS + ASR + platform bundled together, see
11agent_repo/cost_extraction.py). That total is not decomposable into
per-stage dollars, so this maps to a single stage="voice_agent" usage event
per call rather than the multiple stage events telephony_poller.py builds --
cost_engine.calculate_cost() stores that number verbatim for this stage
instead of repricing it from a price_book rate (there isn't one to look up).

Safe to run repeatedly / on a schedule: cost-api's own idempotency
(event_id derived from conversation_id) means re-polling the same call is a
no-op, not a double-count.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal, InvalidOperation

import requests

logger = logging.getLogger(__name__)


def sign(body: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def fetch_elevenlabs_calls(elevenlabs_url: str, limit: int = 50, *, api_key: str = "") -> list[dict]:
    headers = {"X-API-Key": api_key} if api_key else {}
    r = requests.get(
        f"{elevenlabs_url}/cost/calls", params={"limit": limit}, headers=headers, timeout=10
    )
    r.raise_for_status()
    return r.json().get("calls", [])


def build_events(record: dict) -> list[dict]:
    """One usage-cost event per 11agent_repo call record, or none if the
    record is missing what's needed to price/attribute it."""
    conversation_id = record.get("conversation_id")
    if not conversation_id:
        return []
    try:
        cost = Decimal(str(record.get("total_cost_usd", "0")))
    except (InvalidOperation, TypeError, ValueError):
        return []
    if cost < 0:
        return []
    occurred_at = record.get("created_at")
    if not occurred_at:
        return []

    duration = record.get("call_duration_secs")
    return [{
        "event_id": f"{conversation_id}-voice-agent",
        "call_id": conversation_id,
        "restaurant_id": 1,
        "stage": "voice_agent",
        "provider": "elevenlabs",
        "model": record.get("model") or "unknown",
        "billing_unit": "call",
        "input_tokens": record.get("total_input_tokens", 0),
        "output_tokens": record.get("total_output_tokens", 0),
        "quantity": str(cost),
        "call_ended_at": occurred_at,
        "call_duration_seconds": str(duration) if duration is not None else None,
        "occurred_at": occurred_at,
    }]


def ingest(events: list[dict], *, cost_api_url: str, secret: str) -> dict:
    body = json.dumps({"events": events}).encode()
    r = requests.post(
        f"{cost_api_url}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body, secret)},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def poll_once(*, elevenlabs_url: str, cost_api_url: str, secret: str, elevenlabs_api_key: str = "") -> dict:
    """Returns a small summary dict -- callers log it however fits their context."""
    records = fetch_elevenlabs_calls(elevenlabs_url, api_key=elevenlabs_api_key)
    events = [e for r in records for e in build_events(r)]
    if not events:
        return {"records_seen": len(records), "events_forwarded": 0}
    result = ingest(events, cost_api_url=cost_api_url, secret=secret)
    return {"records_seen": len(records), "events_forwarded": len(events), **result}
