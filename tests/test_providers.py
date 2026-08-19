"""Provider factory tests - construction only, no network calls."""

from __future__ import annotations

import pytest

from voice_agent.config import ConfigError, LLMSettings, STTSettings, TTSSettings
from voice_agent.providers import build_llm, build_stt, build_tts
from voice_agent.providers.stt import uses_model_turn_detection


def test_flux_models_report_model_turn_detection():
    assert uses_model_turn_detection(STTSettings(model="flux-general-multi"))
    assert uses_model_turn_detection(STTSettings(model="flux-general-en"))
    assert not uses_model_turn_detection(STTSettings(model="nova-3"))


def test_flux_stt_builds_as_sttv2(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    from livekit.plugins import deepgram

    stt = build_stt(STTSettings(model="flux-general-multi"))
    assert isinstance(stt, deepgram.STTv2)


def test_groq_llm_builds(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    llm = build_llm(LLMSettings())
    assert llm.model == "openai/gpt-oss-120b"


def test_deepgram_tts_builds(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    tts = build_tts(TTSSettings(provider="deepgram", model="aura-2-andromeda-en"))
    assert tts is not None


def test_rumik_tts_builds_with_mulberry(monkeypatch):
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    from livekit.plugins import rumik_ai

    tts = build_tts(TTSSettings(provider="rumik", model="mulberry"))
    assert isinstance(tts, rumik_ai.TTS)


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="Unsupported TTS_PROVIDER"):
        build_tts(TTSSettings(provider="nope"))
    with pytest.raises(ConfigError, match="Unsupported STT_PROVIDER"):
        build_stt(STTSettings(provider="nope"))


def test_build_vad_reuses_prewarmed_instance():
    """The prewarmed VAD must not be silently discarded and reloaded."""
    from voice_agent.providers.vad import build_vad

    sentinel = object()
    assert build_vad(sentinel) is sentinel


async def test_build_session_threads_prewarmed_vad_through(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    monkeypatch.delenv("TTS_PROVIDER", raising=False)

    from voice_agent.config import Settings
    from voice_agent.providers import vad as vad_module

    def _fail():
        raise AssertionError("silero.VAD.load() called despite a prewarmed VAD")

    monkeypatch.setattr(vad_module, "build_vad", lambda p=None: p or _fail())

    import voice_agent.session as session_module

    monkeypatch.setattr(session_module, "build_vad", lambda p=None: p or _fail())
    sentinel = object()
    session = session_module.build_session(Settings.load(), vad=sentinel)
    assert session is not None


def test_rumik_voice_is_always_pinned(monkeypatch):
    """Unpinned mulberry generates a new voice per utterance - the drift bug."""
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    from voice_agent.providers.tts import RUMIK_DEFAULT_SPEAKER, RUMIK_FEMALE_VOICES

    tts = build_tts(TTSSettings(provider="rumik", model="mulberry"))
    assert tts._opts.speaker == RUMIK_DEFAULT_SPEAKER
    assert RUMIK_DEFAULT_SPEAKER in RUMIK_FEMALE_VOICES


def test_rumik_speaker_override_is_normalised(monkeypatch):
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    tts = build_tts(
        TTSSettings(provider="rumik", model="mulberry", rumik_speaker="Emma")
    )
    assert tts._opts.speaker == "emma"


def test_rumik_description_replaces_the_default_speaker(monkeypatch):
    monkeypatch.setenv("RUMIK_API_KEY", "test-key")
    tts = build_tts(
        TTSSettings(
            provider="rumik", model="mulberry", rumik_description="warm Indian woman"
        )
    )
    assert tts._opts.description == "warm Indian woman"
