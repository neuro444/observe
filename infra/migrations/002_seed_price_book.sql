-- Confirmed pricing as of 2026-08-07 (cross-checked against the lead's
-- comments and the earlier verified OpenAI pricing page fetch — both agree).
-- Cached-input rates included this time (were missing from the client-repo
-- in-code rate table).

INSERT INTO price_book (provider, model, billing_unit, input_rate, cached_input_rate, output_rate, effective_from, pricing_source_url, approval_status) VALUES
('openai', 'gpt-4o',       'million_tokens', 2.50, 1.25, 10.00, '2026-01-01', 'https://developers.openai.com/api/docs/models/gpt-4o', 'approved'),
('openai', 'gpt-5.4-nano', 'million_tokens', 0.20, 0.02, 1.25,  '2026-07-29', 'https://developers.openai.com/api/docs/pricing', 'approved'),
('openai', 'gpt-5.6-luna', 'million_tokens', 0.20, 0.02, 1.20,  '2026-07-29', 'https://developers.openai.com/api/docs/pricing', 'approved'),
('openai', 'gpt-4o-mini',  'million_tokens', 0.15, NULL, 0.60,  '2026-01-01', 'https://developers.openai.com/api/docs/models/gpt-4o-mini', 'approved'),
('openai', 'gpt-5-nano',   'million_tokens', 0.05, NULL, 0.40,  '2026-07-29', 'https://developers.openai.com/api/docs/pricing', 'approved');

-- Model names here must match exactly what plivo_agent/cost_capture.py sends
-- (config.py's real defaults), not simplified placeholders — a mismatch means
-- a silent RateNotFoundError on every real event.
INSERT INTO price_book (provider, model, billing_unit, flat_rate, effective_from, pricing_source_url, approval_status) VALUES
('deepgram',   'nova-3-keyterm',   'minute',        0.0061, '2026-01-01', 'https://deepgram.com/pricing', 'approved'),  -- $0.0048 base + $0.0013 keyterm, combined since bot.py always enables keyterm boosting
('elevenlabs', 'eleven_flash_v2',  '1k_characters', 0.05,   '2026-01-01', 'https://elevenlabs.io/pricing/api', 'approved'),  -- matches ELEVENLABS_TTS_MODEL's real default
('plivo',      'voice',            'minute',        0.0028, '2026-01-01', 'https://www.plivo.com/pricing/', 'approved'),
('plivo',      'whatsapp',         'message',       0.0044, '2026-01-01', 'https://www.plivo.com/pricing/', 'approved');

-- Seed restaurant so the ingestion tests below have something real to reference.
INSERT INTO restaurants (name, timezone, monthly_budget) VALUES
('Cake World Eatery', 'America/New_York', 500.00);
