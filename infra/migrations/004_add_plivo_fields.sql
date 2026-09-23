-- Migration 004: Add Plivo-specific cost tracking fields.
--
-- cost_status on usage_events distinguishes an *estimated* cost (written
-- immediately when the HANGUP webhook fires) from a *final* cost (written
-- when the CDR is fetched ~60 seconds later via Plivo Calls API).
--
-- DEFAULT 'final' is intentional: every existing usage_event (stt/llm/tts)
-- is considered final at insert time.  Only the new Plivo telephony ingest
-- path writes 'pending' rows.

ALTER TABLE usage_events ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'final';

-- Partial index: only 'pending' rows ever need scanning by the nightly cron.
CREATE INDEX idx_usage_events_cost_status
    ON usage_events(cost_status)
    WHERE cost_status = 'pending';

-- conversation_id / conversation_url link calls to Plivo Agent Runs in the
-- Plivo Console.  Supplied via HANGUP webhook body once Phase 2 agent wiring
-- is complete (cloned-agent branch change).  NULL is fine until then.
ALTER TABLE calls ADD COLUMN conversation_id TEXT;
ALTER TABLE calls ADD COLUMN conversation_url TEXT;
