"""
Weekly price-reconciliation: re-checks each vendor's PUBLIC pricing page
(no API key needed — this is published information, unlike our own private
usage/billing data, which is what the blocked vendor Admin keys were for)
and compares it against price_book.

Design, per the lead's direction (minimal human intervention) balanced
against the real risk of a webpage misparse silently corrupting a price:
- A normal-looking change (within PRICE_CHANGE_THRESHOLD) auto-applies.
- An abnormal change, or a parse/fetch failure, gets flagged for a human
  instead of applied — the failure mode of "someone checks a page a bit
  later than flagged" is far cheaper than "a wrong number silently prices
  every call from now on."
- price_book is never edited in place for a real rate change: the old row
  is closed (effective_to) and a new one inserted, so every past
  usage_event's price_version_id still points at the exact rate that was
  actually in effect when it happened.

Each parser here was built and verified against the REAL live page content
before being trusted (see commit history) — not guessed at. Deepgram and
Plivo WhatsApp have known limitations, documented on each function.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

PRICE_CHANGE_THRESHOLD = Decimal("0.30")  # 30% — beyond this, flag instead of auto-apply.
# Confirmed with the team (not just an initial guess) — keep this comment in
# sync if that number is ever revisited.
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 15


def _fetch(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception:
        logger.warning("price_check: failed to fetch %s", url, exc_info=True)
        return None


# ── Per-vendor parsers — each tested against real fetched page content ──────

def parse_openai_model_rates(html: str, model: str) -> Optional[dict[str, Decimal]]:
    """OpenAI's pricing page embeds rates as structured data:
    [0,"gpt-4o"],[0,2.5],[0,1.25],[0,10] -> model, input, cached, output.
    Some newer models (gpt-5.4-nano, gpt-5.6-luna) have a 4th number (a
    separate cache-write rate) between cached and output — confirmed by
    reading the real page, not assumed; verified this parser gets the
    right position for both 3- and 4-number rows."""
    pattern = re.escape(model) + r'&quot;\],((?:\[0,(?:[\d.]+|null|&quot;-&quot;)\],?)+)\]'
    m = re.search(pattern, html)
    if not m:
        return None
    values = re.findall(r'\[0,([\d.]+|null|&quot;-&quot;)\]', m.group(1))

    def norm(v: str) -> Optional[Decimal]:
        return None if v in ("null", "&quot;-&quot;") else Decimal(v)

    nums = [norm(v) for v in values]
    if len(nums) == 3:
        input_rate, cached_rate, output_rate = nums
    elif len(nums) == 4:
        input_rate, cached_rate, _extra, output_rate = nums
    else:
        return None
    return {"input_rate": input_rate, "cached_input_rate": cached_rate, "output_rate": output_rate}


def parse_deepgram_nova3_keyterm_rate(html: str) -> Optional[Decimal]:
    """Deepgram embeds pricing as schema.org Offer markup with clear labels —
    "Nova-3 Monolingual - Pay As You Go" (base) + "Keyterm Prompting - Pay As
    You Go" (boost). Our stored rate is the sum, since bot.py always enables
    keyterm boosting — see price_book seed data comment."""

    def offer(name: str) -> Optional[Decimal]:
        m = re.search(r'"name":"' + re.escape(name) + r'","price":"([\d.]+)"', html)
        return Decimal(m.group(1)) if m else None

    base = offer("Deepgram Voice AI Platform Pricing - Streaming - Nova-3 Monolingual - Pay As You Go")
    keyterm = offer("Deepgram Voice AI Platform Pricing - Streaming - Keyterm Prompting - Pay As You Go")
    if base is None or keyterm is None:
        return None
    return base + keyterm


def parse_elevenlabs_flash_rate(html: str) -> Optional[Decimal]:
    """Confirmed present as a plain sentence on the page, not structured
    data: "Text to Speech $0.10 per 1,000 characters (Multilingual v2/v3)
    or $0.05 (Flash/Turbo)." — matches ELEVENLABS_TTS_MODEL's real default
    (eleven_flash_v2, a Flash-family model)."""
    m = re.search(
        r"Text to Speech \$[\d.]+ per 1,000 characters \(Multilingual v2/v3\) or \$([\d.]+) \(Flash/Turbo\)",
        html,
    )
    return Decimal(m.group(1)) if m else None


def parse_plivo_inbound_voice_rate(html: str) -> Optional[Decimal]:
    """The page shows Outbound/Inbound side by side under "Calls start at" —
    our rate is Inbound, since customers call in to the restaurant, not the
    other way around. Confirmed by reading the surrounding table structure,
    not just matching the number."""
    m = re.search(
        r'Calls start at.*?Outbound.*?\$[\d.]+/min.*?Inbound.*?\$([\d.]+)/min',
        html,
    ) or re.search(
        r'font-semibold text-foreground">\$[\d.]+/min</span></td>'
        r'<td class="py-2\.5 text-muted-foreground"><span class="font-semibold text-foreground">\$([\d.]+)/min',
        html,
    )
    return Decimal(m.group(1)) if m else None


def parse_plivo_whatsapp_rate(html: str) -> Optional[Decimal]:
    """Known limitation, confirmed by checking the real page: WhatsApp
    Business messaging pricing isn't a simple stable number on this page —
    Meta tiers it by message category and country. Returns None always;
    kept as its own function (rather than silently omitted) so it shows up
    explicitly as 'not_checkable' in the check log, not as a missing case
    nobody noticed."""
    return None


@dataclass(frozen=True)
class PriceBookRow:
    id: int
    provider: str
    model: str
    billing_unit: str
    input_rate: Optional[Decimal]
    cached_input_rate: Optional[Decimal]
    output_rate: Optional[Decimal]
    flat_rate: Optional[Decimal]
    pricing_source_url: Optional[str]


def _live_rates_for(row: PriceBookRow) -> tuple[Optional[dict[str, Optional[Decimal]]], str]:
    """Returns (field->live_value dict, outcome_if_none) for one price_book row."""
    if row.pricing_source_url is None:
        return None, "not_checkable"
    html = _fetch(row.pricing_source_url)
    if html is None:
        return None, "check_failed"

    if row.provider == "openai":
        rates = parse_openai_model_rates(html, row.model)
        if rates is None:
            return None, "check_failed"
        return rates, ""
    if row.provider == "deepgram":
        rate = parse_deepgram_nova3_keyterm_rate(html)
        return ({"flat_rate": rate}, "") if rate is not None else (None, "check_failed")
    if row.provider == "elevenlabs":
        rate = parse_elevenlabs_flash_rate(html)
        return ({"flat_rate": rate}, "") if rate is not None else (None, "check_failed")
    if row.provider == "plivo" and row.model == "voice":
        rate = parse_plivo_inbound_voice_rate(html)
        return ({"flat_rate": rate}, "") if rate is not None else (None, "check_failed")
    if row.provider == "plivo" and row.model == "whatsapp":
        return None, "not_checkable"
    return None, "not_checkable"


def _percent_change(old: Decimal, new: Decimal) -> Decimal:
    if old == 0:
        return Decimal("1") if new != 0 else Decimal("0")
    return abs(new - old) / abs(old)


def reconcile_prices(dsn: str) -> dict[str, int]:
    """Runs the weekly check across every active price_book row. Returns a
    summary count by outcome. Safe to run repeatedly — each run is its own
    price_check_runs entry, and auto-updates always version (never edit in
    place), so re-running never double-applies or loses history."""
    counts = {"unchanged": 0, "auto_updated": 0, "flagged": 0, "check_failed": 0, "not_checkable": 0}

    with psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, provider, model, billing_unit, input_rate, cached_input_rate,
                   output_rate, flat_rate, pricing_source_url
            FROM price_book
            WHERE effective_to IS NULL AND approval_status = 'approved'
            """
        )
        rows = [PriceBookRow(**r) for r in cur.fetchall()]

        for row in rows:
            live, none_outcome = _live_rates_for(row)
            now = datetime.now(timezone.utc)
            if live is None:
                counts[none_outcome] += 1
                cur.execute(
                    """
                    INSERT INTO price_check_runs (provider, model, field, old_value, new_value, outcome, reason, checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (row.provider, row.model, "all", None, None, none_outcome,
                     "no pricing_source_url" if none_outcome == "not_checkable" else "fetch/parse failed", now),
                )
                continue

            row_had_a_flag = False
            for field, live_value in live.items():
                old_value = getattr(row, field)
                if live_value is None or old_value is None:
                    continue  # field not applicable to this row (e.g. cached_input_rate on a flat-rate row)
                if old_value == live_value:
                    outcome, reason = "unchanged", None
                else:
                    change = _percent_change(old_value, live_value)
                    if change <= PRICE_CHANGE_THRESHOLD:
                        outcome, reason = "auto_updated", None
                    else:
                        outcome, reason = "flagged", f"{change*100:.0f}% change exceeds {PRICE_CHANGE_THRESHOLD*100:.0f}% threshold"
                        row_had_a_flag = True

                counts[outcome] += 1
                cur.execute(
                    """
                    INSERT INTO price_check_runs (provider, model, field, old_value, new_value, outcome, reason, checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (row.provider, row.model, field, old_value, live_value, outcome, reason, now),
                )
                run_id = cur.fetchone()["id"]

                if outcome == "flagged":
                    cur.execute(
                        "INSERT INTO price_review_flags (check_run_id) VALUES (%s)",
                        (run_id,),
                    )
                elif outcome == "auto_updated":
                    _apply_auto_update(cur, row, field, live_value, now)

            if not row_had_a_flag:
                cur.execute("UPDATE price_book SET last_verified_at = %s WHERE id = %s", (now, row.id))

        conn.commit()
    return counts


def _apply_auto_update(cur, row: PriceBookRow, field: str, new_value: Decimal, now: datetime) -> None:
    """Never edits price_book in place for a real value change — closes the
    old row and inserts a new one, so every past usage_event's
    price_version_id keeps pointing at the rate that was actually in effect
    when that event happened."""
    cur.execute("UPDATE price_book SET effective_to = %s WHERE id = %s", (now, row.id))
    new_values = {
        "input_rate": row.input_rate, "cached_input_rate": row.cached_input_rate,
        "output_rate": row.output_rate, "flat_rate": row.flat_rate,
    }
    new_values[field] = new_value
    cur.execute(
        """
        INSERT INTO price_book (provider, model, billing_unit, input_rate, cached_input_rate,
                                 output_rate, flat_rate, effective_from, pricing_source_url,
                                 approval_status, last_verified_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)
        """,
        (row.provider, row.model, row.billing_unit, new_values["input_rate"], new_values["cached_input_rate"],
         new_values["output_rate"], new_values["flat_rate"], now, row.pricing_source_url, now),
    )
