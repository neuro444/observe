-- Data-entry correction, not a real price change: gpt-4o-mini's cached_input_rate
-- was never set (NULL) even though OpenAI has always priced it at $0.075/M —
-- confirmed by reading the live pricing page directly (developers.openai.com/api/docs/pricing,
-- embedded pricing table: gpt-4o-mini -> [0.15, 0.075, 0.6]), 2026-08-12.
-- Corrected in place rather than versioned as a new effective-dated row, since
-- this was always the real rate — we just never recorded it, not a market change.

UPDATE price_book
SET cached_input_rate = 0.075
WHERE provider = 'openai' AND model = 'gpt-4o-mini' AND cached_input_rate IS NULL;
