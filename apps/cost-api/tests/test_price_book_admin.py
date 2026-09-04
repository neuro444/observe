"""price_book admin endpoints -- add/approve/reject/deactivate rates.

Never UPDATEs or DELETEs history: a rate change always inserts a new row and
closes the old one out via effective_to, only at approval time."""
from __future__ import annotations

from decimal import Decimal

import pytest

PROVIDER = "pytest-vendor"


@pytest.fixture(autouse=True)
def _cleanup_price_book(db_connection):
    yield
    with db_connection.cursor() as cur:
        # usage_events.price_version_id references price_book -- any row a
        # test priced against must go first, or the price_book delete below
        # hits a foreign key violation.
        cur.execute("DELETE FROM usage_events WHERE provider = %s", (PROVIDER,))
        cur.execute("DELETE FROM calls WHERE call_id LIKE 'pytest-pb-%%'")
        cur.execute("DELETE FROM price_book WHERE provider = %s", (PROVIDER,))
    db_connection.commit()


def _create(client, **overrides):
    body = dict(
        provider=PROVIDER, model="test-model", billing_unit="minute", flat_rate="0.01",
    )
    body.update(overrides)
    return client.post("/internal/price-book", json=body)


def test_create_rate_starts_pending_and_not_billable(client):
    resp = _create(client)
    assert resp.status_code == 200
    row = resp.json()
    assert row["approval_status"] == "pending"

    listed = client.get("/internal/price-book", params={"provider": PROVIDER}).json()["rates"]
    assert listed == []  # active-only view excludes pending rows


def test_create_llm_rate_requires_input_and_output(client):
    resp = _create(client, billing_unit="million_tokens", flat_rate=None)
    assert resp.status_code == 422


def test_create_flat_rate_requires_flat_rate(client):
    resp = _create(client, flat_rate=None)
    assert resp.status_code == 422


def test_approve_makes_rate_active(client):
    rate_id = _create(client).json()["id"]
    resp = client.patch(f"/internal/price-book/{rate_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["approval_status"] == "approved"

    listed = client.get("/internal/price-book", params={"provider": PROVIDER}).json()["rates"]
    assert any(r["id"] == rate_id for r in listed)


def test_approve_closes_out_previous_active_rate(client):
    first_id = _create(client, flat_rate="0.01").json()["id"]
    client.patch(f"/internal/price-book/{first_id}/approve")

    second_id = _create(client, flat_rate="0.02").json()["id"]
    client.patch(f"/internal/price-book/{second_id}/approve")

    history = client.get(
        "/internal/price-book", params={"provider": PROVIDER, "include_history": True},
    ).json()["rates"]
    first = next(r for r in history if r["id"] == first_id)
    second = next(r for r in history if r["id"] == second_id)
    assert first["effective_to"] is not None
    assert second["effective_to"] is None


def test_reject_does_not_touch_other_rows(client):
    first_id = _create(client, flat_rate="0.01").json()["id"]
    client.patch(f"/internal/price-book/{first_id}/approve")

    second_id = _create(client, flat_rate="0.02").json()["id"]
    resp = client.patch(f"/internal/price-book/{second_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["approval_status"] == "rejected"

    active = client.get("/internal/price-book", params={"provider": PROVIDER}).json()["rates"]
    assert len(active) == 1
    assert active[0]["id"] == first_id


def test_approve_already_resolved_row_is_conflict(client):
    rate_id = _create(client).json()["id"]
    client.patch(f"/internal/price-book/{rate_id}/approve")
    resp = client.patch(f"/internal/price-book/{rate_id}/approve")
    assert resp.status_code == 409


def test_deactivate_active_rate(client):
    rate_id = _create(client).json()["id"]
    client.patch(f"/internal/price-book/{rate_id}/approve")

    resp = client.post(f"/internal/price-book/{rate_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["effective_to"] is not None

    active = client.get("/internal/price-book", params={"provider": PROVIDER}).json()["rates"]
    assert active == []


def test_deactivate_pending_rate_is_conflict(client):
    rate_id = _create(client).json()["id"]
    resp = client.post(f"/internal/price-book/{rate_id}/deactivate")
    assert resp.status_code == 409


def test_approved_rate_is_actually_used_for_cost_calculation(client, db_connection):
    from test_ingestion import make_event, post_events

    rate_id = _create(client, model="pytest-model", billing_unit="minute", flat_rate="0.05").json()["id"]
    client.patch(f"/internal/price-book/{rate_id}/approve")

    event = make_event(
        "pytest-pb-evt-1", "pytest-pb-call-1", provider=PROVIDER, model="pytest-model",
        stage="telephony", billing_unit="minute", input_tokens=0, output_tokens=0,
        cached_input_tokens=0, quantity="4",
    )
    resp = post_events(client, [event])
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1

    with db_connection.cursor() as cur:
        cur.execute("SELECT calculated_cost_usd FROM usage_events WHERE event_id = 'pytest-pb-evt-1'")
        cost = cur.fetchone()[0]
    assert Decimal(cost) == Decimal("0.200000")  # 4 minutes * 0.05
