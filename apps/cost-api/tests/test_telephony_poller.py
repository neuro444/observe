from decimal import Decimal

from telephony_poller import build_events


def test_call_ended_keeps_actual_and_billed_duration_separate():
    events = build_events({
        "event": "call_ended",
        "emitted_at": "2026-08-29T02:57:14.919619+00:00",
        "call_uuid": "pytest-poller-call",
        "duration_seconds": 65,
        "bill_duration_seconds": 66,
    })

    assert len(events) == 1
    event = events[0]
    assert event["call_ended_at"] == "2026-08-29T02:57:14.919619+00:00"
    assert event["call_duration_seconds"] == "65"
    assert Decimal(event["quantity"]) == Decimal("1.1")


def test_legacy_call_end_uses_actual_duration_for_billing_fallback():
    event = build_events({
        "event": "call_ended",
        "emitted_at": "2026-08-29T02:57:14.919619+00:00",
        "call_uuid": "pytest-legacy-poller-call",
        "duration_seconds": 65,
    })[0]

    assert Decimal(event["quantity"]) == Decimal("65") / Decimal("60")
