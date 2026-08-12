-- Weekly price-reconciliation support: makes staleness visible (last_verified_at
-- per rate, per the original plan's Section 2c) and gives abnormal-looking
-- changes a place to land for human review, rather than either silently
-- auto-applying something suspicious or requiring a person to check every
-- single week even when nothing changed.

ALTER TABLE price_book ADD COLUMN last_verified_at TIMESTAMPTZ;

-- One row per week's check per provider/model, whether it matched, changed
-- normally, or needs a human look. Keeps a full history, not just current
-- state — so "was this ever actually checked" is always answerable.
CREATE TABLE price_check_runs (
    id                  SERIAL PRIMARY KEY,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    field               TEXT NOT NULL,               -- input_rate | cached_input_rate | output_rate | flat_rate
    old_value           NUMERIC(12, 6),
    new_value           NUMERIC(12, 6),
    outcome             TEXT NOT NULL,                -- unchanged | auto_updated | flagged | check_failed | not_checkable
    reason              TEXT,                          -- why flagged/failed, or null for unchanged/auto_updated
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_price_check_runs_outcome ON price_check_runs(outcome, checked_at);

-- Only rows needing a human decision — a filtered view of price_check_runs
-- where outcome = 'flagged', with a resolution workflow (mirrors
-- daily_call_reviews' status pattern).
CREATE TABLE price_review_flags (
    id                  SERIAL PRIMARY KEY,
    check_run_id        INTEGER NOT NULL REFERENCES price_check_runs(id),
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved
    resolver             TEXT,
    resolution_notes    TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_price_review_flags_status ON price_review_flags(status, created_at);
