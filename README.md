# NeuroHeart Internal Platform

Private, internal-only. Cost monitoring and observability for the restaurant
voice-agent client products. **Never grant client access to this repository.**

This is genuinely separate from any client repository — no nested git repo, no
shared history, no client collaborator access. See `docs/architecture.md` for
the full plan this was built from.

## Layout

```text
apps/
  cost-api/              # HMAC-signed ingestion + read/admin API (FastAPI, Postgres).
                          # Also owns the nightly anomaly scan (in-process
                          # APScheduler job) — daily-review-worker below was the
                          # originally planned separate app, but the scan currently
                          # lives here instead; split out later if it needs to.
  cost-dashboard/        # Internal dashboard UI (not yet built)
  daily-review-worker/   # Not yet its own app — see apps/cost-api note above
packages/
  event-schema/          # Shared usage-event schema/types
  cost-engine/           # Pure cost calculation — Decimal arithmetic, price_book lookups
  provider-catalog/      # Known providers/models/units (not yet built)
observability/
  phoenix/                # Self-hosted Phoenix — running, own Postgres, auth enforced.
                          # Not yet instrumented: bot.py/whatsapp_agent.py don't send it
                          # any trace data yet, so it's live but empty.
infra/
  docker/                # docker-compose for local Postgres (+ Phoenix later)
  migrations/            # SQL schema migrations
```

## What's actually running today

- Postgres via `infra/docker/docker-compose.yml` (real database, not SQLite).
- `apps/cost-api`, with:
  - `POST /internal/cost-events` — the ingestion endpoint, now against real
    Postgres with Decimal money and a DB-backed `price_book` instead of an
    in-code rate table.
  - `GET /internal/calls`, `GET /internal/calls/{call_id}` — per-call cost
    list and drill-down, for the future dashboard's call table.
  - `GET /internal/reviews`, `PATCH /internal/reviews/{id}`,
    `POST /internal/reviews/run` — the nightly anomaly scan (long calls, high
    tokens/minute, high cost), plus marking a flagged call
    reviewed/legitimate/abuse_suspected. Runs automatically every night via an
    in-process scheduler.
  - `GET /internal/costs/daily` — Phase 1's basic daily cost total (phone +
    WhatsApp + a flat prorated server cost), per the lead's simplified ask.
  - `GET /internal/price-checks`, `GET /internal/price-flags`,
    `PATCH /internal/price-flags/{id}`, `POST /internal/prices/reconcile/run`
    — the weekly price-reconciliation job. Checks each vendor's real public
    pricing page (no API key needed — this is published information, not our
    private billing data), auto-applies a normal-looking change, flags
    anything bigger than `PRICE_CHANGE_THRESHOLD_PERCENT` (30%, team-confirmed)
    or unreadable for a human instead of silently applying it. Plivo's
    WhatsApp rate is explicitly `not_checkable` — Meta prices it by message
    category/country, not a stable public number.
  - Email notifications (`notify.py`) when a price gets flagged — off by
    default (`NOTIFICATIONS_ENABLED`), recipients configurable via
    `PRICE_REVIEW_NOTIFY_EMAILS`.
- Both channels in the client repo now report real usage: the phone line
  (`plivo_agent/cost_capture.py`) and WhatsApp
  (`utils/whatsapp_cost_capture.py`, added once we found WhatsApp's actual
  OpenAI call site is `utils/whatsapp_agent.py`, not `utils/brain.py`).
- Both channels also have a `tiktoken` fallback for when the provider doesn't
  report real token usage (`token_source` becomes `tiktoken_estimate`) —
  the phone line's uses `session.transcript_turns` (already captured by
  `bot.py` for the staff dashboard) rather than adding new capture logic to
  the live call pipeline.
- Self-hosted Phoenix (`observability/phoenix/`) — running, own Postgres,
  authentication enforced. Both the phone line (`plivo_agent/server.py`) and
  WhatsApp (`main.py`) are now instrumented with OpenTelemetry
  (`utils/tracing.py` in the client repo) — off by default
  (`TRACING_ENABLED`). Verified with a real span end to end (correct model,
  tokens, latency, real content) against the running Phoenix instance; the
  phone line's coverage is via the same instrumented `openai` SDK class
  Pipecat's `OpenAILLMService` wraps internally (confirmed by reading
  Pipecat's source, not assumed) rather than a separately live-tested phone
  call, since that needs real telephony infra this environment doesn't have.

## Setup still needed (not code — config/access)

- **Email notifications**: code is done and tested, but not actually live —
  needs a real Gmail app password
  ([myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords))
  filled into `apps/cost-api/.env`'s `SMTP_USERNAME` / `SMTP_PASSWORD` /
  `SMTP_FROM_EMAIL`, then `NOTIFICATIONS_ENABLED=true`. Currently off.
- Weekly price check hasn't run on its real automatic schedule yet — only
  tested via manual trigger so far.
- Phoenix has real, working instrumentation but no real trace data yet —
  `TRACING_ENABLED` is off in the client repo until turned on for a real
  deployment.

## What's not built yet

- `cost-dashboard` (the actual UI) — next up.
- CI/CD, secret scanning, branch protection, CODEOWNERS.

## Client-repo side

The client repo (`restaurant/voice-ai-ordering-agent`) keeps only thin,
provider-neutral telemetry adapters — no pricing, no dashboard, no
vendor-decision logic:
- `plivo_agent/cost_capture.py` — the phone line.
- `utils/whatsapp_cost_capture.py` — WhatsApp (LLM usage + Plivo per-message cost).

Both send raw usage (call id, restaurant id, provider, model, tokens, duration,
latency) to whatever `COST_INGEST_URL` points at — this repo's `cost-api`, once
deployed somewhere reachable.
