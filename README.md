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
  phoenix/                # OpenTelemetry + Phoenix self-hosting (not yet configured — infra step)
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
- Both channels in the client repo now report real usage: the phone line
  (`plivo_agent/cost_capture.py`) and WhatsApp
  (`utils/whatsapp_cost_capture.py`, added once we found WhatsApp's actual
  OpenAI call site is `utils/whatsapp_agent.py`, not `utils/brain.py`).

## What's not built yet (needs infra/access decisions, not just code)

- Phoenix deployment for the observability plane — still an open decision
  with the lead, not just a build task.
- `cost-dashboard` (the actual UI) — next up.
- A `tiktoken`-based fallback for when a provider doesn't report token usage
  (`token_source` is currently always `provider_reported`).
- Weekly reconciliation against real vendor invoices — blocked on getting an
  OpenAI Admin-tier API key.
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
