-- The new chat_manager/telephony repos default to ELEVENLABS_MODEL_ID=eleven_turbo_v2_5
-- (see telephony/config.py), not eleven_flash_v2 (the old restaurant repo's model).
-- Confirmed on ElevenLabs' real pricing page: "Our Flash/Turbo models include
-- ElevenLabs Flash/Turbo V2 and V2.5" -- same $0.05/1,000-character rate as the
-- existing eleven_flash_v2 row, just a distinct model string that price_book's
-- exact-match lookup needs its own row for.
INSERT INTO price_book (provider, model, billing_unit, flat_rate, effective_from, pricing_source_url, approval_status)
VALUES ('elevenlabs', 'eleven_turbo_v2_5', '1k_characters', 0.05, now(), 'https://elevenlabs.io/pricing/api', 'approved');
