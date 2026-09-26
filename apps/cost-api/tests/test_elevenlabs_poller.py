from decimal import Decimal

from elevenlabs_poller import build_events


def test_call_record_maps_to_one_voice_agent_event_with_real_cost():
    events = build_events({
        "conversation_id": "conv_pytest_1",
        "call_duration_secs": 187,
        "total_input_tokens": 4210,
        "total_output_tokens": 980,
        "total_cost_usd": 0.0432,
        "model": "gpt-5.6-luna",
        "created_at": "2026-09-26T03:56:16.031403",
    })

    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == "conv_pytest_1-voice-agent"
    assert event["call_id"] == "conv_pytest_1"
    assert event["stage"] == "voice_agent"
    assert event["provider"] == "elevenlabs"
    assert event["model"] == "gpt-5.6-luna"
    assert event["billing_unit"] == "call"
    assert Decimal(event["quantity"]) == Decimal("0.0432")
    assert event["input_tokens"] == 4210
    assert event["output_tokens"] == 980
    assert event["call_duration_seconds"] == "187"
    assert event["occurred_at"] == "2026-09-26T03:56:16.031403"


def test_missing_conversation_id_is_skipped():
    assert build_events({"total_cost_usd": 1.0, "created_at": "2026-09-26T00:00:00"}) == []


def test_missing_created_at_is_skipped():
    assert build_events({"conversation_id": "conv_x", "total_cost_usd": 1.0}) == []


def test_negative_cost_is_skipped():
    events = build_events({
        "conversation_id": "conv_x", "total_cost_usd": -1.0, "created_at": "2026-09-26T00:00:00",
    })
    assert events == []


def test_zero_cost_call_still_forwarded():
    # A short/aborted call can legitimately cost $0 -- still worth a record
    # (e.g. for call-count purposes), not silently dropped like a bad record.
    events = build_events({
        "conversation_id": "conv_zero", "total_cost_usd": 0, "call_duration_secs": 3,
        "total_input_tokens": 0, "total_output_tokens": 0, "model": None,
        "created_at": "2026-09-26T00:07:38.563916",
    })
    assert len(events) == 1
    assert Decimal(events[0]["quantity"]) == Decimal("0")
    assert events[0]["model"] == "unknown"


def test_missing_duration_omits_call_duration_seconds():
    events = build_events({
        "conversation_id": "conv_x", "total_cost_usd": 1.0, "created_at": "2026-09-26T00:00:00",
    })
    assert events[0]["call_duration_seconds"] is None
