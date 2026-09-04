"""GET /internal/costs/daily -- date param handling.

FastAPI silently drops unrecognized query params rather than erroring, so a
caller using `?date=` instead of the handler's real `target_date` param used
to fail silently and fall back to today. These tests pin that `date` now
works as an alias, and that `target_date` still works unchanged."""
from __future__ import annotations


def test_target_date_param_is_honored(client):
    resp = client.get("/internal/costs/daily", params={"target_date": "2026-01-15"})
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-01-15"


def test_date_alias_is_honored(client):
    resp = client.get("/internal/costs/daily", params={"date": "2026-01-15"})
    assert resp.status_code == 200
    assert resp.json()["date"] == "2026-01-15"


def test_no_date_param_defaults_to_today(client):
    from datetime import datetime, timezone

    resp = client.get("/internal/costs/daily")
    assert resp.status_code == 200
    assert resp.json()["date"] == datetime.now(timezone.utc).date().isoformat()
