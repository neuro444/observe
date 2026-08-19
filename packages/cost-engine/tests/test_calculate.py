"""Pure-function tests for cost_engine.calculate — no DB, no network.
Every formula is exercised directly against hand-built PriceBookRate
fixtures so the Decimal math itself is verified independent of any lookup."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cost_engine.calculate import (
    calculate_cost,
    llm_cost,
    stt_cost,
    telephony_cost,
    tts_cost,
)
from cost_engine.rates import PriceBookLookup, PriceBookRate, RateNotFoundError


def make_rate(**overrides) -> PriceBookRate:
    defaults = dict(
        id=1, provider="openai", model="gpt-4o", billing_unit="million_tokens",
        input_rate=Decimal("2.5"), cached_input_rate=Decimal("1.25"), output_rate=Decimal("10"),
        flat_rate=None, effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc), effective_to=None,
    )
    defaults.update(overrides)
    return PriceBookRate(**defaults)


class TestLlmCost:
    def test_basic_uncached(self):
        rate = make_rate()
        cost = llm_cost(rate, input_tokens=1_000_000, cached_input_tokens=0, output_tokens=1_000_000)
        assert cost == Decimal("12.500000")

    def test_cached_tokens_use_cheaper_rate(self):
        rate = make_rate()
        cost = llm_cost(rate, input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=0)
        # 500k uncached @ 2.5/M + 500k cached @ 1.25/M
        assert cost == Decimal("1.875000")

    def test_cached_tokens_not_double_counted_against_total_input(self):
        rate = make_rate()
        # cached_input_tokens must be a subset of input_tokens, not additional.
        all_cached = llm_cost(rate, input_tokens=1000, cached_input_tokens=1000, output_tokens=0)
        assert all_cached == Decimal("0.001250")  # 1000 * 1.25/1M, zero uncached

    def test_cached_tokens_without_cached_rate_billed_at_full_input_rate(self):
        rate = make_rate(cached_input_rate=None)
        cost = llm_cost(rate, input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=0)
        # No cached rate on file -> all 1M tokens effectively billed: 500k uncached + 500k at full rate
        assert cost == Decimal("2.500000")

    def test_missing_input_rate_raises(self):
        rate = make_rate(input_rate=None)
        with pytest.raises(ValueError):
            llm_cost(rate, input_tokens=100, output_tokens=100)

    def test_missing_output_rate_raises(self):
        rate = make_rate(output_rate=None)
        with pytest.raises(ValueError):
            llm_cost(rate, input_tokens=100, output_tokens=100)

    def test_uncached_never_goes_negative(self):
        # cached_input_tokens larger than input_tokens shouldn't underflow.
        rate = make_rate()
        cost = llm_cost(rate, input_tokens=100, cached_input_tokens=500, output_tokens=0)
        assert cost >= Decimal("0")


class TestSttCost:
    def test_basic(self):
        rate = make_rate(flat_rate=Decimal("0.0061"))
        cost = stt_cost(rate, audio_seconds=Decimal("120"))
        assert cost == Decimal("0.012200")  # 2 minutes * 0.0061

    def test_missing_flat_rate_raises(self):
        rate = make_rate(flat_rate=None)
        with pytest.raises(ValueError):
            stt_cost(rate, audio_seconds=Decimal("60"))

    def test_zero_seconds_is_zero_cost(self):
        rate = make_rate(flat_rate=Decimal("0.0061"))
        assert stt_cost(rate, audio_seconds=Decimal("0")) == Decimal("0.000000")


class TestTtsCost:
    def test_basic(self):
        rate = make_rate(flat_rate=Decimal("0.05"))
        cost = tts_cost(rate, characters=2000)
        assert cost == Decimal("0.100000")  # 2 * 0.05

    def test_missing_flat_rate_raises(self):
        rate = make_rate(flat_rate=None)
        with pytest.raises(ValueError):
            tts_cost(rate, characters=1000)


class TestTelephonyCost:
    def test_voice_per_minute(self):
        rate = make_rate(flat_rate=Decimal("0.0028"))
        cost = telephony_cost(rate, minutes=Decimal("3.5"))
        assert cost == Decimal("0.009800")

    def test_whatsapp_message_free_tier(self):
        rate = make_rate(flat_rate=Decimal("0"))
        cost = telephony_cost(rate, minutes=Decimal("1"))
        assert cost == Decimal("0.000000")

    def test_missing_flat_rate_raises(self):
        rate = make_rate(flat_rate=None)
        with pytest.raises(ValueError):
            telephony_cost(rate, minutes=Decimal("1"))


class _FakeLookup(PriceBookLookup):
    """Bypasses the real DB connection — returns a fixed rate for dispatch tests."""

    def __init__(self, rate: PriceBookRate):
        self._rate = rate

    def get_rate(self, *, provider, model, billing_unit, as_of=None):
        return self._rate


class TestCalculateCostDispatch:
    def test_llm_stage_dispatches_to_llm_cost(self):
        lookup = _FakeLookup(make_rate())
        cost, rate_id = calculate_cost(
            lookup, stage="llm", provider="openai", model="gpt-4o", billing_unit="million_tokens",
            input_tokens=1_000_000, output_tokens=0,
        )
        assert cost == Decimal("2.500000")
        assert rate_id == 1

    def test_stt_stage_dispatches_to_stt_cost(self):
        lookup = _FakeLookup(make_rate(flat_rate=Decimal("0.0061")))
        cost, _ = calculate_cost(
            lookup, stage="stt", provider="deepgram", model="nova-3-keyterm", billing_unit="minute",
            audio_seconds=Decimal("60"),
        )
        assert cost == Decimal("0.006100")

    def test_unknown_stage_raises(self):
        lookup = _FakeLookup(make_rate())
        with pytest.raises(ValueError):
            calculate_cost(lookup, stage="not_a_real_stage", provider="openai", model="gpt-4o", billing_unit="x")

    def test_rate_not_found_propagates(self):
        class _RaisingLookup(PriceBookLookup):
            def __init__(self):
                pass

            def get_rate(self, **kwargs):
                raise RateNotFoundError("no rate")

        with pytest.raises(RateNotFoundError):
            calculate_cost(_RaisingLookup(), stage="llm", provider="x", model="y", billing_unit="z")
