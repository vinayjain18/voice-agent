"""The bracket stripper runs on a live token stream, so chunking is the risk."""

from __future__ import annotations

from collections.abc import AsyncIterable

import pytest

from voice_agent.text_filters import MAX_SWALLOWED_CHARS, strip_parentheticals


async def _feed(chunks: list[str]) -> AsyncIterable[str]:
    for chunk in chunks:
        yield chunk


async def _run(chunks: list[str]) -> str:
    return "".join([out async for out in strip_parentheticals(_feed(chunks))])


@pytest.mark.asyncio
async def test_removes_a_parenthetical_aside():
    assert await _run(["We can help with that (the caller wants mobile)."]) == (
        "We can help with that."
    )


@pytest.mark.asyncio
async def test_bracket_split_across_chunks():
    """The whole point: the LLM streams, so a bracket arrives in pieces."""
    assert await _run(["Sure thing (", "an aside", " here) ", "no problem."]) == (
        "Sure thing no problem."
    )


@pytest.mark.asyncio
async def test_single_characters_per_chunk():
    text = "Hello (aside) world"
    assert await _run(list(text)) == "Hello world"


@pytest.mark.asyncio
async def test_nested_brackets_removed_whole():
    assert await _run(["Yes (a (b) c) done"]) == "Yes done"


@pytest.mark.asyncio
async def test_square_and_full_width_brackets():
    assert await _run(["Ok [note] fine"]) == "Ok fine"
    assert await _run(["Haan （yeh hindi hai） theek hai"]) == "Haan theek hai"


@pytest.mark.asyncio
async def test_plain_text_is_untouched():
    text = "Our hours are ten am to seven pm, Monday to Friday."
    assert await _run([text]) == text


@pytest.mark.asyncio
async def test_unclosed_bracket_releases_rather_than_muting_the_reply():
    """A stray "(" must not silence the rest of the call."""
    tail = "x" * (MAX_SWALLOWED_CHARS + 10)
    out = await _run([f"Hello ({tail}"])
    assert out.startswith("Hello ")
    assert "x" in out
    # The stray bracket itself is still dropped.
    assert "(" not in out


@pytest.mark.asyncio
async def test_no_double_space_left_behind():
    assert "  " not in await _run(["one (two) three"])
