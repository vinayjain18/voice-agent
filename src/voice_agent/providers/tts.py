"""Text-to-speech provider factory."""

from __future__ import annotations

from livekit.agents import tts as tts_base

# Must be module scope - see the note in providers/stt.py.
from livekit.plugins import cartesia, deepgram, rumik_ai

from voice_agent.config import ConfigError, TTSSettings, require_api_key


def build_tts(settings: TTSSettings) -> tts_base.TTS:
    if settings.provider == "deepgram":
        return deepgram.TTS(model=settings.model, api_key=require_api_key("deepgram"))

    if settings.provider == "rumik":
        # Rumik reads RUMIK_API_KEY itself, but resolving it here gives a better
        # error message than a mid-call failure.
        require_api_key("rumik")
        kwargs: dict[str, object] = {"model": settings.model}
        if settings.rumik_speaker:
            kwargs["speaker"] = settings.rumik_speaker
        elif settings.rumik_description:
            kwargs["description"] = settings.rumik_description
        return rumik_ai.TTS(**kwargs)

    if settings.provider == "cartesia":
        kwargs = {"model": settings.model, "api_key": require_api_key("cartesia")}
        if settings.cartesia_voice:
            kwargs["voice"] = settings.cartesia_voice
        return cartesia.TTS(**kwargs)

    raise ConfigError(
        f"Unsupported TTS_PROVIDER '{settings.provider}'. "
        "Supported: deepgram, rumik, cartesia."
    )
