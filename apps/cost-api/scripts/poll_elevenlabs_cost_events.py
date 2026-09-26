"""Manual/debugging entry point for the ElevenLabs (11agent_repo) cost poller.

The real, ongoing path is main.py's scheduled job (runs automatically once
ELEVENLABS_AGENT_URL is set) -- this script is for manual runs, local testing,
or catching up a gap. Thin wrapper only; all the actual logic (fetch, event
building, ingestion) lives in app/elevenlabs_poller.py so there's one source
of truth between this script and the scheduled job.

Run: PYTHONPATH="app" python3 scripts/poll_elevenlabs_cost_events.py [--elevenlabs-url URL] [--elevenlabs-api-key KEY] [--once]
"""
from __future__ import annotations

import argparse
import os
import time

from elevenlabs_poller import poll_once

COST_API_URL = "http://127.0.0.1:8000"
COST_INGEST_SECRET = "test-secret-do-not-use-in-prod"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--elevenlabs-url", default="https://cakeworld.neuroheart.ai/elevenlabs-agent")
    parser.add_argument(
        "--elevenlabs-api-key", default=os.getenv("ELEVENLABS_AGENT_API_KEY", ""),
        help="must match 11agent_repo's ELEVENLABS_AGENT_API_KEY; defaults to $ELEVENLABS_AGENT_API_KEY",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit (default: every 60s)")
    args = parser.parse_args()

    def run():
        result = poll_once(
            elevenlabs_url=args.elevenlabs_url, cost_api_url=COST_API_URL, secret=COST_INGEST_SECRET,
            elevenlabs_api_key=args.elevenlabs_api_key,
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
