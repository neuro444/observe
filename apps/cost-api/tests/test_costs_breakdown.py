"""GET /internal/costs/breakdown -- day/month/year cost buckets, split by
provider, plus a summary block for the dashboard sidebar."""
from __future__ import annotations

from decimal import Decimal

from test_ingestion import make_event, post_events

RESTAURANT_ID = 1


def _seed(client, call_id: str, occurred_at: str, provider: str, **overrides):
    event = make_event(
        f"pytest-brk-{call_id}-{provider}", call_id,
        occurred_at=occurred_at, provider=provider, **overrides,
    )
    resp = post_events(client, [event])
    assert resp.status_code == 200, resp.text


def test_breakdown_splits_cost_by_provider_and_day(client):
    _seed(client, "pytest-call-brk-1", "2026-08-10T12:00:00+00:00", "openai")
    _seed(client, "pytest-call-brk-2", "2026-08-10T13:00:00+00:00", "elevenlabs",
          model="eleven_flash_v2", stage="tts", billing_unit="1k_characters",
          input_tokens=0, output_tokens=0, cached_input_tokens=0, characters=500)
    _seed(client, "pytest-call-brk-3", "2026-08-11T09:00:00+00:00", "openai")

    resp = client.get("/internal/costs/breakdown", params={
        "group_by": "day", "start_date": "2026-08-10", "end_date": "2026-08-11",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "day"
    periods = {p["period"]: p for p in body["periods"]}
    assert "2026-08-10" in periods
    assert "2026-08-11" in periods
    day10 = periods["2026-08-10"]
    assert Decimal(day10["openai_cost_usd"]) > 0
    assert Decimal(day10["elevenlabs_cost_usd"]) > 0
    assert Decimal(day10["plivo_cost_usd"]) == 0


def test_breakdown_month_grouping_aggregates_across_days(client):
    _seed(client, "pytest-call-brk-4", "2026-08-05T00:00:00+00:00", "openai")
    _seed(client, "pytest-call-brk-5", "2026-08-20T00:00:00+00:00", "openai")

    resp = client.get("/internal/costs/breakdown", params={
        "group_by": "month", "start_date": "2026-08-01", "end_date": "2026-08-31",
    })
    assert resp.status_code == 200
    periods = resp.json()["periods"]
    assert len(periods) == 1
    assert periods[0]["period"] == "2026-08-01"
    assert Decimal(periods[0]["openai_cost_usd"]) > 0


def test_breakdown_summary_totals_match_periods(client):
    _seed(client, "pytest-call-brk-6", "2026-08-12T00:00:00+00:00", "openai")
    _seed(client, "pytest-call-brk-7", "2026-08-13T00:00:00+00:00", "plivo",
          model="voice", stage="telephony", billing_unit="minute",
          input_tokens=0, output_tokens=0, cached_input_tokens=0,
          quantity=2.5)

    resp = client.get("/internal/costs/breakdown", params={
        "group_by": "day", "start_date": "2026-08-12", "end_date": "2026-08-13",
    })
    body = resp.json()
    summary = body["summary"]
    period_total = sum(Decimal(p["total_cost_usd"]) for p in body["periods"])
    assert abs(Decimal(summary["total_cost_usd"]) - period_total) < Decimal("0.000001")
    assert summary["call_count"] == 2
    assert Decimal(summary["openai_cost_usd"]) > 0
    assert Decimal(summary["plivo_cost_usd"]) > 0


def test_breakdown_fixed_cost_present_even_with_no_events(client):
    resp = client.get("/internal/costs/breakdown", params={
        "group_by": "day", "start_date": "2026-01-01", "end_date": "2026-01-01",
    })
    assert resp.status_code == 200
    periods = resp.json()["periods"]
    assert len(periods) == 1
    assert periods[0]["period"] == "2026-01-01"
    assert periods[0]["call_count"] == 0
    assert Decimal(periods[0]["fixed_cost_usd"]) > 0
    assert periods[0]["total_cost_usd"] == periods[0]["fixed_cost_usd"]


def test_breakdown_rejects_invalid_group_by(client):
    resp = client.get("/internal/costs/breakdown", params={"group_by": "week"})
    assert resp.status_code == 422
