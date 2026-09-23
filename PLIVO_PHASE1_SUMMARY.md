# Phase 1: Plivo Cost Monitoring — Implementation Summary

**Branch:** `plivo-max` | **Commit:** `dbc78dd`

---

## Files Delivered

| File | Status | What it does |
|:--|:--|:--|
| `infra/migrations/004_add_plivo_fields.sql` | **NEW** | Adds `cost_status` to `usage_events` + `conversation_id` / `conversation_url` to `calls` |
| `apps/cost-api/app/plivo_cdr_sync.py` | **NEW** | Two-phase sync engine: `store_pending_cdr`, `fetch_cdr` with retry, `run_nightly_plivo_backfill` |
| `apps/cost-api/app/main.py` | **MODIFIED** | `POST /internal/plivo/hangup` endpoint + nightly cron job |
| `apps/cost-api/requirements.txt` | **MODIFIED** | Added `requests>=2.31.0` |
| `apps/cost-api/test_plivo_sync.py` | **NEW** | 9 tests, fully mocked — no DB or Plivo account required |

---

## Tests

```
9 passed in 0.32s
  ✓ test_commits_final_cost_when_total_amount_positive
  ✓ test_schedules_retry_when_amount_is_zero
  ✓ test_does_not_retry_past_max_retries
  ✓ test_skips_when_no_credentials
  ✓ test_inserts_call_and_usage_event
  ✓ test_estimated_cost_calculation
  ✓ test_caller_is_hashed
  ✓ test_resolves_pending_calls
  ✓ test_skips_when_no_credentials (backfill)
```

---

## To Go Live

### 1. Run the migration

```bash
cd infra/docker && docker compose up -d
psql postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger \
  -f ../migrations/004_add_plivo_fields.sql
```

### 2. Populate `.env`

```dotenv
# apps/cost-api/.env
PLIVO_AUTH_ID=<Plivo Console → Account → API Credentials>
PLIVO_AUTH_TOKEN=<Plivo Console → Account → API Credentials>
PLIVO_HANGUP_SECRET=<any strong shared secret>
PLIVO_RATE_PER_MINUTE=<your actual inbound US rate, e.g. 0.0085>
```

### 3. Phase 2 — Plivo Agent Config (separate, cloned agent branch)

In a **cloned** CakeWorldAgent, update the `HANGUP` event callback to:
- Add `X-Actions-Secret: <PLIVO_HANGUP_SECRET>` header.
- Pass `conversation_id` and `conversation_url` in the POST body so Agent Run deep-links are stored.
