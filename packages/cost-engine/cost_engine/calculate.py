"""
Pure cost-calculation formulas, Decimal throughout — never float, per the
lead's explicit correction (floating point can silently drift on money across
many transactions).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .rates import PriceBookLookup, PriceBookRate

_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")
_SIXTY = Decimal("60")
_MONEY_PRECISION = Decimal("0.000001")  # six decimal places — sub-cent precision matters at scale


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_PRECISION, rounding=ROUND_HALF_UP)


def llm_cost(
    rate: PriceBookRate,
    *,
    input_tokens: int,
    cached_input_tokens: int = 0,
    output_tokens: int,
) -> Decimal:
    """LLM cost = (input/1M * input_rate) + (cached/1M * cached_rate) + (output/1M * output_rate).
    Uncached input tokens are billed at the full input rate; only the reported
    cached_input_tokens count gets the (cheaper) cached rate — they are not
    double-counted against total input."""
    if rate.input_rate is None or rate.output_rate is None:
        raise ValueError(f"price_book rate {rate.id} is missing input/output rate for an LLM calculation")
    uncached_input = max(input_tokens - cached_input_tokens, 0)
    cost = (Decimal(uncached_input) / _MILLION) * rate.input_rate
    cost += (Decimal(output_tokens) / _MILLION) * rate.output_rate
    if cached_input_tokens and rate.cached_input_rate is not None:
        cost += (Decimal(cached_input_tokens) / _MILLION) * rate.cached_input_rate
    elif cached_input_tokens:
        # No cached rate on file — bill cached tokens at the full input rate
        # rather than silently treating them as free.
        cost += (Decimal(cached_input_tokens) / _MILLION) * rate.input_rate
    return _quantize(cost)


def stt_cost(rate: PriceBookRate, *, audio_seconds: Decimal) -> Decimal:
    """STT cost = audio_seconds / 60 * per_minute_rate."""
    if rate.flat_rate is None:
        raise ValueError(f"price_book rate {rate.id} is missing a flat_rate for an STT calculation")
    minutes = audio_seconds / _SIXTY
    return _quantize(minutes * rate.flat_rate)


def tts_cost(rate: PriceBookRate, *, characters: int) -> Decimal:
    """TTS cost = characters / 1000 * per_1k_character_rate."""
    if rate.flat_rate is None:
        raise ValueError(f"price_book rate {rate.id} is missing a flat_rate for a TTS calculation")
    return _quantize((Decimal(characters) / _THOUSAND) * rate.flat_rate)


def telephony_cost(rate: PriceBookRate, *, minutes: Decimal) -> Decimal:
    """Telephony cost = minutes * per_minute_rate (voice) or per-message flat_rate (WhatsApp)."""
    if rate.flat_rate is None:
        raise ValueError(f"price_book rate {rate.id} is missing a flat_rate for a telephony calculation")
    return _quantize(minutes * rate.flat_rate)


def calculate_cost(
    lookup: PriceBookLookup,
    *,
    stage: str,
    provider: str,
    model: str,
    billing_unit: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    characters: int = 0,
    audio_seconds: Decimal = Decimal("0"),
    quantity: Decimal = Decimal("0"),  # generic unit count (minutes/messages) for telephony
) -> tuple[Decimal, int]:
    """Dispatch to the right formula and return (cost, price_book_id_used) —
    the price_version_id gets stored on the usage_events row so a later rate
    change never silently rewrites a past estimate."""
    rate = lookup.get_rate(provider=provider, model=model, billing_unit=billing_unit)
    if stage == "llm":
        cost = llm_cost(rate, input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
                         output_tokens=output_tokens)
    elif stage == "stt":
        cost = stt_cost(rate, audio_seconds=audio_seconds)
    elif stage == "tts":
        cost = tts_cost(rate, characters=characters)
    elif stage == "telephony":
        cost = telephony_cost(rate, minutes=quantity)
    else:
        raise ValueError(f"Unknown stage: {stage}")
    return cost, rate.id
