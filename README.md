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
                          # Both the phone line and WhatsApp are instrumented and send
                          # it real traces (client repo's utils/tracing.py), off by
                          # default via TRACING_ENABLED.
infra/
  docker/                # docker-compose for local Postgres (+ Phoenix later)
  migrations/            # SQL schema migrations, plus run_migrations.py — the runner
                          # CI (and local dev) uses to build the schema from scratch
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

## Environment variables

### `apps/cost-api/.env` (see `apps/cost-api/.env.example`)

| Variable | What it's for |
|---|---|
| `DATABASE_URL` | The cost ledger's Postgres connection string. Has a working local default. |
| `COST_INGEST_SECRET` | **Required.** Shared HMAC secret so the client repo's telemetry adapters can prove they're really the real app when sending usage data in. |
| `FIXED_SERVER_COST_MONTHLY_USD` | The flat $28.85/month server cost, spread across each day for the Phase 1 daily total. |
| `ANOMALY_SCAN_HOUR_UTC` | What hour (UTC) the nightly "flag weird calls" scan runs. |
| `ANOMALY_LONG_CALL_SECONDS` / `ANOMALY_HIGH_TOKENS_PER_MINUTE` / `ANOMALY_HIGH_COST_USD` | The thresholds that make a call count as "unusually long / token-heavy / expensive." |
| `PRICE_CHECK_DAY_OF_WEEK` / `PRICE_CHECK_HOUR_UTC` | When the weekly price-reconciliation job runs. |
| `PRICE_CHANGE_THRESHOLD_PERCENT` | Team-confirmed at 30% — below it, a price change auto-applies; above it, a human has to review it. Changing this is a config change now, not a code change. |
| `NOTIFICATIONS_ENABLED` | Off by default. The on/off switch for price-review alert emails. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` | The mail server used to actually send alert emails (works with any provider — Gmail, company email, etc.). |
| `SMTP_FROM_EMAIL` | **Required if `NOTIFICATIONS_ENABLED=true`.** The "from" address on alert emails. |
| `PRICE_REVIEW_NOTIFY_EMAILS` | Comma-separated list of who actually receives the alerts. |

### `observability/phoenix/.env` (see `observability/phoenix/.env.example`)

| Variable | What it's for |
|---|---|
| `PHOENIX_DB_PASSWORD` | Password for Phoenix's own, separate Postgres instance (never the cost ledger's). |
| `PHOENIX_SECRET` | Signs and validates Phoenix's own login tokens/sessions. |
| `PHOENIX_ADMIN_SECRET` | Doubles as a ready-to-use bearer token for the first admin user — also how the `voice-agent-tracing` system API key (below) was generated, via `createSystemApiKey` over Phoenix's GraphQL API. |
| `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` | The initial password for logging into the Phoenix UI as `admin@localhost`. |

### Client repo (`restaurant/voice-ai-ordering-agent`) — tracing settings

Tunables live in `config.py` (this repo's own convention — non-secret settings never go in `.env`); only the real credential goes in `.env`:

| Variable | Where | What it's for |
|---|---|---|
| `TRACING_ENABLED` | `config.py` | Off by default. Turns on sending real call traces to Phoenix. |
| `PHOENIX_COLLECTOR_ENDPOINT` | `config.py` | Where Phoenix is reachable — defaults to `http://127.0.0.1:6006` for local dev. |
| `PHOENIX_API_KEY` | `.env` (real secret) | The system API key generated for this app (`voice-agent-tracing`), used as the bearer token when sending traces to a Phoenix instance with auth enabled. |

## Testing and CI

`.github/workflows/ci.yml` runs on every push/PR: a syntax-error-only lint
pass, then a real test job against a Postgres service container (migrations
applied via `infra/migrations/run_migrations.py`, then the full suite).

66 tests total, covering what previously had zero automated coverage:
- `packages/cost-engine/tests/` — every cost formula, Decimal math, cached-token
  discounting.
- `apps/cost-api/tests/test_anomalies.py` — every anomaly threshold boundary.
- `apps/cost-api/tests/test_notify.py` — email content and every guard clause.
- `apps/cost-api/tests/test_price_check_parsers.py` — every vendor parser, run
  against real fixture snippets captured from each pricing page (not synthetic
  HTML) — see `tests/fixtures/`. The live-fetch behavior itself (`_fetch`) is
  intentionally not covered here; CI never depends on real network access to
  external vendor pages.
- `apps/cost-api/tests/test_ingestion.py` — real Postgres integration tests
  (signing, idempotency, cost calculation, rejection paths).

Run locally: `pip install -e packages/cost-engine && pip install -r
apps/cost-api/requirements-dev.txt`, then `pytest packages/cost-engine/tests
apps/cost-api/tests` against a Postgres with migrations applied.

## What's not built yet

- `cost-dashboard` (the actual UI) — next up.
- Secret scanning, branch protection, CODEOWNERS.

## Client-repo side

The client repo (`restaurant/voice-ai-ordering-agent`) keeps only thin,
provider-neutral telemetry adapters — no pricing, no dashboard, no
vendor-decision logic:
- `plivo_agent/cost_capture.py` — the phone line.
- `utils/whatsapp_cost_capture.py` — WhatsApp (LLM usage + Plivo per-message cost).

Both send raw usage (call id, restaurant id, provider, model, tokens, duration,
latency) to whatever `COST_INGEST_URL` points at — this repo's `cost-api`, once
deployed somewhere reachable.
