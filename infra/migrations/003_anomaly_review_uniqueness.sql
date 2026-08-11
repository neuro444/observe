-- Lets the nightly anomaly scan (apps/cost-api/app/anomalies.py) run every
-- night and use ON CONFLICT DO NOTHING to stay idempotent: a rerun for a date
-- that already has findings must never duplicate rows or clobber a row a
-- human has already reviewed (status/reviewer/notes).

ALTER TABLE daily_call_reviews
    ADD CONSTRAINT uq_daily_call_reviews_date_call_reason
    UNIQUE (review_date, call_id, anomaly_reason);
