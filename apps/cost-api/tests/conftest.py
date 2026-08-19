"""Shared fixtures for the DB-backed integration tests. Requires a real
Postgres reachable at DATABASE_URL (defaults to the local docker-compose
instance; CI overrides this to its own service container) with migrations
already applied -- see infra/migrations/run_migrations.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

os.environ.setdefault("COST_INGEST_SECRET", "test-secret-do-not-use-in-prod")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger",
)


def _cleanup(prefix: str) -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daily_call_reviews WHERE call_id LIKE %s", (f"{prefix}%",))
            cur.execute("DELETE FROM usage_events WHERE call_id LIKE %s", (f"{prefix}%",))
            cur.execute("DELETE FROM calls WHERE call_id LIKE %s", (f"{prefix}%",))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean_test_rows():
    """Every test in this suite uses call_ids prefixed 'pytest-' -- never
    touches real data. Cleans up before AND after, so a previous failed run
    never leaves stale rows that make a later test misleadingly pass/fail."""
    _cleanup("pytest-")
    yield
    _cleanup("pytest-")


@pytest.fixture
def db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    yield conn
    conn.close()


@pytest.fixture
def client():
    import main  # imported here, after sys.path/env are set up above

    from fastapi.testclient import TestClient

    return TestClient(main.app)
