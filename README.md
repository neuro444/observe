# NeuroHeart Internal Platform (`observe`)

Private, internal-only platform for cost monitoring, CDR reconciliation, and telemetry observability across restaurant voice-agent deployments. **Never grant client access to this repository.**

This platform runs independently from client-facing tools (such as restaurant staff dashboards in `voice_central`). It acts as our internal ledger and observability engine.

---

## Repository Layout

```text
apps/
  cost-api/              # FastAPI service: HMAC ingest, Plivo webhooks, CDR sync, review workflows
    app/
      main.py            # Endpoints: /internal/cost-events, /internal/plivo/hangup, calls, reviews
      plivo_cdr_sync.py  # 2-phase CDR sync engine + nightly retry backfill
      anomalies.py       # Automated call anomaly scans (spend spikes, retry loops, abnormal latency)
    test_plivo_sync.py   # Unit test suite for Plivo CDR sync & pricing calculations
    test_ingestion.py    # Unit test suite for cost-events ingestion & HMAC validation
packages/
  cost-engine/           # Decimal pricing engine & price_book lookups against PostgreSQL
infra/
  docker/                # docker-compose for local PostgreSQL (port 5433)
  migrations/            # SQL migrations (001_initial through 004_add_plivo_fields)
plivo/
  src/                   # Reference exported Plivo AI Agent flows (CakeWorldAgent_MY2-config.json)
```

---

## Core Capabilities & Current System State

### 1. Two-Phase Plivo Cost Capture & Reconciliation
Standard carrier hangup webhooks fire instantly, but Plivo’s carrier rating engine takes 10–60+ seconds to settle final CDR costs. Furthermore, Plivo charges separately for the carrier leg and the AI Agent platform fee:

* **Phase A (Immediate - On Hangup):**  
  `POST /internal/plivo/hangup` validates the `X-Actions-Secret` header, hashes the caller phone number for privacy, writes a `pending` row into `usage_events` with an estimated combined rate ($0.0355/min: $0.0055 carrier + $0.0300 agent platform fee), and schedules a delayed fetch via `APScheduler`.
* **Phase B (Delayed Async - ~60s Later):**  
  `fetch_cdr()` queries Plivo's Calls API (`GET /v1/Account/{auth_id}/Call/{call_uuid}/`). When the CDR rates positive, it computes the exact combined cost (`total_amount` + `$0.0300/min × billable_minutes`), marks `cost_status = 'final'`, and updates the call status to `completed`.
* **Exponential Backoff & Nightly Backfill:**  
  If the CDR is still calculating ($0.00), retries trigger with exponential backoff (90s, 180s, 360s; up to 3 attempts). A nightly scheduler sweep (`run_nightly_plivo_backfill`) catches any edge cases or dropped webhooks older than 10 minutes.

### 2. Multi-Provider Financial Ledger
* **Database:** PostgreSQL running with strict `NUMERIC` / Python `Decimal` arithmetic—floating-point types are avoided to ensure zero financial drift over high call volumes.
* **DB-Backed Price Book:** Configurable rate card supporting tokens (cached/uncached/reasoning), characters (TTS), audio seconds (STT), and telephony minutes.
* **Generic Usage Event Ingestion:** `POST /internal/cost-events` receives HMAC-signed event batches from self-hosted or proxy services.

### 3. AI Agent Run & Trace Linking
* Plivo AI Agent session variables (`conversation_id`, `conversation_url`) are mapped to the `calls` table.
* Injected directly from the Plivo Agent Studio canvas via Action Node payloads (`Submit Pickup Request`, `Send Cake Manager Request`, `Send Catering Manager Request`) using `{{Start.call.conversation_id}}`.

---

## Local Setup & Quickstart

### 1. Spin up the Database
The platform runs PostgreSQL isolated on port `5433` (avoiding conflicts with any default Postgres instances):

```bash
cd infra/docker
docker compose up -d
```

Apply migrations:
```bash
# In PowerShell or Bash:
psql postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger -f ../migrations/001_initial_schema.sql
psql postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger -f ../migrations/002_seed_price_book.sql
psql postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger -f ../migrations/003_anomaly_review_uniqueness.sql
psql postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger -f ../migrations/004_add_plivo_fields.sql
```

### 2. Environment Variables
Create or verify `apps/cost-api/.env`:

```dotenv
DATABASE_URL=postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger
COST_INGEST_SECRET=dev_secret_change_me
PLIVO_AUTH_ID=your_plivo_auth_id
PLIVO_AUTH_TOKEN=your_plivo_auth_token
PLIVO_HANGUP_SECRET=your_hangup_webhook_secret
PLIVO_RATE_PER_MINUTE=0.0355
PLIVO_VOICE_AGENT_RATE_PER_MINUTE=0.0300
```

### 3. Install Dependencies & Run Tests
```bash
cd apps/cost-api
pip install -r requirements.txt
pip install -e "../../packages/cost-engine"

# Run the test suite:
python -m pytest test_plivo_sync.py test_ingestion.py -v
```

### 4. Start the Ingestion API
```bash
cd apps/cost-api/app
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Active Branches & Remotes

* **Remote:** `https://github.com/neuro444/observe.git`
* **Feature Branch:** `plivo-max` (Contains the Plivo CDR sync engine, migration 004, agent configuration updates, and automated tests).
