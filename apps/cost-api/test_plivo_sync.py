"""
Tests for plivo_cdr_sync.py

Runs entirely without a real database or Plivo account:
  - psycopg2 connections are mocked via unittest.mock.patch
  - requests.get is mocked to return controlled Plivo API responses

Run with:
    cd apps/cost-api
    python -m pytest test_plivo_sync.py -v
"""
from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import sys
import os

# Ensure the app package is importable without installing it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

import plivo_cdr_sync


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
CALL_UUID = "test-call-uuid-abc123"
DATABASE_URL = "postgresql://fake/fake"  # never actually connected in these tests

_PLIVO_RESPONSE_FINAL = {
    "total_amount": "0.015000",
    "billed_duration": 60,
    "hangup_cause": "NORMAL_CLEARING",
}

_PLIVO_RESPONSE_PENDING = {
    "total_amount": "0.000000",
    "billed_duration": 0,
    "hangup_cause": "",
}


def _mock_conn(cur_return: Any = None) -> tuple[MagicMock, MagicMock]:
    """Helper: return (conn_mock, cursor_mock) with __enter__/__exit__."""
    cur = MagicMock()
    cur.fetchall.return_value = cur_return or []
    cur.fetchone.return_value = None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


# ---------------------------------------------------------------------------
# fetch_cdr — happy path: total_amount > 0
# ---------------------------------------------------------------------------
class TestFetchCdrFinalAmount(unittest.TestCase):

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "test-auth-id")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "test-auth-token")
    @patch("plivo_cdr_sync._connect")
    @patch("plivo_cdr_sync.requests.get")
    def test_commits_final_cost_when_total_amount_positive(self, mock_get, mock_connect):
        """fetch_cdr should UPDATE usage_events and calls when CDR has a real total_amount."""
        # arrange
        mock_resp = MagicMock()
        mock_resp.json.return_value = _PLIVO_RESPONSE_FINAL
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        conn, cur = _mock_conn()
        mock_connect.return_value = conn

        # act
        plivo_cdr_sync.fetch_cdr(CALL_UUID, DATABASE_URL, scheduler=None, retry_count=0)

        # assert
        # Two UPDATE statements should have been executed (usage_events + calls)
        self.assertEqual(cur.execute.call_count, 2)
        conn.commit.assert_called_once()

        # The first execute should update calculated_cost_usd to 0.015000
        first_call_args = cur.execute.call_args_list[0][0]
        params = first_call_args[1]
        self.assertEqual(params[0], Decimal("0.015000"))
        self.assertEqual(params[1], Decimal("60"))   # audio_seconds
        self.assertEqual(params[3], CALL_UUID)       # call_id filter


# ---------------------------------------------------------------------------
# fetch_cdr — CDR still pending (total_amount = 0), retry should be scheduled
# ---------------------------------------------------------------------------
class TestFetchCdrRetryScheduling(unittest.TestCase):

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "test-auth-id")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "test-auth-token")
    @patch("plivo_cdr_sync.requests.get")
    def test_schedules_retry_when_amount_is_zero(self, mock_get):
        """fetch_cdr should schedule a retry via the scheduler when amount is 0."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _PLIVO_RESPONSE_PENDING
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        mock_scheduler = MagicMock()

        plivo_cdr_sync.fetch_cdr(CALL_UUID, DATABASE_URL, scheduler=mock_scheduler, retry_count=0)

        mock_scheduler.add_job.assert_called_once()
        job_kwargs = mock_scheduler.add_job.call_args
        # Verify the retry_count in kwargs incremented to 1
        self.assertEqual(job_kwargs.kwargs["kwargs"]["retry_count"], 1)

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "test-auth-id")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "test-auth-token")
    @patch("plivo_cdr_sync.requests.get")
    def test_does_not_retry_past_max_retries(self, mock_get):
        """fetch_cdr must not schedule further retries once _MAX_RETRIES is exhausted."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _PLIVO_RESPONSE_PENDING
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        mock_scheduler = MagicMock()

        plivo_cdr_sync.fetch_cdr(
            CALL_UUID, DATABASE_URL,
            scheduler=mock_scheduler,
            retry_count=plivo_cdr_sync._MAX_RETRIES,
        )

        mock_scheduler.add_job.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_cdr — missing credentials → early return, no HTTP call
# ---------------------------------------------------------------------------
class TestFetchCdrMissingCredentials(unittest.TestCase):

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "")
    @patch("plivo_cdr_sync.requests.get")
    def test_skips_when_no_credentials(self, mock_get):
        plivo_cdr_sync.fetch_cdr(CALL_UUID, DATABASE_URL)
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# store_pending_cdr — basic smoke test (DB writes succeed)
# ---------------------------------------------------------------------------
class TestStorePendingCdr(unittest.TestCase):

    @patch("plivo_cdr_sync._connect")
    def test_inserts_call_and_usage_event(self, mock_connect):
        conn, cur = _mock_conn()
        mock_connect.return_value = conn

        plivo_cdr_sync.store_pending_cdr(
            call_uuid=CALL_UUID,
            restaurant_id=1,
            caller="+17705550001",
            bill_duration=60,
            conversation_id="conv-abc",
            conversation_url="https://cx.plivo.com/...",
            estimated_rate_per_minute=Decimal("0.0085"),
            database_url=DATABASE_URL,
        )

        # Two SQL statements: INSERT INTO calls, INSERT INTO usage_events
        self.assertEqual(cur.execute.call_count, 2)
        conn.commit.assert_called_once()

    @patch("plivo_cdr_sync._connect")
    def test_estimated_cost_calculation(self, mock_connect):
        """60 seconds billed at $0.0085/min should produce $0.0085 estimated cost."""
        conn, cur = _mock_conn()
        mock_connect.return_value = conn

        plivo_cdr_sync.store_pending_cdr(
            call_uuid=CALL_UUID,
            restaurant_id=1,
            caller="+17705550001",
            bill_duration=60,
            conversation_id=None,
            conversation_url=None,
            estimated_rate_per_minute=Decimal("0.0085"),
            database_url=DATABASE_URL,
        )

        # The usage_events INSERT is the second execute call
        insert_params = cur.execute.call_args_list[1][0][1]
        # index 5 = calculated_cost_usd
        estimated_cost = insert_params[5]
        self.assertEqual(estimated_cost, Decimal("0.008500"))

    @patch("plivo_cdr_sync._connect")
    def test_caller_is_hashed(self, mock_connect):
        """Raw phone numbers must never be stored — verify hashing."""
        conn, cur = _mock_conn()
        mock_connect.return_value = conn
        raw_caller = "+17705550001"

        plivo_cdr_sync.store_pending_cdr(
            call_uuid=CALL_UUID,
            restaurant_id=1,
            caller=raw_caller,
            bill_duration=60,
            conversation_id=None,
            conversation_url=None,
            estimated_rate_per_minute=Decimal("0.0085"),
            database_url=DATABASE_URL,
        )

        # calls INSERT is the first execute call; param index 3 is caller_hash
        insert_params = cur.execute.call_args_list[0][0][1]
        stored_hash = insert_params[3]
        expected_hash = hashlib.sha256(raw_caller.encode()).hexdigest()[:32]
        self.assertEqual(stored_hash, expected_hash)
        self.assertNotIn(raw_caller, str(insert_params))


# ---------------------------------------------------------------------------
# run_nightly_plivo_backfill
# ---------------------------------------------------------------------------
class TestNightlyBackfill(unittest.TestCase):

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "")
    def test_skips_when_no_credentials(self):
        result = plivo_cdr_sync.run_nightly_plivo_backfill(DATABASE_URL)
        self.assertEqual(result, 0)

    @patch("plivo_cdr_sync.PLIVO_AUTH_ID", "test-auth-id")
    @patch("plivo_cdr_sync.PLIVO_AUTH_TOKEN", "test-auth-token")
    @patch("plivo_cdr_sync.fetch_cdr")
    @patch("plivo_cdr_sync._connect")
    def test_resolves_pending_calls(self, mock_connect, mock_fetch_cdr):
        stale_rows = [{"call_id": "uuid-1"}, {"call_id": "uuid-2"}]
        conn, cur = _mock_conn(cur_return=stale_rows)
        mock_connect.return_value = conn

        result = plivo_cdr_sync.run_nightly_plivo_backfill(DATABASE_URL)

        self.assertEqual(result, 2)
        self.assertEqual(mock_fetch_cdr.call_count, 2)
        # Verify it's called with retry_count=_MAX_RETRIES (no further retries from nightly job)
        for c in mock_fetch_cdr.call_args_list:
            self.assertEqual(c.kwargs["retry_count"], plivo_cdr_sync._MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()
