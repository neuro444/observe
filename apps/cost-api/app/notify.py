"""
Email notifications for price-review flags — closes the gap where a flagged
item sits in the database with no one actually finding out about it.

Off by default (NOTIFICATIONS_ENABLED), same safety pattern as the rest of
this system: a failure here must never break the reconciliation job itself,
only ever get logged and swallowed.

Uses plain SMTP (smtplib) so it works with any provider — company mail
server, Gmail, etc. — no new third-party service required.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import date
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

NOTIFICATIONS_ENABLED = os.getenv("NOTIFICATIONS_ENABLED", "false").strip().lower() not in {"false", "0", "no", ""}
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() not in {"false", "0", "no"}
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
# Comma-separated — configurable recipient list, not hardcoded.
PRICE_REVIEW_NOTIFY_EMAILS = [
    e.strip() for e in os.getenv("PRICE_REVIEW_NOTIFY_EMAILS", "").split(",") if e.strip()
]


def _build_message(flags: list[dict[str, Any]]) -> EmailMessage:
    count = len(flags)
    subject = f"[Cost Monitoring] {count} price change{'s' if count != 1 else ''} {'need' if count != 1 else 'needs'} review"

    lines = [
        f"The weekly price check found {count} change{'s' if count != 1 else ''} it didn't apply "
        "automatically — each one is either a bigger jump than usual, or a page it couldn't read.",
        "",
    ]
    for f in flags:
        old = f.get("old_value")
        new = f.get("new_value")
        change = f"{old} -> {new}" if old is not None and new is not None else "could not read a value"
        lines.append(f"- {f['provider']} / {f['model']} ({f['field']}): {change}")
        lines.append(f"  Reason: {f.get('reason') or 'n/a'}")
    lines += ["", "Review and resolve at GET/PATCH /internal/price-flags."]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = ", ".join(PRICE_REVIEW_NOTIFY_EMAILS)
    msg.set_content("\n".join(lines))
    return msg


def send_price_review_email(flags: list[dict[str, Any]]) -> None:
    """Sends one summary email for every flag created in a single
    reconciliation run — never one email per flag, so a bad run doesn't
    spam the team. Silently does nothing if notifications are off, no
    recipients are configured, or there's nothing to report."""
    if not NOTIFICATIONS_ENABLED:
        return
    if not flags:
        return
    if not PRICE_REVIEW_NOTIFY_EMAILS:
        logger.warning("notify: NOTIFICATIONS_ENABLED but PRICE_REVIEW_NOTIFY_EMAILS is empty; skipping")
        return
    if not SMTP_HOST or not SMTP_FROM_EMAIL:
        logger.warning("notify: NOTIFICATIONS_ENABLED but SMTP_HOST/SMTP_FROM_EMAIL not configured; skipping")
        return

    try:
        msg = _build_message(flags)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("notify: sent price-review email for %d flag(s)", len(flags))
    except Exception:
        logger.warning("notify: failed to send price-review email (non-fatal)", exc_info=True)
