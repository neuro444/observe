# NeuroHeart Internal Platform

Private, internal-only. Cost monitoring and observability for the restaurant
voice-agent client products. **Never grant client access to this repository.**

This is genuinely separate from any client repository — no nested git repo, no
shared history, no client collaborator access. See `docs/architecture.md` for
the full plan this was built from.

## Layout

```text
apps/
  cost-api/              # HMAC-signed ingestion + admin API (FastAPI, Postgres)
  cost-dashboard/        # Internal dashboard UI (not yet built)
  daily-review-worker/   # Nightly anomaly/review snapshot job (not yet built)
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
- `apps/cost-api` — the ingestion endpoint, ported from the original client-repo
  proof of concept, now against real Postgres with Decimal money and a
  DB-backed `price_book` instead of an in-code rate table.

## What's not built yet (needs infra/access decisions, not just code)

- Actual private GitHub organization + repo — this only exists locally until
  that's created and this gets pushed there.
- Phoenix deployment for the observability plane.
- `cost-dashboard`, `daily-review-worker`.
- CI/CD, secret scanning, branch protection, CODEOWNERS.

## Client-repo side

The client repo (`restaurant/voice-ai-ordering-agent/plivo_agent`) keeps only a
thin, provider-neutral telemetry adapter (`cost_capture.py`) — no pricing, no
dashboard, no vendor-decision logic. It sends raw usage
(call id, restaurant id, provider, model, tokens, duration, latency) to whatever
`COST_INGEST_URL` points at — this repo's `cost-api`, once deployed somewhere
reachable.
