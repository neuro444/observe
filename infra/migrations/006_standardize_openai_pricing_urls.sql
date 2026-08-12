-- gpt-4o and gpt-4o-mini pointed at their individual per-model doc pages
-- (developers.openai.com/api/docs/models/...), which turned out to be API
-- reference/code-sample pages, not pricing pages — confirmed by fetching
-- and reading the actual content, not assumed. The general pricing page
-- (already used by gpt-5.4-nano/gpt-5.6-luna/gpt-5-nano) reliably has all
-- 5 models' real rates in one consistent, verified format. Standardizing
-- all OpenAI rows onto it so the weekly price-check parser works for all
-- five instead of silently/loudly failing on two of them.

UPDATE price_book
SET pricing_source_url = 'https://developers.openai.com/api/docs/pricing'
WHERE provider = 'openai' AND model IN ('gpt-4o', 'gpt-4o-mini');
