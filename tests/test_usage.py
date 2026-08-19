"""Usage accounting and cost estimation."""

from __future__ import annotations

from livekit.agents.metrics.usage import (
    AgentSessionUsage,
    LLMModelUsage,
    STTModelUsage,
    TTSModelUsage,
)

from voice_agent import pricing
from voice_agent.observability.usage import format_summary, summarise


def test_stt_cost_is_per_minute_of_audio():
    usage = AgentSessionUsage(model_usage=[
        STTModelUsage(provider="deepgram", model="flux-general-multi", audio_duration=120.0)
    ])
    cost = summarise(usage, 130.0)
    expected = 2.0 * pricing.STT_PRICES["flux-general-multi"].per_minute_usd
    assert abs(cost.total_usd - expected) < 1e-9


def test_llm_cost_splits_cached_and_fresh_input():
    """Cached input is half price; billing all input at full rate overstates it."""
    usage = AgentSessionUsage(model_usage=[
        LLMModelUsage(
            provider="groq", model="openai/gpt-oss-120b",
            input_tokens=1_000_000, input_cached_tokens=1_000_000, output_tokens=0,
        )
    ])
    fully_cached = summarise(usage, 60.0).total_usd

    usage2 = AgentSessionUsage(model_usage=[
        LLMModelUsage(
            provider="groq", model="openai/gpt-oss-120b",
            input_tokens=1_000_000, input_cached_tokens=0, output_tokens=0,
        )
    ])
    no_cache = summarise(usage2, 60.0).total_usd

    assert abs(fully_cached - 0.075) < 1e-9
    assert abs(no_cache - 0.15) < 1e-9
    assert fully_cached < no_cache


def test_tts_cost_is_per_thousand_characters():
    usage = AgentSessionUsage(model_usage=[
        TTSModelUsage(provider="rumik", model="mulberry",
                      characters_count=2000, audio_duration=60.0)
    ])
    cost = summarise(usage, 70.0)
    assert abs(cost.total_usd - 2 * pricing.TTS_PRICES["mulberry"].per_1k_chars_usd) < 1e-9


def test_rumik_is_cheaper_than_deepgram_for_the_same_text():
    """The core cost argument for Rumik. If this flips, revisit the choice."""
    def cost_of(model):
        return summarise(AgentSessionUsage(model_usage=[
            TTSModelUsage(provider="p", model=model, characters_count=10_000)
        ]), 60.0).total_usd

    assert cost_of("mulberry") < cost_of("aura-2-andromeda-en")


def test_aura_voice_names_resolve_to_the_family_price():
    assert pricing.tts_price("aura-2-andromeda-en") is not None
    assert pricing.tts_price("aura-2-thalia-en") is not None


def test_unknown_model_is_reported_but_not_priced():
    usage = AgentSessionUsage(model_usage=[
        TTSModelUsage(provider="x", model="not-a-real-model", characters_count=5000)
    ])
    cost = summarise(usage, 60.0)
    assert cost.has_unpriced
    assert cost.total_usd == 0.0
    assert "no price" in format_summary(cost)


def test_summary_renders_duration_and_total():
    usage = AgentSessionUsage(model_usage=[
        STTModelUsage(provider="deepgram", model="flux-general-multi", audio_duration=65.0)
    ])
    out = format_summary(summarise(usage, 185.0))
    assert "3m 5s" in out
    assert "TOTAL" in out
    assert "Rs " in out


def test_empty_session_does_not_crash():
    out = format_summary(summarise(AgentSessionUsage(model_usage=[]), 12.0))
    assert "No model usage recorded" in out


def test_usd_inr_override(monkeypatch):
    monkeypatch.setenv("USD_INR", "90.5")
    assert pricing.usd_to_inr() == 90.5
    monkeypatch.setenv("USD_INR", "nonsense")
    assert pricing.usd_to_inr() == pricing.DEFAULT_USD_INR
