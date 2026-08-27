"""Text transforms applied to LLM output before it reaches the TTS.

`AgentSession(tts_text_transforms=...)` accepts the library's built-in
"filter_markdown" and "filter_emoji" plus any callable with the same shape:
an async iterator of text chunks in, an async iterator out. The stream is
incremental - a single word can arrive split across chunks - so anything here
has to be a state machine, not a regex over a finished string.

These affect only what is *spoken*. The transcript keeps the original text,
which is what you want when working out why the model said something odd.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

logger = logging.getLogger(__name__)

# Bracket pairs worth stripping. Curly braces are deliberately absent: the
# prompt template is rendered with str.format, so braces never survive to here,
# and stripping them would silently eat any maths a caller asked about.
BRACKET_PAIRS = {
    "(": ")",
    "[": "]",
    "（": "）",  # full-width, which multilingual models emit for Hindi
    "【": "】",
}

# If a bracket never closes, everything after it would be swallowed for the rest
# of the utterance - the agent would simply go silent mid-sentence. Past this
# many characters we assume the "(" was stray, release what we held back, and
# carry on. Losing a bracket is a cosmetic problem; losing the reply is not.
MAX_SWALLOWED_CHARS = 200

# Punctuation that attaches to the word before it, so no space is spoken in
# front of it. Includes the Devanagari full stop, which Hindi replies use.
CLINGING_PUNCTUATION = frozenset(".,!?;:%…।")


async def strip_parentheticals(text: AsyncIterable[str]) -> AsyncIterable[str]:
    """Drop bracketed asides so they are never spoken.

    Reasoning-tuned models (gpt-oss among them) like to append a parenthetical
    gloss to an otherwise clean sentence - "We can help with that (the caller
    seems interested in mobile work)". On a screen it is noise; read aloud by a
    receptionist it is baffling, and occasionally it leaks the model's own
    private commentary about the caller to the caller.

    Nesting of the same pair is tracked so "(a (b) c)" is removed whole.
    """
    depth = 0
    closer = ""
    # Text swallowed since the current bracket opened, kept so a bracket that
    # never closes can be released rather than lost. Cleared on every close.
    held: list[str] = []
    # Whitespace is held back for one character rather than emitted as it
    # arrives. Removing a bracket otherwise leaves the spaces that surrounded it
    # stranded: "that (aside)." becomes "that ." and the TTS pauses mid-phrase.
    # Deferring means the space is only spent once we know what follows it.
    pending_space = False
    emitted_anything = False

    async for chunk in text:
        out: list[str] = []

        for char in chunk:
            if depth == 0:
                if char in BRACKET_PAIRS:
                    depth = 1
                    closer = BRACKET_PAIRS[char]
                    held = [char]
                    continue
                if char.isspace():
                    pending_space = True
                    continue
                # Drop the held space before punctuation, and at the very start
                # of the reply; keep it everywhere else.
                if pending_space:
                    if emitted_anything and char not in CLINGING_PUNCTUATION:
                        out.append(" ")
                    pending_space = False
                out.append(char)
                emitted_anything = True
                continue

            # Inside a bracket: count nesting, and watch for a runaway.
            held.append(char)
            if char == closer:
                depth -= 1
                if depth == 0:
                    closer = ""
                    held = []
            elif BRACKET_PAIRS.get(char) == closer:
                depth += 1
            elif len(held) >= MAX_SWALLOWED_CHARS:
                logger.warning(
                    "unclosed bracket in TTS text after %d characters; speaking "
                    "the held text rather than muting the rest of the reply",
                    len(held),
                )
                # Release everything except the stray opening bracket itself.
                if pending_space and emitted_anything:
                    out.append(" ")
                pending_space = False
                out.extend(held[1:])
                emitted_anything = True
                depth = 0
                closer = ""
                held = []

        if out:
            yield "".join(out)
