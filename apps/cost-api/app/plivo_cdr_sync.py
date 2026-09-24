"""
Plivo Call Detail Record (CDR) sync module.

Handles the two-phase cost capture pattern required by Plivo's carrier
billing engine:

  Phase A (immediate, on HANGUP webhook):
    The HANGUP callback fires instantly.  We write an *estimated* cost
    (BillDuration ÃƒÆ’Ã¢â‚¬â€ our known Plivo rate from price_book) to usage_events
    with cost_status='pending'.

  Phase B (~60 s later, background):
    fetch_cdr() calls Plivo's GET /Call/{uuid}/ API, which by then contains
    the final total_amount.  We UPDATE the usage_events row to the exact value
    and mark it cost_status='final'.

  Nightly failsafe:
    run_nightly_plivo_backfill() resolves any rows still stuck at 'pending'
    (e.g. server was temporarily unreachable when Phase B fired).

Credentials are read from env vars PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN.
Set them in apps/cost-api/.env ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â never commit the values.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, date, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Optional

import psycopg2
import psycopg2.extras
import requests

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“ injected from env, never hard-coded
# ---------------------------------------------------------------------------
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()

PLIVO_AUTH_ID: str = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN: str = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_VOICE_AGENT_RATE_PER_MINUTE: Decimal = Decimal(os.getenv("PLIVO_VOICE_AGENT_RATE_PER_MINUTE", "0.0300"))
_CDR_URL = "https://api.plivo.com/v1/Account/{auth_id}/Call/{call_uuid}/"

_MAX_RETRIES = 3
# delay in seconds before each retry attempt (indexed by retry_count 1..3)
_RETRY_DELAYS = {1: 90, 2: 180, 3: 360}
_MONEY = Decimal("0.000001")   # six decimal places throughout ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Å“ no floats


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _connect(database_url: str):
    return psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Public: write the immediate 'pending' record on HANGUP
# ---------------------------------------------------------------------------
def store_pending_cdr(
    *,
    call_uuid: str,
    restaurant_id: int,
    caller: str,
    bill_duration: int,
    conversation_id: Optional[str],
    conversation_url: Optional[str],
    estimated_rate_per_minute: Decimal,
    database_url: str,
) -> None:
    """
    Write an estimated, pending usage_events row immediately when the HANGUP
    webhook fires.  The exact amount is back-filled by fetch_cdr().

    estimated_rate_per_minute comes from the price_book row for
    provider='plivo', model='pstn_inbound', billing_unit='minute'.
    """
    billable_minutes = (Decimal(bill_duration) / Decimal("60")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    estimated_cost = (billable_minutes * estimated_rate_per_minute).quantize(
        _MONEY, rounding=ROUND_HALF_UP
    )
    event_id = f"plivo-{call_uuid}"
    now = datetime.now(timezone.utc)

    with _connect(database_url) as conn, conn.cursor() as cur:
        # Upsert the parent calls row.  conversation_id / url may be NULL
        # until Phase 2 agent wiring injects them via the HANGUP payload.
        cur.execute(
            """
            INSERT INTO calls (
                call_id, restaurant_id, started_at, status,
                caller_hash, conversation_id, conversation_url
            )
            VALUES (%s, %s, %s, 'in_progress', %s, %s, %s)
            ON CONFLICT (call_id) DO UPDATE
                SET conversation_id  = COALESCE(EXCLUDED.conversation_id,  calls.conversation_id),
                    conversation_url = COALESCE(EXCLUDED.conversation_url, calls.conversation_url)
            """,
            (
                call_uuid,
                restaurant_id,
                now,
                _hash_caller(caller),
                conversation_id,
                conversation_url,
            ),
        )

        # Idempotent: if the HANGUP fires twice we skip the second write.
        cur.execute(
            """
            INSERT INTO usage_events (
                event_id, call_id, restaurant_id,
                stage, provider, model,
                audio_seconds, billable_minutes, calculated_cost_usd,
                cost_status, occurred_at
            )
            VALUES (%s, %s, %s,
                    'telephony', 'plivo', 'pstn_inbound',
                    %s, %s, %s,
                    'pending', %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                call_uuid,
                restaurant_id,
                Decimal(bill_duration),
                billable_minutes,
                estimated_cost,
                now,
            ),
        )
        conn.commit()

    logger.info(
        "store_pending_cdr: pending record written for %s (est. $%s, %s billed mins)",
        call_uuid, estimated_cost, billable_minutes,
    )


# ---------------------------------------------------------------------------
# Public: async CDR fetch (runs in BackgroundScheduler thread pool)
# ---------------------------------------------------------------------------
def fetch_cdr(
    call_uuid: str,
    database_url: str,
    scheduler: Optional[Any] = None,
    retry_count: int = 0,
) -> None:
    """
    Fetch the finalised CDR from Plivo Calls API and update usage_events.
    Called by APScheduler ~60 s after the HANGUP webhook fires.

    If total_amount is still 0 (Plivo's rating engine hasn't settled yet),
    reschedule with exponential back-off up to _MAX_RETRIES times.
    After that, the nightly CRON backfill handles it.
    """
    if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
        logger.error(
            "fetch_cdr: PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN not configured; "
            "skipping CDR fetch for call %s",
            call_uuid,
        )
        return

    url = _CDR_URL.format(auth_id=PLIVO_AUTH_ID, call_uuid=call_uuid)
    try:
        resp = requests.get(
            url,
            auth=(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN),
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("fetch_cdr: Plivo API error for %s: %s", call_uuid, exc)
        _schedule_retry(call_uuid, database_url, scheduler, retry_count)
        return

    data: dict = resp.json()
    raw_amount: str = data.get("total_amount", "0")
    total_amount = Decimal(raw_amount).quantize(_MONEY, rounding=ROUND_HALF_UP)

    if total_amount <= Decimal("0"):
        logger.info(
            "fetch_cdr: CDR not yet rated for %s (attempt %d of %d)",
            call_uuid, retry_count + 1, _MAX_RETRIES + 1,
        )
        _schedule_retry(call_uuid, database_url, scheduler, retry_count)
        return

    billed_duration = int(data.get("billed_duration", 0))

    # Calculate the Voice Agent runtime fee and add it to the carrier's CDR total_amount
    billable_minutes = (Decimal(billed_duration) / Decimal("60")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    agent_cost = (billable_minutes * PLIVO_VOICE_AGENT_RATE_PER_MINUTE).quantize(_MONEY, rounding=ROUND_HALF_UP)
    actual_total_cost = total_amount + agent_cost

    hangup_cause = data.get("hangup_cause", "")
    logger.info(
        "fetch_cdr: finalising %s â€” Carrier=$%s + Agent=$%s -> Total=$%s, %ds billed, cause=%s",
        call_uuid, total_amount, agent_cost, actual_total_cost, billed_duration, hangup_cause,
    )
    _commit_final_cdr(call_uuid, actual_total_cost, billed_duration, database_url)


def _schedule_retry(
    call_uuid: str,
    database_url: str,
    scheduler: Optional[Any],
    retry_count: int,
) -> None:
    if scheduler is None or retry_count >= _MAX_RETRIES:
        logger.warning(
            "fetch_cdr: max retries reached for %s; nightly CRON will backfill",
            call_uuid,
        )
        return

    next_retry = retry_count + 1
    delay_s = _RETRY_DELAYS.get(next_retry, 360)
    run_date = datetime.now(timezone.utc) + timedelta(seconds=delay_s)
    scheduler.add_job(
        fetch_cdr,
        "date",
        run_date=run_date,
        args=[call_uuid, database_url],
        kwargs={"scheduler": scheduler, "retry_count": next_retry},
        id=f"plivo_cdr_{call_uuid}_retry{next_retry}",
        replace_existing=True,
    )
    logger.info(
        "fetch_cdr: retry %d for %s scheduled in %ds",
        next_retry, call_uuid, delay_s,
    )


def _commit_final_cdr(
    call_uuid: str,
    total_amount: Decimal,
    billed_duration: int,
    database_url: str,
) -> None:
    """
    Overwrite the estimated cost with the CDR-confirmed value and mark the
    usage_events row as 'final'.  Also closes the calls row.
    """
    billable_minutes = (Decimal(billed_duration) / Decimal("60")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE usage_events
            SET calculated_cost_usd = %s,
                audio_seconds        = %s,
                billable_minutes     = %s,
                cost_status          = 'final'
            WHERE call_id    = %s
              AND stage      = 'telephony'
              AND provider   = 'plivo'
              AND cost_status = 'pending'
            """,
            (total_amount, Decimal(billed_duration), billable_minutes, call_uuid),
        )
        cur.execute(
            """
            UPDATE calls
            SET status   = 'completed',
                ended_at = NOW()
            WHERE call_id = %s
              AND status  = 'in_progress'
            """,
            (call_uuid,),
        )
        conn.commit()
    logger.info("fetch_cdr: usage_events and calls committed as final for %s", call_uuid)


# ---------------------------------------------------------------------------
# Public: nightly backfill (triggered by the nightly cron alongside anomaly scan)
# ---------------------------------------------------------------------------
def run_nightly_plivo_backfill(database_url: str) -> int:
    """
    Resolve any usage_events still 'pending' from more than 10 minutes ago.
    Returns the number of calls resolved (or attempted).

    Runs synchronously; the nightly cron doesn't need a scheduler reference
    because it's a one-shot direct fetch for each stale call.
    """
    if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
        logger.error("run_nightly_plivo_backfill: Plivo credentials not set; skipping")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT call_id
            FROM usage_events
            WHERE provider    = 'plivo'
              AND cost_status = 'pending'
              AND occurred_at < %s
            """,
            (cutoff,),
        )
        rows = cur.fetchall()

    stale = [row["call_id"] for row in rows]
    logger.info("run_nightly_plivo_backfill: %d stale pending call(s) found", len(stale))

    for call_uuid in stale:
        # retry_count=_MAX_RETRIES disables further retry scheduling
        fetch_cdr(call_uuid, database_url, scheduler=None, retry_count=_MAX_RETRIES)

    return len(stale)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _hash_caller(raw_number: str) -> str:
    """One-way hash of the caller number ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â never store PII directly."""
    return hashlib.sha256(raw_number.encode()).hexdigest()[:32]
