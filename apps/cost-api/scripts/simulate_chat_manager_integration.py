"""Proves chat_manager + telephony's real event shapes flow correctly through
our EXISTING /internal/cost-events endpoint -- no new endpoint, no separate
system. This is the spec: whatever Rakshitha/Sgopi's emitter code ends up
sending should look like the three events built below.

Uses the exact /chat response shared in Slack (2026-08-20) for one real turn,
plus a representative Plivo hangup duration for the same call. Real cost-api,
real Postgres, real cost-engine math -- nothing here computes cost itself.

Run: PYTHONPATH="app:../../packages/cost-engine" python3 scripts/simulate_chat_manager_integration.py
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

# The exact /chat response Sgopi shared, 2026-08-20 1:54 PM.
CHAT_MANAGER_TURN = {
    "answer": "I have two Samosas and one Gobi Manchurian. Would you like anything else?",
    "session_id": "506c89ce-example-session",
    "model_used": "gpt-5.6-luna",
    "input_tokens": 8428,
    "output_tokens": 127,
    "total_tokens": 8555,
    "token_source": "api",
    "latency_ms": 3128.59,
    "tts_chars": 73,
}

# A representative telephony /voice/hangup -- Plivo's own Duration param,
# same field cost_emitter.py already captures locally today.
TELEPHONY_HANGUP_DURATION_SECONDS = 142


def sign(body: bytes, secret: str = SECRET) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def ingest(events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    resp = requests.post(
        f"{BASE}/internal/cost-events", data=body,
        headers={"Content-Type": "application/json", "X-Cost-Signature": sign(body)},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def cleanup() -> None:
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("DELETE FROM usage_events WHERE call_id LIKE 'chatmgr-sim-%'")
    cur.execute("DELETE FROM calls WHERE call_id LIKE 'chatmgr-sim-%'")
    conn.commit()
    conn.close()


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    cleanup()
    call_id = "chatmgr-sim-call-1"  # would be the real Plivo CallUUID
    now = datetime.now(timezone.utc).isoformat()

    section("1. LLM EVENT -- built from chat_manager's /chat response, unmodified")
    llm_event = dict(
        event_id=f"{call_id}-llm-turn1", call_id=call_id, restaurant_id=1,
        stage="llm", provider="openai", model=CHAT_MANAGER_TURN["model_used"],
        billing_unit="million_tokens",
        input_tokens=CHAT_MANAGER_TURN["input_tokens"],
        output_tokens=CHAT_MANAGER_TURN["output_tokens"],
        latency_ms=int(CHAT_MANAGER_TURN["latency_ms"]),
        occurred_at=now,
    )
    print(json.dumps(llm_event, indent=2))
    print(ingest([llm_event]))

    section("2. TTS EVENT -- built from the same response's tts_chars")
    tts_event = dict(
        event_id=f"{call_id}-tts-turn1", call_id=call_id, restaurant_id=1,
        stage="tts", provider="elevenlabs", model="eleven_turbo_v2_5",
        billing_unit="1k_characters",
        characters=CHAT_MANAGER_TURN["tts_chars"],
        occurred_at=now,
    )
    print(json.dumps(tts_event, indent=2))
    print(ingest([tts_event]))

    section("3. TELEPHONY EVENT -- built from /voice/hangup's Duration param")
    telephony_event = dict(
        event_id=f"{call_id}-voice", call_id=call_id, restaurant_id=1,
        stage="telephony", provider="plivo", model="voice", billing_unit="minute",
        quantity=str(round(TELEPHONY_HANGUP_DURATION_SECONDS / 60, 2)),
        occurred_at=now,
    )
    print(json.dumps(telephony_event, indent=2))
    print(ingest([telephony_event]))

    section("4. REAL COMPUTED COST -- what the existing pricing engine says")
    r = requests.get(f"{BASE}/internal/calls/{call_id}").json()
    print(f"\n{call_id}: total = ${r['total_cost_usd']}")
    for e in r["events"]:
        print(f"  {e['stage']:10s} {e['provider']:10s} {e['model']:20s} -> ${e['calculated_cost_usd']}")

    cleanup()
    section("DONE -- proves the existing endpoint/pricing/schema needs zero changes.")
    print("Only new price_book row needed was eleven_turbo_v2_5 (migration 008).")
    print("This is the exact event shape to hand to Rakshitha/Sgopi's emitter code.")


if __name__ == "__main__":
    main()
