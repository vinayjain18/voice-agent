"""Config and prompt-rendering tests.

These deliberately avoid the network: they check the wiring that breaks
silently, not the providers.
"""

from __future__ import annotations

import pytest

from voice_agent.agents.receptionist import build_prompt_variables
from voice_agent.business import load_profile
from voice_agent.config import ConfigError, Settings, require_api_key
from voice_agent.prompts import render_prompt


def test_defaults_select_english_flux_and_a_us_deepgram_voice(monkeypatch):
    """Default profile is english: English-only STT plus a US-accented voice."""
    for key in ("STT_MODEL", "LLM_MODEL", "TTS_PROVIDER", "TTS_MODEL", "LANGUAGE_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.load()
    assert settings.stt.model == "flux-general-en"
    assert settings.llm.model == "openai/gpt-oss-120b"
    assert settings.tts.provider == "deepgram"
    assert settings.tts.model == "aura-2-asteria-en"


def test_env_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "rumik")
    monkeypatch.setenv("TTS_MODEL", "mulberry")
    monkeypatch.setenv("STT_LANGUAGE_HINTS", "en, hi, ta")
    settings = Settings.load()
    assert settings.tts.provider == "rumik"
    assert settings.tts.model == "mulberry"
    assert settings.stt.language_hints == ["en", "hi", "ta"]


def test_missing_api_key_raises_actionable_error(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        require_api_key("deepgram")
    assert "DEEPGRAM_API_KEY" in str(exc.value)
    assert "console.deepgram.com" in str(exc.value)


def test_prompt_renders_with_every_placeholder_filled(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    profile = load_profile()
    rendered = render_prompt(
        "receptionist", build_prompt_variables(profile, Settings.load())
    )
    assert "{" not in rendered, "prompt still contains an unfilled placeholder"
    assert profile["business_name"] in rendered


def test_prompt_no_longer_pushes_the_booking_link(monkeypatch):
    """The agent takes the booking itself instead of reading out a URL."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    profile = load_profile()
    rendered = render_prompt(
        "receptionist", build_prompt_variables(profile, Settings.load())
    )
    assert profile["booking_link"] not in rendered
    assert "book_callback" in rendered


def test_prompt_raises_on_missing_variable():
    with pytest.raises(KeyError):
        render_prompt("receptionist", {"agent_name": "Nova"})


def test_preflight_reports_missing_key_for_selected_provider(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        Settings.load().preflight()


def test_preflight_passes_when_all_keys_present(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    Settings.load().preflight()
