"""Published list prices for the models this agent can use.

Every number here was taken from the provider's own pricing page on the date
noted. Prices change and volume tiers are not modelled, so treat the totals as a
close estimate rather than an invoice. Override any of them with environment
variables (see PRICE_ENV_OVERRIDES) if your rates differ.

Deliberately NOT included in the totals:
  - LiveKit Cloud agent minutes (only billed when running against Cloud)
  - Telephony minutes (carrier-dependent)
Both are reported separately when known.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PRICES_UPDATED = "2026-08-19"

# Approximate. Override with USD_INR if you want accurate rupee figures.
DEFAULT_USD_INR = 88.0


@dataclass(frozen=True)
class STTPrice:
    per_minute_usd: float
    source: str


@dataclass(frozen=True)
class LLMPrice:
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cached_input_per_mtok_usd: float
    source: str


@dataclass(frozen=True)
class TTSPrice:
    per_1k_chars_usd: float
    source: str


# https://deepgram.com/pricing - streaming, pay-as-you-go tier
STT_PRICES: dict[str, STTPrice] = {
    "flux-general-multi": STTPrice(0.0078, "deepgram.com/pricing"),
    "flux-general-en": STTPrice(0.0077, "deepgram.com/pricing"),
    "nova-3-multilingual": STTPrice(0.0092, "deepgram.com/pricing"),
    "nova-3": STTPrice(0.0077, "deepgram.com/pricing"),
}

# https://console.groq.com/docs/models - cached input is half the input rate
LLM_PRICES: dict[str, LLMPrice] = {
    "openai/gpt-oss-120b": LLMPrice(0.15, 0.60, 0.075, "console.groq.com/docs/models"),
    "openai/gpt-oss-20b": LLMPrice(0.075, 0.30, 0.0375, "console.groq.com/docs/models"),
}

TTS_PRICES: dict[str, TTSPrice] = {
    # https://deepgram.com/pricing - Aura-2, per 1k characters
    "aura-2": TTSPrice(0.030, "deepgram.com/pricing"),
    # https://rumik.ai/silk-api - Rs 0.50 / 1k chars, quoted as $0.005 / 1k
    "mulberry": TTSPrice(0.005, "rumik.ai/silk-api"),
    "muga": TTSPrice(0.010, "rumik.ai/silk-api"),
}


def stt_price(model: str) -> STTPrice | None:
    if model in STT_PRICES:
        return STT_PRICES[model]
    # Aura-style family fallbacks keep this useful when a voice name changes.
    for key, price in STT_PRICES.items():
        if model.startswith(key):
            return price
    return None


def llm_price(model: str) -> LLMPrice | None:
    return LLM_PRICES.get(model)


def tts_price(model: str) -> TTSPrice | None:
    if model in TTS_PRICES:
        return TTS_PRICES[model]
    if model.startswith("aura-2"):
        return TTS_PRICES["aura-2"]
    return None


def usd_to_inr() -> float:
    raw = os.environ.get("USD_INR", "").strip()
    try:
        return float(raw) if raw else DEFAULT_USD_INR
    except ValueError:
        return DEFAULT_USD_INR
