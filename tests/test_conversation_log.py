"""The conversation log is the only readable record of a call on LiveKit Cloud.

The transcript JSON is written to an ephemeral disk there, so if these lines are
wrong or missing, a call is undebuggable after the fact.
"""

from __future__ import annotations

import json
import logging

import pytest
from livekit.agents import ChatMessage
from livekit.agents.llm import FunctionCall, FunctionCallOutput
from livekit.agents.voice.events import (
    ConversationItemAddedEvent,
    FunctionToolsExecutedEvent,
    UserInputTranscribedEvent,
    UserTranscriptionTimeoutEvent,
)

from voice_agent.observability.conversation import attach_conversation_logging


class FakeSession:
    """Just the .on() surface the logger subscribes to."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def on(self, name: str):
        def register(fn):
            self._handlers.setdefault(name, []).append(fn)
            return fn

        return register

    def emit(self, name: str, event) -> None:
        for fn in self._handlers.get(name, []):
            fn(event)


@pytest.fixture
def session_and_log(caplog):
    caplog.set_level(logging.DEBUG, logger="voice_agent.conversation")
    session = FakeSession()
    attach_conversation_logging(session)
    return session, caplog


def test_final_user_speech_is_logged(session_and_log):
    session, caplog = session_and_log
    session.emit(
        "user_input_transcribed",
        UserInputTranscribedEvent(transcript="you can do it at two PM", is_final=True),
    )
    assert ">> user | you can do it at two PM" in caplog.text


def test_interim_transcripts_are_off_by_default(session_and_log):
    """Deepgram revises a partial several times a second."""
    session, caplog = session_and_log
    session.emit(
        "user_input_transcribed",
        UserInputTranscribedEvent(transcript="you can do", is_final=False),
    )
    assert "you can do" not in caplog.text


def test_interim_transcripts_can_be_turned_on(caplog):
    caplog.set_level(logging.DEBUG, logger="voice_agent.conversation")
    session = FakeSession()
    attach_conversation_logging(session, include_interim=True)
    session.emit(
        "user_input_transcribed",
        UserInputTranscribedEvent(transcript="you can do", is_final=False),
    )
    assert ".. user | you can do" in caplog.text


def test_agent_speech_is_logged(session_and_log):
    session, caplog = session_and_log
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(
            item=ChatMessage(role="assistant", content=["Morning or afternoon?"])
        ),
    )
    assert "<< agent | Morning or afternoon?" in caplog.text


def test_a_multi_line_agent_reply_stays_on_one_log_line(session_and_log):
    """A stacked reply is itself the signal; splitting it hides the shape."""
    session, caplog = session_and_log
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(
            item=ChatMessage(
                role="assistant",
                content=["Morning or afternoon?\nWhat time works?\nAnything else?"],
            )
        ),
    )
    line = next(r for r in caplog.records if "<< agent" in r.getMessage())
    assert "\n" not in line.getMessage()
    assert "Morning or afternoon? What time works? Anything else?" in line.getMessage()


def test_user_items_are_not_logged_twice(session_and_log):
    """User text arrives via user_input_transcribed, sooner and with a language."""
    session, caplog = session_and_log
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(item=ChatMessage(role="user", content=["hello"])),
    )
    assert "hello" not in caplog.text


def test_tool_calls_log_arguments_and_result(session_and_log):
    session, caplog = session_and_log
    session.emit(
        "function_tools_executed",
        FunctionToolsExecutedEvent(
            function_calls=[
                FunctionCall(
                    call_id="1",
                    name="book_callback",
                    arguments=json.dumps({"name": "Rajesh", "preferred_time": "14:00"}),
                )
            ],
            function_call_outputs=[
                FunctionCallOutput(
                    call_id="1", name="book_callback", output="Booked.", is_error=False
                )
            ],
        ),
    )
    assert "-- tool | book_callback(" in caplog.text
    assert "name='Rajesh'" in caplog.text
    assert "preferred_time='14:00'" in caplog.text
    assert "-> Booked." in caplog.text


def test_a_tool_with_no_output_is_still_logged(session_and_log):
    """end_call returns nothing; silence here would hide the hangup."""
    session, caplog = session_and_log
    session.emit(
        "function_tools_executed",
        FunctionToolsExecutedEvent(
            function_calls=[FunctionCall(call_id="2", name="end_call", arguments="{}")],
            function_call_outputs=[None],
        ),
    )
    assert "-- tool | end_call() -> (no output)" in caplog.text


def test_malformed_tool_arguments_do_not_raise(session_and_log):
    session, caplog = session_and_log
    session.emit(
        "function_tools_executed",
        FunctionToolsExecutedEvent(
            function_calls=[
                FunctionCall(call_id="3", name="book_callback", arguments="not json")
            ],
            function_call_outputs=[None],
        ),
    )
    assert "book_callback(not json)" in caplog.text


def test_speech_with_no_transcript_is_warned_about(session_and_log):
    """The signature of the stacked-question bug, visible as it happens."""
    session, caplog = session_and_log
    session.emit(
        "user_transcription_timeout",
        UserTranscriptionTimeoutEvent(speech_duration=1.4, vad_speech_started_at=0.0),
    )
    assert "!! user | 1.4s of speech produced no transcript" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_empty_text_is_not_logged(session_and_log):
    session, caplog = session_and_log
    session.emit(
        "user_input_transcribed",
        UserInputTranscribedEvent(transcript="   ", is_final=True),
    )
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(item=ChatMessage(role="assistant", content=[""])),
    )
    assert ">> user" not in caplog.text
    assert "<< agent" not in caplog.text


def test_agent_line_reports_what_the_caller_actually_waited(session_and_log):
    """The commit event fires after the reply finishes playing.

    Without the measured figure on the line, the log timestamp overstates
    latency by the whole duration of the agent's speech.
    """
    session, caplog = session_and_log
    item = ChatMessage(role="assistant", content=["Morning or afternoon?"])
    item.metrics = {"e2e_latency": 1.4}
    session.emit("conversation_item_added", ConversationItemAddedEvent(item=item))
    assert "[waited 1.4s]" in caplog.text


def test_agent_line_omits_the_wait_when_it_was_not_measured(session_and_log):
    """The greeting has no user turn before it, so there is nothing to measure."""
    session, caplog = session_and_log
    session.emit(
        "conversation_item_added",
        ConversationItemAddedEvent(
            item=ChatMessage(role="assistant", content=["Thank you for calling."])
        ),
    )
    assert "<< agent | Thank you for calling." in caplog.text
    assert "waited" not in caplog.text
