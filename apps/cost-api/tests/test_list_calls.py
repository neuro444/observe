"""GET /internal/calls -- date-range filtering, provider filtering, paging."""
from __future__ import annotations

from test_ingestion import make_event, post_events


def _seed_call(client, call_id: str, occurred_at: str, provider: str = "openai", **overrides):
    event = make_event(f"pytest-lc-{call_id}", call_id, occurred_at=occurred_at, provider=provider, **overrides)
    resp = post_events(client, [event])
    assert resp.status_code == 200, resp.text


def test_start_date_excludes_earlier_calls(client):
    _seed_call(client, "pytest-call-lc-1", "2026-07-01T00:00:00+00:00")
    _seed_call(client, "pytest-call-lc-2", "2026-07-15T00:00:00+00:00")

    resp = client.get("/internal/calls", params={"start_date": "2026-07-10", "limit": 100})
    assert resp.status_code == 200
    ids = {c["call_id"] for c in resp.json()["calls"]}
    assert "pytest-call-lc-2" in ids
    assert "pytest-call-lc-1" not in ids


def test_end_date_is_inclusive(client):
    _seed_call(client, "pytest-call-lc-3", "2026-07-20T23:00:00+00:00")

    resp = client.get("/internal/calls", params={
        "start_date": "2026-07-20", "end_date": "2026-07-20", "limit": 100,
    })
    assert resp.status_code == 200
    ids = {c["call_id"] for c in resp.json()["calls"]}
    assert "pytest-call-lc-3" in ids


def test_offset_pages_past_limit(client):
    _seed_call(client, "pytest-call-lc-4", "2026-07-25T00:00:00+00:00")
    _seed_call(client, "pytest-call-lc-5", "2026-07-26T00:00:00+00:00")

    first_page = client.get("/internal/calls", params={
        "start_date": "2026-07-25", "end_date": "2026-07-26", "limit": 1, "offset": 0,
    }).json()["calls"]
    second_page = client.get("/internal/calls", params={
        "start_date": "2026-07-25", "end_date": "2026-07-26", "limit": 1, "offset": 1,
    }).json()["calls"]
    assert len(first_page) == 1
    assert len(second_page) == 1
    assert first_page[0]["call_id"] != second_page[0]["call_id"]
