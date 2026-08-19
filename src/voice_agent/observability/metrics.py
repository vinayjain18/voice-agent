"""Per-turn latency logging.

You cannot tune what you cannot see. This prints one line per agent turn with
the latency split out by stage, so it is obvious whether a slow reply came from
turn detection, the LLM, or TTS.

In livekit-agents 1.6 the `metrics_collected` event is deprecated; per-turn
latency now hangs off `ChatMessage.metrics` and arrives with
`conversation_item_added`.
"""

from __future__ import annotations

import logging
from typing import Any

from livekit.agents import AgentSession, ConversationItemAddedEvent

logger = logging.getLogger("voice_agent.metrics")


def _ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value * 1000:.0f}ms"


def attach_metrics_logging(session: AgentSession) -> None:
    """Subscribe to the session and log a latency breakdown per agent turn."""

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        item = event.item
        metrics = getattr(item, "metrics", None)
        if not metrics:
            return

        role = getattr(item, "role", "?")
        if role != "assistant":
            # User turns carry transcription timings; not the latency we tune on.
            return

        logger.info(
            "turn latency | e2e=%s eot=%s llm_ttft=%s tts_ttfb=%s playback=%s",
            _ms(metrics.get("e2e_latency")),
            _ms(metrics.get("end_of_turn_delay")),
            _ms(metrics.get("llm_node_ttft")),
            _ms(metrics.get("tts_node_ttfb")),
            _ms(metrics.get("playback_latency")),
        )

    @session.on("user_input_transcribed")
    def _on_transcript(event: Any) -> None:
        if getattr(event, "is_final", False):
            logger.debug("user said: %s", getattr(event, "transcript", ""))
