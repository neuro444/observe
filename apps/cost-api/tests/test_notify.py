"""Tests for notify.py. _build_message is pure (no I/O). send_price_review_email's
guard clauses and the actual smtplib call are tested by monkeypatching the
module's already-imported constants directly -- they're read from os.getenv
at import time, so setting env vars after import has no effect on them."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import notify  # noqa: E402

ONE_FLAG = [{
    "provider": "openai", "model": "gpt-4o", "field": "input_rate",
    "old_value": "2.5", "new_value": "5.0", "reason": "50% change exceeds 30% threshold",
}]
TWO_FLAGS = ONE_FLAG + [{
    "provider": "deepgram", "model": "nova-3-keyterm", "field": "flat_rate",
    "old_value": "0.0061", "new_value": "0.02", "reason": "228% change exceeds 30% threshold",
}]


class TestBuildMessage:
    def test_singular_subject_for_one_flag(self):
        msg = notify._build_message(ONE_FLAG)
        assert msg["Subject"] == "[Cost Monitoring] 1 price change needs review"

    def test_plural_subject_for_multiple_flags(self):
        msg = notify._build_message(TWO_FLAGS)
        assert msg["Subject"] == "[Cost Monitoring] 2 price changes need review"

    def test_plural_subject_for_zero_flags(self):
        msg = notify._build_message([])
        assert msg["Subject"] == "[Cost Monitoring] 0 price changes need review"

    def test_body_includes_each_flag(self):
        msg = notify._build_message(TWO_FLAGS)
        body = msg.get_content()
        assert "openai / gpt-4o (input_rate): 2.5 -> 5.0" in body
        assert "deepgram / nova-3-keyterm (flat_rate): 0.0061 -> 0.02" in body
        assert "50% change exceeds 30% threshold" in body

    def test_body_handles_missing_values_gracefully(self):
        flag = [{"provider": "plivo", "model": "whatsapp", "field": "flat_rate",
                  "old_value": None, "new_value": None, "reason": None}]
        msg = notify._build_message(flag)
        body = msg.get_content()
        assert "could not read a value" in body
        assert "n/a" in body


class TestSendPriceReviewEmailGuards:
    def test_does_nothing_when_notifications_disabled(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", False)
        with patch("notify.smtplib.SMTP") as mock_smtp:
            notify.send_price_review_email(ONE_FLAG)
            mock_smtp.assert_not_called()

    def test_does_nothing_when_no_flags(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(notify, "PRICE_REVIEW_NOTIFY_EMAILS", ["team@example.com"])
        monkeypatch.setattr(notify, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(notify, "SMTP_FROM_EMAIL", "cost@example.com")
        with patch("notify.smtplib.SMTP") as mock_smtp:
            notify.send_price_review_email([])
            mock_smtp.assert_not_called()

    def test_does_nothing_when_no_recipients_configured(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(notify, "PRICE_REVIEW_NOTIFY_EMAILS", [])
        monkeypatch.setattr(notify, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(notify, "SMTP_FROM_EMAIL", "cost@example.com")
        with patch("notify.smtplib.SMTP") as mock_smtp:
            notify.send_price_review_email(ONE_FLAG)
            mock_smtp.assert_not_called()

    def test_does_nothing_when_smtp_not_configured(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(notify, "PRICE_REVIEW_NOTIFY_EMAILS", ["team@example.com"])
        monkeypatch.setattr(notify, "SMTP_HOST", "")
        monkeypatch.setattr(notify, "SMTP_FROM_EMAIL", "cost@example.com")
        with patch("notify.smtplib.SMTP") as mock_smtp:
            notify.send_price_review_email(ONE_FLAG)
            mock_smtp.assert_not_called()

    def test_sends_when_fully_configured(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(notify, "PRICE_REVIEW_NOTIFY_EMAILS", ["team@example.com"])
        monkeypatch.setattr(notify, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(notify, "SMTP_FROM_EMAIL", "cost@example.com")
        monkeypatch.setattr(notify, "SMTP_USE_TLS", True)
        monkeypatch.setattr(notify, "SMTP_USERNAME", "")

        mock_server = MagicMock()
        with patch("notify.smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value = mock_server
            notify.send_price_review_email(ONE_FLAG)
            mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=10)
            mock_server.starttls.assert_called_once()
            mock_server.send_message.assert_called_once()

    def test_smtp_failure_is_swallowed_not_raised(self, monkeypatch):
        monkeypatch.setattr(notify, "NOTIFICATIONS_ENABLED", True)
        monkeypatch.setattr(notify, "PRICE_REVIEW_NOTIFY_EMAILS", ["team@example.com"])
        monkeypatch.setattr(notify, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(notify, "SMTP_FROM_EMAIL", "cost@example.com")
        with patch("notify.smtplib.SMTP", side_effect=ConnectionRefusedError("no server")):
            notify.send_price_review_email(ONE_FLAG)  # must not raise
