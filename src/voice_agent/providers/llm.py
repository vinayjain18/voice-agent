"""LLM provider factory."""

from __future__ import annotations

from livekit.agents import llm as llm_base

# Must be module scope - see the note in providers/stt.py.
from livekit.plugins import groq

from voice_agent.config import ConfigError, LLMSettings, require_api_key


def build_llm(settings: LLMSettings) -> llm_base.LLM:
    if settings.provider == "groq":
        return groq.LLM(
            model=settings.model,
            api_key=require_api_key("groq"),
            temperature=settings.temperature,
            max_completion_tokens=settings.max_completion_tokens,
        )
    raise ConfigError(
        f"Unsupported LLM_PROVIDER '{settings.provider}'. Supported: groq."
    )
