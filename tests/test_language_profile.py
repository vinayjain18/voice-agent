"""Language profile selection and the guard against a mute-in-Hindi pipeline."""

from __future__ import annotations

import pytest

from voice_agent.config import TTS_DEFAULT_MODELS, ConfigError, Settings


def _clean(monkeypatch):
    for key in (
        "LANGUAGE_PROFILE", "STT_MODEL", "STT_LANGUAGE_HINTS",
        "TTS_PROVIDER", "TTS_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


def test_hinglish_profile_selects_multilingual_flux_and_rumik(monkeypatch):
    _clean(monkeypatch)
    s = Settings.load()
    assert s.stt.model == "flux-general-multi"
    assert s.stt.language_hints == ["en", "hi"]
    assert s.tts.provider == "rumik"
    assert s.tts.model == "mulberry"
    s.validate_language_support()


def test_english_profile_selects_english_flux_and_deepgram(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("LANGUAGE_PROFILE", "english")
    s = Settings.load()
    assert s.stt.model == "flux-general-en"
    assert s.tts.provider == "deepgram"
    assert s.tts.model == "aura-2-andromeda-en"
    s.validate_language_support()


def test_each_provider_gets_its_own_default_model(monkeypatch):
    """The Phase 2 footgun: rumik must not inherit a Deepgram model name."""
    for provider, expected in TTS_DEFAULT_MODELS.items():
        _clean(monkeypatch)
        monkeypatch.setenv("TTS_PROVIDER", provider)
        assert Settings.load().tts.model == expected


def test_hinglish_with_english_only_tts_is_rejected(monkeypatch):
    """The Phase 1 defect: prompt promises Hindi, voice cannot speak it."""
    _clean(monkeypatch)
    monkeypatch.setenv("TTS_PROVIDER", "deepgram")
    with pytest.raises(ConfigError, match="English-only"):
        Settings.load().validate_language_support()


def test_explicit_model_overrides_the_profile(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("TTS_MODEL", "muga")
    assert Settings.load().tts.model == "muga"


def test_invalid_profile_rejected(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("LANGUAGE_PROFILE", "klingon")
    with pytest.raises(ConfigError, match="LANGUAGE_PROFILE"):
        Settings.load()


def test_spoken_languages_track_the_profile(monkeypatch):
    _clean(monkeypatch)
    assert "Hindi" in Settings.load().language.spoken_languages
    monkeypatch.setenv("LANGUAGE_PROFILE", "english")
    assert "Hindi" not in Settings.load().language.spoken_languages
