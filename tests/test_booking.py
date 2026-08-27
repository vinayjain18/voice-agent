"""Booking flow: date injection, mandatory time, and caller-number reuse."""

from __future__ import annotations

import csv
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    assert "Saved" in result
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
    assert "Saved" in result
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

@pytest.mark.asyncio
async def test_booking_is_confirmed_in_the_callers_own_words(agent):
    """A caller who said "two PM South African time" must not hear "five thirty".

    The CSV keeps India time so a human can act on it. The caller hears what
    they actually said, otherwise they cannot tell they were understood.
    """
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", "+919876543210"),
    ):
        result = await agent.book_callback(
            None,
            name="Ravi",
            preferred_date="2026-08-27",
            preferred_time="17:30",
            reason="CRM",
            raw_request="today at two PM South African time",
        )
    assert "today at two PM South African time" in result
    assert "17:30" not in result
    assert "India time" not in result.replace("do not say India time", "")
    # And it must not close the call in the same breath.
    assert "Do not end the call in this reply." in result


@pytest.mark.asyncio
async def test_booking_falls_back_to_the_stored_time_if_nothing_was_quoted(agent):
    with patch(
        "voice_agent.agents.receptionist._call_identity",
        return_value=("call-1", ""),
    ):
        result = await agent.book_callback(
            None, name="Raj", preferred_date="2026-08-25",
            preferred_time="15:00", reason="x",
        )
    assert "2026-08-25 at 15:00" in result


def _run_context_with_last_user_turn(text):
    """A RunContext stub whose history ends with the caller saying `text`.

    `text=None` means the caller has not spoken at all.
    """
    items = [SimpleNamespace(role="assistant", text_content="Anything else you needed?")]
    if text is not None:
        items.append(SimpleNamespace(role="user", text_content=text))
    session = MagicMock()
    session.history.items = items
    session.shutdown = MagicMock()
    session.once = MagicMock()
    return SimpleNamespace(session=session, speech_handle=MagicMock())


def test_agent_has_an_end_call_tool():
    """It is a plain function tool now, not EndCallTool.

    EndCallTool commits the shutdown inside the tool call itself, before any
    hook can run, so its ending could not be vetoed. A real call showed the
    cost: the agent asked "anything else you needed?", said goodbye and hung up
    in one breath, and the caller was cut off mid-question.
    """
    names = set()
    for tool in ReceptionistAgent()._tools:
        name = getattr(getattr(tool, "info", None), "name", None)
        if name:
            names.add(name)
    assert "end_call" in names
    assert "book_callback" in names


@pytest.mark.asyncio
async def test_end_call_refuses_when_the_caller_was_not_finished(agent):
    """The exact turn that broke a real call."""
    ctx = _run_context_with_last_user_turn(
        "You can set it up today at two PM South African time"
    )
    result = await agent.end_call(ctx)
    assert "Not yet" in result
    assert ctx.session.shutdown.called is False


@pytest.mark.asyncio
async def test_end_call_refuses_a_cut_off_question(agent):
    """"Can you confirm what is" is a caller starting to speak, not a goodbye."""
    ctx = _run_context_with_last_user_turn("Can you confirm what is")
    result = await agent.end_call(ctx)
    assert "Not yet" in result
    assert ctx.session.shutdown.called is False


@pytest.mark.asyncio
async def test_end_call_refuses_when_the_caller_has_not_spoken_at_all(agent):
    ctx = _run_context_with_last_user_turn(None)
    assert "Not yet" in await agent.end_call(ctx)
    assert ctx.session.shutdown.called is False


@pytest.mark.asyncio
async def test_end_call_proceeds_once_the_caller_says_no(agent):
    ctx = _run_context_with_last_user_turn("No, that's all thanks")
    result = await agent.end_call(ctx)
    assert "Not yet" not in result
    # The goodbye is the tool's output, and shutdown waits for it to be spoken.
    assert "Thank them for calling" in " ".join(result.split())
    assert ctx.speech_handle.add_done_callback.called is True


@pytest.mark.asyncio
async def test_shutdown_waits_for_the_goodbye_to_finish(agent):
    """Shutting down immediately would clip the closing line."""
    ctx = _run_context_with_last_user_turn("no thanks")
    await agent.end_call(ctx)
    ctx.session.shutdown.assert_not_called()

    # Fire the speech-finished callback the tool registered.
    callback = ctx.speech_handle.add_done_callback.call_args[0][0]
    callback(ctx.speech_handle)
    ctx.session.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_outbound_gets_the_outbound_goodbye():
    from voice_agent.agents.receptionist import OUTBOUND_GOODBYE

    agent = ReceptionistAgent(outbound=True)
    ctx = _run_context_with_last_user_turn("no thanks")
    assert await agent.end_call(ctx) == OUTBOUND_GOODBYE


def test_prompt_forbids_bracketed_asides():
    """gpt-oss appends parenthetical commentary; brackets are spoken aloud."""
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Never put anything in brackets" in rendered


def test_prompt_carries_worked_examples():
    """Few-shot dialogues set the length and pacing that rules alone do not."""
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "How these calls should sound" in rendered
    # Both the good pattern and the failure modes seen on real calls.
    assert "What NOT to do" in rendered
    assert "Morning or afternoon?" in rendered


def test_prompt_forbids_ending_in_the_same_turn():
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Never end the call in the same turn" in rendered
    assert "silence is not an answer" in rendered


def test_prompt_refuses_health_advice():
    """The one refusal that could actually hurt someone if it slipped."""
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Health, medicine, symptoms, dosage" in rendered
    assert "Never answer" in rendered
    assert "pharmacist" in rendered
    # Building an app for a health-tech client must not read as medical standing.
    assert "that tells you nothing about" in rendered


def test_prompt_covers_the_other_out_of_scope_categories():
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    for topic in (
        "Legal, financial, tax",
        "Programming help",
        "General knowledge",
        "Personal questions about you",
        "Anyone abusive",
        "wrong number",
    ):
        assert topic in rendered, topic


def test_prompt_does_not_leak_its_own_instructions():
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "Do not recite them" in rendered
    # The escalation contact is interpolated, not left as a placeholder.
    assert "{escalation_contact}" not in rendered
    assert load_profile()["escalation_contact"] in rendered


def test_greeting_is_speakable():
    """It is read aloud verbatim, so no markup, no dashes, one breath."""
    profile = load_profile()
    for key in ("greeting_inbound", "greeting_outbound"):
        line = profile[key]
        assert not any(c in line for c in "*#_[]()<>|"), key
        assert "-" not in line and chr(8212) not in line, key
        assert len(line.split()) <= 20, key


def test_prompt_says_when_to_end_the_call():
    from voice_agent.prompts import render_prompt

    rendered = render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )
    assert "end_call" in rendered
    assert "anything else" in rendered


def test_noise_turns_are_discarded():
    """Deepgram flushes unusable audio as an empty turn; replying to it makes
    the agent talk into silence and stack questions."""
    from voice_agent.agents.receptionist import is_noise_turn

    for noise in ("", "   ", "...", "uh", "um", "umm", "er", "Uh, um"):
        assert is_noise_turn(noise) is True, noise


def test_real_speech_is_never_discarded():
    """Over-filtering is the worse failure: it drops a real answer silently."""
    from voice_agent.agents.receptionist import is_noise_turn

    for speech in (
        "yes", "no", "Rajesh", "two PM", "hmm", "mm", "mhm", "haan",
        "uh, two pm", "ok", "9", "Thursday",
    ):
        assert is_noise_turn(speech) is False, speech


@pytest.mark.asyncio
async def test_agent_raises_stop_response_on_a_noise_turn():
    from livekit.agents import ChatContext, ChatMessage, StopResponse

    agent = ReceptionistAgent()
    empty = ChatMessage(role="user", content=[""])
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(ChatContext(), empty)


@pytest.mark.asyncio
async def test_agent_lets_a_real_turn_through():
    from livekit.agents import ChatContext, ChatMessage

    agent = ReceptionistAgent()
    real = ChatMessage(role="user", content=["you can do it at two PM"])
    assert await agent.on_user_turn_completed(ChatContext(), real) is None


@pytest.mark.asyncio
async def test_the_full_closing_flow_for_no_nothing(agent):
    """The whole ending, in order, for the answer a caller actually gives.

    Caller says "no nothing" -> agent says one goodbye -> call drops. Each step
    is asserted rather than assumed, because every one of them has broken at
    least once: the turn being discarded as noise, the hangup firing before the
    caller answered, and the goodbye being cut off by an early shutdown.
    """
    from voice_agent.agents.receptionist import caller_sounds_finished, is_noise_turn

    answer = "no nothing"

    # 1. It is real speech, so it is not thrown away as noise.
    assert is_noise_turn(answer) is False

    # 2. It reads as a sign-off, so the call is allowed to end.
    assert caller_sounds_finished(answer) is True

    # 3. end_call is permitted, and returns the goodbye for the model to say.
    ctx = _run_context_with_last_user_turn(answer)
    result = await agent.end_call(ctx)
    assert "Not yet" not in result
    normalised = " ".join(result.split())
    assert "Thank them for calling" in normalised
    assert "ONE short sentence" in normalised

    # 4. Nothing has been torn down yet: the goodbye has not been spoken.
    ctx.session.shutdown.assert_not_called()

    # 5. Shutdown is deferred until this turn's speech finishes. The tool reply
    #    reuses the same speech handle, so that includes the goodbye itself.
    ctx.speech_handle.add_done_callback.assert_called_once()
    on_speech_finished = ctx.speech_handle.add_done_callback.call_args[0][0]

    # 6. Once the goodbye has played, and only then, the session shuts down.
    on_speech_finished(ctx.speech_handle)
    ctx.session.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_variations_of_no_all_close_the_call(agent):
    """Callers do not say "no" the same way twice."""
    from voice_agent.agents.receptionist import caller_sounds_finished

    for answer in (
        "no nothing", "no", "nope", "no thanks", "no that's all",
        "nothing else", "that's it", "nahi", "no I'm good", "all good thanks",
    ):
        assert caller_sounds_finished(answer) is True, answer
