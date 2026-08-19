"""Tests for price_check.py's parsers -- run against real fixture snippets
captured from each vendor's live pricing page (see tests/fixtures/), not
synthetic HTML. No network access in CI; the fetching (_fetch) itself is
untested here on purpose -- only the parsing, which is the part a page
change could silently break."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from price_check import (  # noqa: E402
    _percent_change,
    parse_deepgram_nova3_keyterm_rate,
    parse_elevenlabs_flash_rate,
    parse_openai_model_rates,
    parse_plivo_inbound_voice_rate,
    parse_plivo_whatsapp_rate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseOpenaiModelRates:
    def test_three_number_row(self):
        rates = parse_openai_model_rates(_read("openai_gpt4o_snippet.html"), "gpt-4o")
        assert rates == {
            "input_rate": Decimal("2.5"),
            "cached_input_rate": Decimal("1.25"),
            "output_rate": Decimal("10"),
        }

    def test_four_number_row_skips_the_extra_cache_write_rate(self):
        rates = parse_openai_model_rates(_read("openai_gpt56luna_snippet.html"), "gpt-5.6-luna")
        assert rates == {
            "input_rate": Decimal("0.2"),
            "cached_input_rate": Decimal("0.02"),
            "output_rate": Decimal("1.2"),
        }

    def test_model_not_present_returns_none(self):
        assert parse_openai_model_rates(_read("openai_gpt4o_snippet.html"), "not-a-real-model") is None

    def test_malformed_html_returns_none_not_a_crash(self):
        assert parse_openai_model_rates("<html>nothing here</html>", "gpt-4o") is None


class TestParseDeepgram:
    def test_sums_base_and_keyterm_offers(self):
        rate = parse_deepgram_nova3_keyterm_rate(_read("deepgram_snippet.html"))
        assert rate == Decimal("0.0061")  # 0.0048 base + 0.0013 keyterm

    def test_missing_offer_returns_none(self):
        assert parse_deepgram_nova3_keyterm_rate("<html>nothing here</html>") is None


class TestParseElevenlabs:
    def test_extracts_flash_rate(self):
        rate = parse_elevenlabs_flash_rate(_read("elevenlabs_snippet.html"))
        assert rate == Decimal("0.05")

    def test_missing_sentence_returns_none(self):
        assert parse_elevenlabs_flash_rate("<html>nothing here</html>") is None


class TestParsePlivoVoice:
    def test_extracts_inbound_rate(self):
        rate = parse_plivo_inbound_voice_rate(_read("plivo_voice_snippet.html"))
        assert rate == Decimal("0.0028")

    def test_missing_table_returns_none(self):
        assert parse_plivo_inbound_voice_rate("<html>nothing here</html>") is None


class TestParsePlivoWhatsapp:
    def test_always_returns_none_documented_limitation(self):
        # Meta prices WhatsApp by message category/country, not a stable
        # public number -- this parser intentionally never resolves a rate.
        assert parse_plivo_whatsapp_rate("<html>anything at all</html>") is None
        assert parse_plivo_whatsapp_rate("") is None


class TestPercentChange:
    def test_no_change(self):
        assert _percent_change(Decimal("2.5"), Decimal("2.5")) == Decimal("0")

    def test_increase(self):
        assert _percent_change(Decimal("2.5"), Decimal("5.0")) == Decimal("1")  # 100% increase

    def test_decrease(self):
        assert _percent_change(Decimal("10"), Decimal("5")) == Decimal("0.5")

    def test_change_is_always_positive_magnitude(self):
        up = _percent_change(Decimal("10"), Decimal("15"))
        down = _percent_change(Decimal("15"), Decimal("10"))
        assert up > 0 and down > 0

    def test_old_zero_new_nonzero_is_full_change(self):
        assert _percent_change(Decimal("0"), Decimal("5")) == Decimal("1")

    def test_old_and_new_both_zero_is_no_change(self):
        assert _percent_change(Decimal("0"), Decimal("0")) == Decimal("0")
