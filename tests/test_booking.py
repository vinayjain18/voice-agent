"""Booking flow: date injection, mandatory time, and caller-number reuse."""

from __future__ import annotations

import csv
from unittest.mock import patch

import pytest

from voice_agent.agents.receptionist import ReceptionistAgent, build_prompt_variables
from voice_agent.business import load_profile
from voice_agent.config import Settings


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return ReceptionistAgent(settings=Settings.load())


def _rows(tmp_path):
    return list(csv.DictReader((tmp_path / "leads.csv").open()))


def test_prompt_carries_todays_date():
    """Without this the model invents a date for 'next Tuesday'."""
    variables = build_prompt_variables(load_profile(), Settings.load())
    assert variables["current_date"].count("-") == 2
    assert variables["current_datetime"]


def test_prompt_forbids_asking_for_contact_details():
    """It nagged a real caller for a number it did not need."""
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Never ask for a phone number" in rendered
    assert "email address" in rendered


def test_prompt_forbids_repeating_questions():
    """It re-asked the same question repeatedly into silence."""
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Never repeat a question" in rendered
    assert "Never send several messages in a row" in rendered


def test_booking_tool_has_no_contact_argument():
    """The model cannot ask for what it cannot pass."""
    import inspect

    from voice_agent.agents.receptionist import ReceptionistAgent

    params = inspect.signature(ReceptionistAgent.book_callback).parameters
    assert "phone_or_email" not in params
    assert "email" not in " ".join(params)


async def test_booking_records_caller_number_silently(agent, tmp_path):
    """The number is captured from the call, never requested."""
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.book_callback(
            None, name="Raj", preferred_date="2026-08-25",
            preferred_time="15:00", reason="voice agent",
            raw_request="next Tuesday at 3",
        )
    assert "Booked" in result
    row = _rows(tmp_path)[0]
    assert row["kind"] == "booking"
    assert row["contact"] == "+919876543210"
    assert row["caller_number"] == "+919876543210"
    assert row["preferred_date"] == "2026-08-25"
    assert row["preferred_time"] == "15:00"


async def test_booking_refuses_without_a_time(agent, tmp_path):
    """The real bug: a lead was saved with no time because none was asked for."""
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.book_callback(
            None, name="Raj", preferred_date="2026-08-25", preferred_time="",
            reason="x",
        )
    assert "no time was given" in result
    assert not (tmp_path / "leads.csv").exists()


async def test_booking_refuses_without_name(agent, tmp_path):
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.book_callback(
            None, name="", preferred_date="2026-08-25", preferred_time="15:00",
            reason="x",
        )
    assert "Do not save yet" in result
    assert not (tmp_path / "leads.csv").exists()


async def test_message_requires_a_reason_no_time_was_given(agent, tmp_path):
    """Forces the model to have actually asked for a time."""
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.take_callback_details(
            None, name="Raj", reason="x", why_no_time="",
        )
    assert "what day and time" in result
    assert not (tmp_path / "leads.csv").exists()


async def test_booking_works_without_a_caller_number(agent, tmp_path):
    """Console and browser sessions have no number. That must not block a booking."""
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("console-1", ""),
    ):
        result = await agent.book_callback(
            None, name="Raj", preferred_date="2026-08-25",
            preferred_time="15:00", reason="x",
        )
    assert "Booked" in result
    assert _rows(tmp_path)[0]["contact"] == ""


async def test_message_saved_with_justification(agent, tmp_path):
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.take_callback_details(
            None, name="Raj", reason="pricing",
            why_no_time="wants to check their calendar",
        )
    assert "Noted" in result
    row = _rows(tmp_path)[0]
    assert row["kind"] == "message"
    assert row["raw_request"] == "wants to check their calendar"
    assert row["contact"] == "+919876543210"


# --- ending the call --------------------------------------------------------

def test_agent_has_the_end_call_tool():
    """Without it the caller is left on a silent line after the booking."""
    from livekit.agents.beta.tools import EndCallTool

    agent = ReceptionistAgent()
    toolsets = [t for t in agent._tools if isinstance(t, EndCallTool)]
    assert toolsets, "EndCallTool is not registered"
    assert "end_call" in [t.info.name for t in toolsets[0].tools]


def test_end_call_cannot_fire_during_the_greeting():
    """ignore_on_enter stops the model hanging up while saying hello."""
    from livekit.agents.beta.tools import EndCallTool
    from livekit.agents.llm import ToolFlag

    agent = ReceptionistAgent()
    toolset = next(t for t in agent._tools if isinstance(t, EndCallTool))
    tool = toolset.tools[0]
    assert tool.info.flags & ToolFlag.IGNORE_ON_ENTER


def test_end_call_deletes_the_room():
    """Deleting the room is what actually disconnects a phone or WhatsApp caller."""
    from livekit.agents.beta.tools import EndCallTool

    agent = ReceptionistAgent()
    toolset = next(t for t in agent._tools if isinstance(t, EndCallTool))
    assert toolset._delete_room is True


def test_prompt_says_when_to_end_the_call():
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "end_call" in rendered
    assert "anything else" in rendered
