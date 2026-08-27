"""Log the actual conversation: what was heard, what was said, what was called.

Without this the only record of a call is the transcript JSON written at
shutdown, which on LiveKit Cloud lands on an ephemeral disk and disappears with
the container. Debugging a call you cannot read is guesswork, and guesswork is
how you end up tuning a prompt against a bug that was never in the prompt.

Four things get a line, in the order they happen:

    >> user   what Deepgram finally settled on
    -- tool   which tool ran, with what arguments, and what it returned
    << agent  what the LLM produced, with what the caller actually waited for it
    !! ...    a turn that produced no transcript at all

The agent line is logged when the message is committed, which is *after* the
reply has finished playing. Timing a call by that timestamp overstates the
latency by however long the agent spoke, so the line carries the measured wait
rather than leaving it to be inferred.

Every line carries the same `job_id` and `room` as the rest of the logs, so a
single call can be pulled out of `lk agent logs` in one grep.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from livekit.agents import AgentSession, ConversationItemAddedEvent

logger = logging.getLogger("voice_agent.conversation")

# Long tool arguments and outputs are truncated. The point of these lines is to
# follow the shape of a call, not to reproduce every field.
MAX_VALUE_CHARS = 120


def _clip(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_arguments(raw: str) -> str:
    """Render a tool's JSON arguments as key=value, falling back to the raw string."""
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return _clip(raw)
    if not isinstance(parsed, dict):
        return _clip(parsed)
    if not parsed:
        return ""
    return " ".join(f"{key}={_clip(value, 60)!r}" for key, value in parsed.items())


def attach_conversation_logging(
    session: AgentSession, *, include_interim: bool = False
) -> None:
    """Subscribe to the session and log every turn as it happens.

    `include_interim` also logs partial transcripts, which is useful when
    diagnosing turn detection but noisy otherwise: Deepgram revises a partial
    several times per second.
    """

    @session.on("user_input_transcribed")
    def _on_user_transcript(event: Any) -> None:
        transcript = (getattr(event, "transcript", "") or "").strip()
        if not transcript:
            return
        if getattr(event, "is_final", False):
            language = getattr(event, "language", None)
            logger.info(
                ">> user | %s%s",
                transcript,
                f"  [{language}]" if language else "",
            )
        elif include_interim:
            logger.debug(".. user | %s", transcript)

    @session.on("conversation_item_added")
    def _on_item(event: ConversationItemAddedEvent) -> None:
        item = event.item
        # Only the agent side here. The user side is logged above, where it
        # arrives sooner and carries the detected language.
        if getattr(item, "role", None) != "assistant":
            return
        text = (getattr(item, "text_content", "") or "").strip()
        if not text:
            return

        # This event fires when the message is COMMITTED, which is after the
        # agent has finished speaking it. Read against the wall clock alone the
        # line therefore looks far slower than the call actually was: a three
        # second reply appears three seconds late. Carry the real figure on the
        # line so the timestamp cannot be misread.
        #
        # e2e_latency is measured by the library as
        # `started_speaking_at - user_stopped_speaking_at`, so it is what the
        # caller actually waited before hearing anything.
        waited = ""
        metrics = getattr(item, "metrics", None) or {}
        latency = metrics.get("e2e_latency")
        if isinstance(latency, (int, float)):
            waited = f"  [waited {latency:.1f}s]"

        # Newlines would split one turn across several log records, and a
        # multi-line agent reply is itself a signal worth seeing on one line.
        logger.info("<< agent | %s%s", " ".join(text.split()), waited)

    @session.on("function_tools_executed")
    def _on_tools(event: Any) -> None:
        calls = list(getattr(event, "function_calls", []) or [])
        outputs = list(getattr(event, "function_call_outputs", []) or [])
        for index, call in enumerate(calls):
            output = outputs[index] if index < len(outputs) else None
            # A None output means the tool raised StopResponse or returned
            # nothing usable, which is worth seeing rather than hiding.
            result = (
                _clip(getattr(output, "output", "")) if output is not None else "(no output)"
            )
            logger.info(
                "-- tool | %s(%s) -> %s",
                getattr(call, "name", "?"),
                _format_arguments(getattr(call, "arguments", "")),
                result,
            )

    @session.on("user_transcription_timeout")
    def _on_timeout(event: Any) -> None:
        """VAD heard speech but the STT produced nothing for it.

        This is the signature of the stacked-question bug: a flushed turn with
        an empty transcript. Logging it makes that visible while it happens
        rather than only in the wreckage afterwards.
        """
        logger.warning(
            "!! user | %.1fs of speech produced no transcript",
            getattr(event, "speech_duration", 0.0) or 0.0,
        )
