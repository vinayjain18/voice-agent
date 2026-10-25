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


def test_english_is_the_default_profile(monkeypatch):
    """Ships English-only Flux with a US-accented Deepgram voice."""
    _clean(monkeypatch)
    s = Settings.load()
    assert s.language.name == "english"
    assert s.stt.model == "flux-general-en"
    assert s.stt.language_hints == ["en"]
    assert s.tts.provider == "deepgram"
    assert s.tts.model == "aura-2-asteria-en"
    s.validate_language_support()


def test_the_default_voice_is_a_us_accent():
    """asteria is one of Deepgram's American English voices.

    The accent is the whole reason this is the default, and the voice name is
    the only thing that carries it - there is no separate accent setting.
    """
    from voice_agent.config import TTS_DEFAULT_MODELS

    assert TTS_DEFAULT_MODELS["deepgram"] == "aura-2-asteria-en"


def test_default_needs_no_rumik_key(monkeypatch):
    """The default pipeline is Deepgram plus Groq, so Rumik is not required."""
    _clean(monkeypatch)
    monkeypatch.delenv("RUMIK_API_KEY", raising=False)
    Settings.load().preflight()


def test_hinglish_profile_selects_multilingual_flux_and_rumik(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("LANGUAGE_PROFILE", "hinglish")
    s = Settings.load()
    assert s.stt.model == "flux-general-multi"
    assert s.stt.language_hints == ["en", "hi"]
    assert s.tts.provider == "rumik"
    assert s.tts.model == "mulberry"
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
    monkeypatch.setenv("LANGUAGE_PROFILE", "hinglish")
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
    """What the prompt promises has to match what the voice can actually say."""
    _clean(monkeypatch)
    assert "Hindi" not in Settings.load().language.spoken_languages
    monkeypatch.setenv("LANGUAGE_PROFILE", "hinglish")
    assert "Hindi" in Settings.load().language.spoken_languages


# --- how the Hinglish half actually speaks ------------------------------------

def _hinglish(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("LANGUAGE_PROFILE", "hinglish")
    return Settings.load().language


def test_hinglish_is_written_in_roman_script(monkeypatch):
    """Devanagari came back as formal, bookish Hindi on a real call.

    Roman Hinglish is what people actually speak on the phone, and it keeps the
    written turn and the spoken turn the same thing.
    """
    language = _hinglish(monkeypatch)
    guidance = language.speaking_guidance

    assert "Roman" in guidance
    assert "Devanagari" in guidance
    for example in language.worked_example.splitlines():
        assert not any("ऀ" <= ch <= "ॿ" for ch in example), example


def test_hinglish_speaks_as_a_woman(monkeypatch):
    """The agent is a woman and must not use masculine verb forms."""
    language = _hinglish(monkeypatch)
    guidance = language.speaking_guidance

    for feminine in ("rahi hoon", "sakti hoon", "gayi"):
        assert feminine in guidance, feminine
    for masculine in ("raha hoon", "sakta hoon"):
        assert f"never '{masculine}'" in guidance.lower(), masculine


def test_hinglish_examples_use_feminine_forms_only(monkeypatch):
    """An example that breaks the rule beats the rule (constraint 21)."""
    spoken = [
        line for line in _hinglish(monkeypatch).worked_example.splitlines()
        if line.startswith("You:")
    ]
    assert spoken, "the Hinglish example teaches nothing without agent lines"
    for line in spoken:
        lowered = line.lower()
        for masculine in ("raha hoon", "sakta hoon", "karunga", "bataunga"):
            assert masculine not in lowered, line


def test_hinglish_keeps_banking_words_in_english(monkeypatch):
    """Nobody asks for their "khaata vivaran". Translating jargon sounds wrong."""
    guidance = _hinglish(monkeypatch).speaking_guidance
    assert "KYC" in guidance
    assert "shuddh" in guidance.lower() or "formal Hindi" in guidance


def test_hinglish_example_is_a_bank_call(monkeypatch):
    """The example used to be about building a website for a restaurant."""
    example = _hinglish(monkeypatch).worked_example.lower()
    assert "kyc" in example
    assert "website" not in example
