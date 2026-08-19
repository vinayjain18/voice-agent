"""Text-to-speech provider factory."""

from __future__ import annotations

import logging

from livekit.agents import tts as tts_base

# Must be module scope - see the note in providers/stt.py.
from livekit.plugins import cartesia, deepgram, rumik_ai

from voice_agent.config import ConfigError, TTSSettings, require_api_key

logger = logging.getLogger(__name__)

# Rumik mulberry MUST be pinned to a voice. When neither `speaker` nor
# `description` is sent the server generates one from scratch, and because those
# fields go out on every request, each utterance can come back in a different
# voice - in practice English in a female voice and the next Hindi reply in a
# male one. Pinning a preset keeps one voice for the whole call.
RUMIK_DEFAULT_SPEAKER = "ira"

# Source of truth is the server, but the plugin ships the canonical list so we
# can warn on a typo instead of silently falling back to a generated voice.
RUMIK_FEMALE_VOICES = {"emma", "mia", "sophia", "ava", "ira", "siya", "aisha", "zoya"}
RUMIK_MALE_VOICES = {"lucas", "noah", "theo", "adam"}


def build_tts(settings: TTSSettings) -> tts_base.TTS:
    if settings.provider == "deepgram":
        return deepgram.TTS(model=settings.model, api_key=require_api_key("deepgram"))

    if settings.provider == "rumik":
        # Rumik reads RUMIK_API_KEY itself, but resolving it here gives a better
        # error message than a mid-call failure.
        require_api_key("rumik")
        kwargs: dict[str, object] = {"model": settings.model}

        if settings.rumik_speaker:
            speaker = settings.rumik_speaker.strip().lower()
            known = RUMIK_FEMALE_VOICES | RUMIK_MALE_VOICES
            if speaker not in known and not speaker.startswith("speaker_"):
                logger.warning(
                    "RUMIK_SPEAKER=%r is not a known mulberry voice. The server "
                    "will generate a voice instead, which can change between "
                    "utterances. Known voices: %s",
                    speaker,
                    ", ".join(sorted(known)),
                )
            kwargs["speaker"] = speaker
        elif settings.rumik_description:
            kwargs["description"] = settings.rumik_description
        else:
            # Never leave the voice unpinned - see RUMIK_DEFAULT_SPEAKER above.
            kwargs["speaker"] = RUMIK_DEFAULT_SPEAKER

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
