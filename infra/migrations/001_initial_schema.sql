-- NeuroHeart internal cost ledger — initial schema.
-- Money is NUMERIC throughout, never FLOAT/DOUBLE — per the lead's explicit
-- correction (floating point can silently drift on financial data over many
-- transactions). All timestamps are TIMESTAMPTZ (UTC).

CREATE TABLE restaurants (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'America/New_York',
    monthly_budget  NUMERIC(12, 2),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE calls (
    call_id         TEXT PRIMARY KEY,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    total_duration_s NUMERIC(10, 2),
    status          TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | completed | dropped
    caller_hash     TEXT,  -- hashed, never the raw phone number — see security requirements
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_calls_restaurant_id ON calls(restaurant_id);
CREATE INDEX idx_calls_started_at ON calls(started_at);

-- price_book: the DB-backed, effective-dated rate card. Replaces the in-code
-- rate table from the client-repo proof of concept — pricing is configuration,
-- not application logic, per the lead's correction.
CREATE TABLE price_book (
    id                  SERIAL PRIMARY KEY,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,          -- model/SKU
    billing_unit        TEXT NOT NULL,           -- minute | 1k_characters | million_tokens | message
    input_rate          NUMERIC(12, 6),
    cached_input_rate   NUMERIC(12, 6),
    output_rate         NUMERIC(12, 6),
    flat_rate           NUMERIC(12, 6),          -- for non-token units (minute, message, 1k_characters)
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,             -- NULL = still active
    pricing_source_url  TEXT,
    approval_status     TEXT NOT NULL DEFAULT 'approved',  -- pending | approved | rejected
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_price_book_lookup ON price_book(provider, model, billing_unit, effective_from);

-- provider_config_history: audits when a restaurant was actually using a given
-- provider/model, independent of price_book — supports "what would this have
-- cost with a different provider" comparisons and historical reconciliation.
CREATE TABLE provider_config_history (
    id              SERIAL PRIMARY KEY,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    service_type    TEXT NOT NULL,   -- stt | llm | tts | telephony
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    activated_at    TIMESTAMPTZ NOT NULL,
    deactivated_at  TIMESTAMPTZ
);
CREATE INDEX idx_provider_config_restaurant ON provider_config_history(restaurant_id, service_type);

CREATE TABLE usage_events (
    event_id                TEXT PRIMARY KEY,   -- idempotency key
    call_id                 TEXT REFERENCES calls(call_id),
    restaurant_id           INTEGER NOT NULL REFERENCES restaurants(id),
    stage                   TEXT NOT NULL,        -- stt | llm | tts | telephony
    provider                TEXT NOT NULL,
    model                   TEXT,
    input_tokens            INTEGER DEFAULT 0,
    cached_input_tokens     INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    reasoning_tokens        INTEGER DEFAULT 0,
    characters              INTEGER DEFAULT 0,
    audio_seconds           NUMERIC(10, 3) DEFAULT 0,
    billable_minutes        NUMERIC(10, 4) DEFAULT 0,
    latency_ms              INTEGER,
    token_source             TEXT NOT NULL DEFAULT 'provider_reported',  -- provider_reported | tiktoken_estimate
    price_version_id        INTEGER REFERENCES price_book(id),
    calculated_cost_usd     NUMERIC(14, 6) NOT NULL,
    had_empty_response      BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at             TIMESTAMPTZ NOT NULL,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_usage_events_restaurant ON usage_events(restaurant_id, occurred_at);
CREATE INDEX idx_usage_events_call ON usage_events(call_id);
CREATE INDEX idx_usage_events_provider ON usage_events(provider, model);

CREATE TABLE daily_call_reviews (
    id              SERIAL PRIMARY KEY,
    review_date     DATE NOT NULL,
    call_id         TEXT NOT NULL REFERENCES calls(call_id),
    anomaly_reason  TEXT NOT NULL,   -- long_call | high_tokens_per_minute | high_cost | repeated_caller | excessive_retries | tts_llm_mismatch | spend_spike
    severity        TEXT NOT NULL DEFAULT 'warning',  -- info | warning | critical
    status          TEXT NOT NULL DEFAULT 'follow_up_required',  -- reviewed | legitimate | abuse_suspected | follow_up_required
    reviewer        TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_daily_reviews_date ON daily_call_reviews(review_date, status);
