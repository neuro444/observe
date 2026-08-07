"""
price_book lookups against the real Postgres database. Pricing is
configuration data, not application logic — see the README and the lead's
comments this package was built from.

psycopg2 maps PostgreSQL NUMERIC columns to Python Decimal automatically, so
every rate returned here is already a Decimal, never a float.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras


class RateNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class PriceBookRate:
    id: int
    provider: str
    model: str
    billing_unit: str
    input_rate: Optional[Decimal]
    cached_input_rate: Optional[Decimal]
    output_rate: Optional[Decimal]
    flat_rate: Optional[Decimal]
    effective_from: datetime
    effective_to: Optional[datetime]


class PriceBookLookup:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _connect(self):
        return psycopg2.connect(self._dsn, cursor_factory=psycopg2.extras.RealDictCursor)

    def get_rate(
        self,
        *,
        provider: str,
        model: str,
        billing_unit: str,
        as_of: Optional[datetime] = None,
    ) -> PriceBookRate:
        """The rate active at `as_of` (default now) for this provider/model/unit.
        Raises RateNotFoundError rather than silently returning zero — an
        unpriced event should be visible as a failure, not counted as free."""
        moment = as_of or datetime.now(timezone.utc)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, provider, model, billing_unit, input_rate,
                       cached_input_rate, output_rate, flat_rate,
                       effective_from, effective_to
                FROM price_book
                WHERE provider = %s AND model = %s AND billing_unit = %s
                  AND effective_from <= %s
                  AND (effective_to IS NULL OR effective_to > %s)
                  AND approval_status = 'approved'
                ORDER BY effective_from DESC
                LIMIT 1
                """,
                (provider, model, billing_unit, moment, moment),
            )
            row = cur.fetchone()
            if row is None:
                raise RateNotFoundError(
                    f"No approved price_book rate for {provider}/{model}/{billing_unit} "
                    f"as of {moment.isoformat()}"
                )
            return PriceBookRate(**row)
