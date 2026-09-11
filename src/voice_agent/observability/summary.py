"""Turn a finished conversation into one line an admin can scan.

Runs after the call has been torn down, so its latency costs the caller nothing.
The LLM call is cheap (a few hundred tokens against the model already
configured) but it is still a network call, so everything here degrades rather
than raises: a failed summary falls back to the caller's own first sentence,
which is usually the reason they rang anyway.
"""

from __future__ import annotations

import logging
from typing import Any

from voice_agent.storage.calls import (
    OUTCOME_BOOKED,
    OUTCOME_CANCELLED,
    OUTCOME_ENQUIRY,
    OUTCOME_NO_ACTION,
    OUTCOME_RESCHEDULED,
)

logger = logging.getLogger(__name__)

INSTRUCTION = """Summarise this phone call to a medical center in ONE sentence,
under 30 words, for a receptionist reading a log.

Say what the caller wanted and what happened. Use plain past tense. Do not
mention the assistant, do not add any medical interpretation, and do not invent
anything that is not in the transcript.

Transcript:
{transcript}

One sentence:"""

# Tool names that tell us how the call actually ended, most specific first.
OUTCOME_BY_TOOL = [
    ("reschedule_appointment", OUTCOME_RESCHEDULED),
    ("cancel_appointment", OUTCOME_CANCELLED),
    ("book_appointment", OUTCOME_BOOKED),
]

MAX_TRANSCRIPT_CHARS = 6000


def conversation_lines(history: Any) -> list[tuple[str, str]]:
    """(role, text) for everything actually said, oldest first."""
    out: list[tuple[str, str]] = []
    try:
        for item in history.items:
            role = getattr(item, "role", "")
            if role not in {"user", "assistant"}:
                continue
            text = (getattr(item, "text_content", "") or "").strip()
            if text:
                out.append((role, text))
    except Exception:
        logger.debug("could not read conversation history", exc_info=True)
    return out


def called_tools(history: Any) -> list[str]:
    """Names of every tool the model called, in order."""
    names: list[str] = []
    try:
        for item in history.items:
            name = getattr(item, "name", None)
            if name and getattr(item, "type", "") != "message":
                names.append(str(name))
    except Exception:
        logger.debug("could not read tool calls", exc_info=True)
    return names


def infer_outcome(history: Any) -> str:
    """What the call achieved, from the tools that actually ran."""
    tools = set(called_tools(history))
    for name, outcome in OUTCOME_BY_TOOL:
        if name in tools:
            return outcome
    if tools:
        return OUTCOME_ENQUIRY
    return OUTCOME_NO_ACTION if not conversation_lines(history) else OUTCOME_ENQUIRY


def fallback_summary(history: Any) -> str:
    """The caller's first real sentence. Usually why they rang."""
    for role, text in conversation_lines(history):
        if role == "user":
            return text[:200]
    return "No conversation recorded."


def render_transcript(history: Any) -> str:
    lines = [
        f"{'Caller' if role == 'user' else 'Receptionist'}: {text}"
        for role, text in conversation_lines(history)
    ]
    return "\n".join(lines)[-MAX_TRANSCRIPT_CHARS:]


async def summarise_call(session: Any, llm: Any) -> str:
    """One sentence describing the call. Never raises."""
    history = getattr(session, "history", None)
    if history is None:
        return "No conversation recorded."

    transcript = render_transcript(history)
    if not transcript.strip():
        return "Call connected but nothing was said."

    try:
        text = await _ask(llm, INSTRUCTION.format(transcript=transcript))
        cleaned = " ".join((text or "").split())
        if cleaned:
            return cleaned
    except Exception:
        logger.warning("could not generate a call summary", exc_info=True)

    return fallback_summary(history)


async def _ask(llm: Any, prompt: str) -> str:
    """One completion from the session's own LLM.

    livekit-agents wraps provider SDKs behind `llm.chat(chat_ctx=...)`, which
    returns a stream of deltas rather than a finished string.
    """
    from livekit.agents import ChatContext

    context = ChatContext()
    context.add_message(role="user", content=prompt)

    chunks: list[str] = []
    stream = llm.chat(chat_ctx=context)
    try:
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                chunks.append(content)
    finally:
        close = getattr(stream, "aclose", None)
        if close:
            await close()

    return "".join(chunks).strip()
