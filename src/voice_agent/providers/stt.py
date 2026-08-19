"""Speech-to-text provider factory."""

from __future__ import annotations

from livekit.agents import stt as stt_base

# Plugin packages call Plugin.register_plugin() at import time, which raises
# unless it happens on the main thread. Job runners execute in a worker thread,
# so every livekit.plugins import must be at module scope - never inside a
# function that a job will call. See livekit/agents/plugin.py:32.
from livekit.plugins import deepgram

from voice_agent.config import ConfigError, STTSettings, require_api_key


def build_stt(settings: STTSettings) -> stt_base.STT:
    if settings.provider == "deepgram":
        return _deepgram(settings)
    raise ConfigError(
        f"Unsupported STT_PROVIDER '{settings.provider}'. Supported: deepgram."
    )


def _deepgram(settings: STTSettings) -> stt_base.STT:
    api_key = require_api_key("deepgram")

    # Flux models speak the /v2/listen protocol and carry end-of-turn detection
    # inside the model, so they need STTv2 rather than the classic STT class.
    if settings.model.startswith("flux-"):
        kwargs = {
            "model": settings.model,
            "api_key": api_key,
            "eot_threshold": settings.eot_threshold,
            "eot_timeout_ms": settings.eot_timeout_ms,
        }
        # language_hint is only accepted by the multilingual Flux model; passing
        # it to flux-general-en makes the plugin log a warning and drop it.
        if settings.model == "flux-general-multi" and settings.language_hints:
            kwargs["language_hint"] = settings.language_hints
        return deepgram.STTv2(**kwargs)

    return deepgram.STT(model=settings.model, api_key=api_key)


def uses_model_turn_detection(settings: STTSettings) -> bool:
    """True when the STT model detects end-of-turn itself (Deepgram Flux)."""
    return settings.provider == "deepgram" and settings.model.startswith("flux-")
