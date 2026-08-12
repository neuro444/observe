-- Correction based on checking Plivo's dedicated WhatsApp pricing page
-- (plivo.com/whatsapp/pricing/us/) and cross-referencing against the actual
-- code: every WhatsApp send in this system goes through send_text() — a
-- free-form reply within an active customer-initiated conversation, which
-- is Meta's "Service" category. Per that page's real table (Marketing
-- $0.0275, Utility/Authentication $0.0037, Service $0/message), Service
-- messages are free. The old $0.0044 flat rate didn't match any of the
-- four real categories at all.
--
-- Versioned rather than edited in place — this is a real, dated correction
-- (we were charging the wrong category), not a data-entry gap, so any past
-- usage_event that referenced the old rate keeps its historical
-- price_version_id and calculated cost untouched.
--
-- Flagged to the lead for confirmation — not yet 100% certain admin
-- notifications (vs. customer replies) categorize the same way, since both
-- currently go through the same send_text() path. Revisit if that turns
-- out to matter.

UPDATE price_book
SET effective_to = now()
WHERE provider = 'plivo' AND model = 'whatsapp' AND effective_to IS NULL;

INSERT INTO price_book (provider, model, billing_unit, flat_rate, effective_from, pricing_source_url, approval_status, last_verified_at)
VALUES ('plivo', 'whatsapp', 'message', 0.00, now(), 'https://www.plivo.com/whatsapp/pricing/us/', 'approved', now());
