"""Manual/debugging entry point for the telephony cost poller.

The real, ongoing path is main.py's scheduled job (runs automatically once
TELEPHONY_URL is set) -- this script is for manual runs, local testing, or
catching up a gap. Thin wrapper only; all the actual logic (fetch, event
building, ingestion) lives in app/telephony_poller.py so there's one source
of truth between this script and the scheduled job.

Run: PYTHONPATH="app" python3 scripts/poll_telephony_cost_events.py [--telephony-url URL] [--telephony-api-key KEY] [--once]
"""
from __future__ import annotations

import argparse
import os
import time

from telephony_poller import poll_once

COST_API_URL = "http://127.0.0.1:8000"
COST_INGEST_SECRET = "test-secret-do-not-use-in-prod"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telephony-url", default="http://127.0.0.1:8200")
    parser.add_argument(
        "--telephony-api-key", default=os.getenv("TELEPHONY_API_KEY", ""),
        help="must match telephony's DASHBOARD_API_KEY; defaults to $TELEPHONY_API_KEY",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit (default: every 60s)")
    args = parser.parse_args()

    def run():
        result = poll_once(
            telephony_url=args.telephony_url, cost_api_url=COST_API_URL, secret=COST_INGEST_SECRET,
            telephony_api_key=args.telephony_api_key,
        )
        print(result)

    run()
    if args.once:
        return
    while True:
        time.sleep(60)
        run()


if __name__ == "__main__":
    main()
