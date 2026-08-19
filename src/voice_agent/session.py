"""Assemble an AgentSession from configuration.

Everything provider-specific has already been resolved by the factories in
providers/, so this file only expresses *pipeline* decisions: how turns are
detected, how interruptions behave, how long we wait before replying.
"""

from __future__ import annotations

import logging

from livekit.agents import AgentSession
from livekit.agents import vad as vad_base

from voice_agent.config import ConfigError, Settings
from voice_agent.providers import build_llm, build_stt, build_tts, build_vad
from voice_agent.providers.stt import uses_model_turn_detection

logger = logging.getLogger(__name__)


def build_session(
    settings: Settings, *, vad: vad_base.VAD | None = None
) -> AgentSession:
    stt = build_stt(settings.stt)
    llm = build_llm(settings.llm)
    tts = build_tts(settings.tts)
    vad = build_vad(vad)

    turn_detection = _resolve_turn_detection(settings)
    interruption_mode = _resolve_interruption_mode(settings)

    logger.info(
        "pipeline | stt=%s:%s llm=%s:%s tts=%s:%s turn_detection=%s",
        settings.stt.provider,
        settings.stt.model,
        settings.llm.provider,
        settings.llm.model,
        settings.tts.provider,
        settings.tts.model,
        turn_detection,
    )

    turn_handling: dict = {
        "endpointing": {
            # Flux already waits for semantic end-of-turn, so an extra fixed
            # silence window here is pure added latency.
            "min_delay": 0.2 if turn_detection == "stt" else 0.5,
            "max_delay": 3.0,
        },
        "interruption": {
            "enabled": True,
            "mode": interruption_mode,
            # Ignore very short noises ("mm", a cough) so the agent is not cut
            # off by backchannel.
            "min_duration": 0.4,
            "min_words": 0,
        },
        "preemptive_generation": {"enabled": True},
    }

    # "livekit" means "let the library build its own cloud turn detector", which
    # it only does when the key is absent - the default is evaluated inside
    # AgentSession. Any other value is passed through explicitly.
    if turn_detection != "livekit":
        turn_handling["turn_detection"] = turn_detection

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        # VAD stays on even with model turn detection - it drives barge-in,
        # which is a different job from deciding the user has finished.
        vad=vad,
        turn_handling=turn_handling,
    )


def _resolve_turn_detection(settings: Settings) -> str:
    """Pick who decides the user has stopped talking.

    "auto" prefers the STT model's own end-of-turn detection (Deepgram Flux),
    which is the biggest latency win available: no separate model, no extra
    round trip. It also needs no LiveKit credentials, so console mode works
    standalone. Falls back to VAD when the STT cannot do it.

    "livekit" uses LiveKit's cloud turn detector, which requires credentials.
    """
    choice = settings.pipeline.turn_detection

    if choice == "auto":
        return "stt" if uses_model_turn_detection(settings.stt) else "vad"

    if choice in {"stt", "vad"}:
        if choice == "stt" and not uses_model_turn_detection(settings.stt):
            raise ConfigError(
                f"TURN_DETECTION=stt needs an STT model with built-in end-of-turn "
                f"detection (Deepgram Flux). Current STT model: {settings.stt.model}."
            )
        return choice

    if choice == "livekit":
        if not settings.livekit.has_cloud_inference:
            raise ConfigError(
                "TURN_DETECTION=livekit is a LiveKit Cloud inference model and "
                "needs LIVEKIT_MODE=cloud with LIVEKIT_URL, LIVEKIT_API_KEY and "
                "LIVEKIT_API_SECRET. A self-hosted livekit-server has no "
                "inference gateway - use 'stt' or 'vad' there."
            )
        # Passing no explicit value lets the library construct its cloud detector.
        return "livekit"

    raise ConfigError(
        f"TURN_DETECTION must be auto, stt, vad or livekit. Got '{choice}'."
    )


def _resolve_interruption_mode(settings: Settings) -> str:
    """Pick how barge-in is detected.

    "adaptive" is a LiveKit *Cloud* inference service; without it the detector
    fails to construct and logs a warning on every session. So "auto" only
    selects it against Cloud - not against a self-hosted server, which has no
    inference gateway.
    """
    choice = settings.pipeline.interruption_mode

    if choice == "auto":
        return "adaptive" if settings.livekit.has_cloud_inference else "vad"

    if choice == "adaptive" and not settings.livekit.has_cloud_inference:
        raise ConfigError(
            "INTERRUPTION_MODE=adaptive is a LiveKit Cloud inference service and "
            "needs LIVEKIT_MODE=cloud with credentials. Use 'vad' to run against "
            "a local server or with no LiveKit account."
        )

    if choice not in {"vad", "adaptive"}:
        raise ConfigError(
            f"INTERRUPTION_MODE must be auto, vad or adaptive. Got '{choice}'."
        )

    return choice
