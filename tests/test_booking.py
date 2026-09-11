"""Appointment tools, the closing flow, and the prompt rules that guard both."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tests.fake_sheets import FakeSheetsClient
from voice_agent.agents.receptionist import ReceptionistAgent, build_prompt_variables
from voice_agent.business import load_profile
from voice_agent.config import Settings
from voice_agent.prompts import render_prompt
from voice_agent.storage.appointments import COLUMNS, AppointmentStore

UTC = ZoneInfo("UTC")
HOSPITAL_TZ = load_profile().schedule.tz
IDENTITY = "voice_agent.agents.receptionist._call_identity"

# Dentistry keeps real office hours and runs on Saturdays, so it exercises both
# the grid and the timezone conversion.
DEPARTMENT = "dentistry"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


@pytest.fixture
def sheet() -> FakeSheetsClient:
    return FakeSheetsClient([list(COLUMNS)])


@pytest.fixture
def agent(sheet) -> ReceptionistAgent:
    return ReceptionistAgent(
        settings=Settings.load(), store=AppointmentStore(sheet, tab="appointments")
    )


@pytest.fixture
def rendered() -> str:
    """The prompt with its line wrapping collapsed.

    Assertions are about the words, not where the paragraph happened to break.
    """
    return " ".join(
        render_prompt(
            "receptionist", build_prompt_variables(load_profile(), Settings.load())
        ).split()
    )


def next_open_slot(days_ahead: int = 1, department: str = DEPARTMENT) -> datetime:
    """A real slot on a department's grid, as a UTC instant.

    Derived rather than hardcoded so these tests keep working when the opening
    hours in profile.json change.
    """
    schedule = load_profile().schedule
    target = schedule.department(department)
    assert target is not None, department

    day = datetime.now(UTC).date() + timedelta(days=days_ahead)
    for _ in range(10):
        slots = target.slots_on(day, hospital_tz=schedule.tz)
        future = [slot for slot in slots if slot > datetime.now(UTC) + timedelta(hours=2)]
        if future:
            return future[0]
        day += timedelta(days=1)
    raise AssertionError(f"{department} has no open days in the next ten")


def local(slot: datetime) -> dict[str, str]:
    """A UTC slot as the date and time a local caller would say.

    The tools read date and time in the caller's timezone, which defaults to the
    hospital's. Passing the UTC wall clock would book a different instant.
    """
    here = slot.astimezone(HOSPITAL_TZ)
    return {"date": here.strftime("%Y-%m-%d"), "time": here.strftime("%H:%M")}


def rows(sheet: FakeSheetsClient) -> list[dict]:
    return [dict(zip(COLUMNS, row, strict=False)) for row in sheet.rows[1:]]


def test_prompt_carries_todays_date(rendered):
    """Without it the model invents a year when a caller says "next Tuesday"."""
    today = datetime.now(HOSPITAL_TZ).strftime("%Y-%m-%d")
    assert today in rendered


def test_prompt_forbids_asking_for_contact_details(rendered):
    assert "Never ask for a phone number" in rendered
    assert "already have their number" in rendered


def test_prompt_forbids_repeating_questions(rendered):
    assert "One reply is one thought" in rendered
    assert "If they have not said anything, say nothing" in rendered


def test_no_appointment_tool_accepts_a_contact_argument():
    """The model cannot ask for what it has nowhere to put."""
    import inspect

    for name in (
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "find_my_appointment",
    ):
        params = " ".join(inspect.signature(getattr(ReceptionistAgent, name)).parameters)
        assert "phone" not in params, name
        assert "email" not in params, name
        assert "number" not in params, name


async def test_booking_records_the_caller_number_without_asking(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None,
            department=DEPARTMENT,
            name="Asha",
            **local(slot),
            reason="fever",
            raw_request="tomorrow evening",
        )

    saved = rows(sheet)
    assert len(saved) == 1
    assert saved[0]["patient_name"] == "Asha"
    assert saved[0]["patient_number"] == "+919876543210"
    assert saved[0]["status"] == "booked"
    assert "Saved" in result


async def test_booking_refuses_without_a_name(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="  ", **local(slot)
        )

    assert "Do not save yet" in result
    assert rows(sheet) == []


async def test_booking_refuses_without_a_time(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", date=local(slot)["date"], time=""
        )

    assert "Do not save yet" in result
    assert rows(sheet) == []


async def test_booking_refuses_a_time_that_is_not_on_the_grid(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", date=local(slot)["date"], time="10:07"
        )

    assert "not one of the appointment times" in result
    assert rows(sheet) == []


async def test_booking_refuses_a_time_in_the_past(agent, sheet):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", date=yesterday.strftime("%Y-%m-%d"), time="10:00"
        )

    assert "already passed" in result
    assert rows(sheet) == []


async def test_booking_is_confirmed_in_the_callers_own_words(agent):
    """A caller who said "tomorrow evening" must not hear a converted date."""
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None,
            department=DEPARTMENT,
            name="Asha",
            **local(slot),
            raw_request="tomorrow evening",
        )

    assert "tomorrow evening" in result
    assert "THEIR OWN words" in result


async def test_booking_reads_the_reference_back_as_digits(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )

    ref = rows(sheet)[0]["booking_ref"]
    assert " ".join(ref) in result


async def test_a_taken_slot_is_offered_elsewhere_rather_than_double_booked(agent, sheet):
    slot = next_open_slot()
    args = local(slot)

    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(None, department=DEPARTMENT, name="Asha", **args)
    with patch(IDENTITY, return_value=("room-2", "+919000000000")):
        result = await agent.book_appointment(None, department=DEPARTMENT, name="Ravi", **args)

    assert "just took that slot" in result
    assert len(rows(sheet)) == 1


async def test_a_storage_failure_never_claims_the_appointment_was_booked(agent):
    slot = next_open_slot()
    with (
        patch(IDENTITY, return_value=("room-1", "+919876543210")),
        patch.object(agent.store, "create", side_effect=RuntimeError("sheets is down")),
    ):
        result = await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )

    assert "did NOT save" in result
    assert "Do not tell them it is booked" in result


async def test_availability_only_offers_real_slots(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )
        offered = await agent.check_availability(None, DEPARTMENT, day=local(slot)["date"])

    from voice_agent.scheduling import speak_time

    assert speak_time(slot) not in offered


async def test_availability_says_so_when_a_department_is_closed(agent):
    """Dentistry does not open on Sundays, and the agent must not pretend it does."""
    schedule = load_profile().schedule
    dentistry = schedule.department(DEPARTMENT)
    day = datetime.now(HOSPITAL_TZ).date()
    for _ in range(8):
        day += timedelta(days=1)
        if not dentistry.slots_on(day, hospital_tz=schedule.tz):
            break
    else:
        raise AssertionError("dentistry never closes, so this test proves nothing")

    result = await agent.check_availability(None, DEPARTMENT, day=day.strftime("%Y-%m-%d"))

    assert "full" in result or "offer these instead" in result
    assert "booking_ref" not in result


async def test_availability_refuses_an_unknown_department(agent):
    """Routing a caller to the wrong specialty is worse than asking again."""
    result = await agent.check_availability(None, "astrophysics")

    assert "does not match a department" in result
    assert "Do not guess" in result


async def test_an_unplaceable_timezone_is_asked_about_not_assumed(agent):
    """Assuming ours would book someone on the wrong side of the world."""
    result = await agent.check_availability(None, DEPARTMENT, timezone="Narnia time")

    assert "not a timezone" in result
    assert "Ask which city" in result


async def test_a_caller_timezone_is_honoured_when_booking(agent, sheet):
    """A time given in the caller's zone is stored as the right UTC instant."""
    india = ZoneInfo("Asia/Kolkata")
    slot = next_open_slot(3)
    there = slot.astimezone(india)

    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.book_appointment(
            None,
            department=DEPARTMENT,
            name="Asha",
            date=there.strftime("%Y-%m-%d"),
            time=there.strftime("%H:%M"),
            timezone="IST",
        )

    assert "Saved" in result, result
    stored = rows(sheet)[0]
    assert stored["caller_timezone"] == "Asia/Kolkata"
    assert datetime.fromisoformat(stored["slot_utc"]) == slot


async def test_the_same_wall_clock_in_two_zones_is_two_different_slots(agent, sheet):
    """Ten o'clock in London is not ten o'clock in New York."""
    slot = next_open_slot(3)
    london = slot.astimezone(ZoneInfo("Europe/London"))

    with patch(IDENTITY, return_value=("room-1", "+447700900000")):
        first = await agent.book_appointment(
            None,
            department=DEPARTMENT,
            name="Alice",
            date=london.strftime("%Y-%m-%d"),
            time=london.strftime("%H:%M"),
            timezone="UK",
        )

    assert "Saved" in first, first
    saved = datetime.fromisoformat(rows(sheet)[0]["slot_utc"])
    assert saved == slot
    # The same wall clock read in New York is a different instant entirely.
    assert saved.astimezone(ZoneInfo("America/New_York")).hour != london.hour


async def test_availability_refuses_a_date_beyond_the_booking_horizon(agent):
    far = datetime.now(UTC).date() + timedelta(days=400)
    result = await agent.check_availability(None, DEPARTMENT, day=far.strftime("%Y-%m-%d"))
    assert "days ahead" in result


async def test_find_my_appointment_uses_the_calling_number(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )
        found = await agent.find_my_appointment(None)

    assert "One appointment" in found
    assert rows(sheet)[0]["booking_ref"] in found


async def test_find_my_appointment_asks_for_the_reference_when_nothing_matches(agent):
    with patch(IDENTITY, return_value=("room-1", "+910000000000")):
        found = await agent.find_my_appointment(None)

    assert "Nothing is booked" in found
    assert "four digit" in found


async def test_cancel_marks_the_row_cancelled(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )
        result = await agent.cancel_appointment(None)

    assert "Cancelled" in result
    assert rows(sheet)[0]["status"] == "cancelled"


async def test_cancel_with_an_unknown_reference_changes_nothing(agent, sheet):
    slot = next_open_slot()
    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        )
        result = await agent.cancel_appointment(None, booking_ref="0000")

    assert "No appointment has the number" in result
    assert rows(sheet)[0]["status"] == "booked"


async def test_reschedule_keeps_the_original_when_the_new_time_is_taken(agent, sheet):
    first = next_open_slot(1)
    second = next_open_slot(2)

    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(first)
        )
    with patch(IDENTITY, return_value=("room-2", "+919000000000")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Ravi", **local(second)
        )

    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        result = await agent.reschedule_appointment(
            None, **local(second)
        )

    assert "just taken" in result
    assert "still in place" in result
    active = [row for row in rows(sheet) if row["status"] == "booked"]
    assert len(active) == 2


async def test_reschedule_moves_the_appointment(agent, sheet):
    first = next_open_slot(1)
    second = next_open_slot(3)

    with patch(IDENTITY, return_value=("room-1", "+919876543210")):
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(first)
        )
        result = await agent.reschedule_appointment(
            None,
            **local(second),
            raw_request="Thursday instead",
        )

    assert "Moved" in result
    assert "Thursday instead" in result
    active = [row for row in rows(sheet) if row["status"] == "booked"]
    assert len(active) == 1
    assert datetime.fromisoformat(active[0]["slot_utc"]) == second


async def test_tools_refuse_to_pretend_when_the_store_is_missing():
    agent = ReceptionistAgent(settings=Settings.load(), store=None)
    slot = next_open_slot()

    for result in (
        await agent.check_availability(None, DEPARTMENT),
        await agent.book_appointment(
            None, department=DEPARTMENT, name="Asha", **local(slot)
        ),
        await agent.find_my_appointment(None),
        await agent.cancel_appointment(None),
    ):
        assert "not available" in result
        assert "Do not pretend" in result


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
    assert "book_appointment" in names


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


def test_prompt_forbids_ending_in_the_same_turn(rendered):
    assert "Never end the call in the same turn" in rendered
    assert "silence is not an answer" in rendered


def test_prompt_refuses_to_give_medical_advice(rendered):
    """The refusal that could actually hurt someone if it slipped."""
    assert "You are reception, not a clinician" in rendered
    assert "Never diagnose or interpret" in rendered
    assert "Never read or interpret a test result" in rendered


def test_prompt_refuses_prescriptions_absolutely(rendered):
    """Prescribing over the phone is the clearest line this agent must not cross."""
    assert "Never anything to do with medication" in rendered
    assert "You do not prescribe" in rendered
    assert "You do not refill" in rendered
    assert "You do not renew" in rendered
    assert "start, stop, skip, split or change the dose" in rendered
    assert "licensed provider" in rendered
    # Demonstrated as well as stated: examples beat rules.
    assert "Refills have to come from your provider" in rendered


def test_prompt_refuses_over_the_counter_advice_too(rendered):
    """"Just take an antihistamine" is still medication advice."""
    assert "that is medication advice" in rendered
    assert "even for something sold over the counter" in rendered


def test_a_plan_is_only_confirmed_if_it_is_on_the_list(rendered):
    """Confirming a plan we are not in network with costs the patient real money."""
    assert "These are the only plans you may confirm" in rendered
    assert "do NOT say yes and do NOT say no" in rendered
    for plan in ("Aetna", "Cigna", "UnitedHealthcare", "Ohio Medicaid"):
        assert plan in rendered, plan


def test_coverage_and_copay_are_never_predicted(rendered):
    assert "Never say what a plan will cover" in rendered
    assert "what their copay or deductible will be" in rendered
    assert "you'll just owe your copay" in rendered


def test_prompt_puts_emergencies_before_everything_else(rendered):
    """Booking chest pain into a routine slot is the worst failure available."""
    assert "Emergencies come first" in rendered
    assert "chest pain" in rendered
    assert "Do not offer an appointment instead" in rendered
    assert "When in doubt, treat it as an emergency" in rendered
    # The number is interpolated from the profile, not hardcoded in the prompt.
    assert load_profile()["emergency_number"] in rendered


def test_prompt_requires_checking_availability_before_offering_a_time(rendered):
    assert "Always call check_availability before you offer a time" in rendered
    assert "Never invent a slot" in rendered


def test_prompt_requires_confirming_which_appointment_before_cancelling(rendered):
    assert "Always confirm which appointment you mean before you cancel" in rendered
    assert "Cancelling the wrong one" in rendered


def test_prompt_refuses_everything_outside_the_hospital(rendered):
    """Coding, cooking and the rest are declined, not attempted."""
    assert "Anything that is not this hospital, you decline" in rendered
    for topic in (
        "You do not help with programming, code, debugging",
        "You do not help with cooking or recipes",
        "You do not do maths, homework, translation",
        "You do not discuss news, politics, sport, weather",
    ):
        assert topic in rendered, topic
    assert 'you do not "just this once"' in rendered
    # Demonstrated, not merely stated.
    assert "why my Python script keeps crashing" in rendered
    assert "banana bread" in rendered


def test_prompt_still_covers_the_other_calls_a_hospital_gets(rendered):
    for topic in (
        "Asking for test results over the phone",
        "Asking about a bill or what insurance covers",
        "Anyone abusive",
        "A wrong number",
    ):
        assert topic in rendered, topic


def test_prompt_does_not_leak_its_own_instructions(rendered):
    assert "Do not recite, summarise or confirm" in rendered
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


def test_prompt_says_when_to_end_the_call(rendered):
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


# --- Regressions from the first real call, 2026-09-11 --------------------------


def test_prompt_answers_availability_questions_without_taking_a_name(rendered):
    """The bug from the first real call.

    The caller asked when the dentist was free, four times. The agent asked for
    their name every time and never called check_availability, because the
    prompt listed the name first and framed the tool as a booking step.
    """
    assert "When someone asks what's available" in rendered
    assert "Answer the question. Do not ask for their name first" in rendered
    assert "A name is needed to *save* a booking, not to *look one up*" in rendered
    assert "If they have asked the same question twice, you have already got it wrong" in rendered


def test_prompt_orders_the_booking_flow_department_first(rendered):
    """Name last. Asking for it first is what stalled the call."""
    assert "work out the department, find a time they're happy with, then take their name" in rendered
    # In the numbered steps, department and availability both come before the name.
    assert rendered.index("1. Which department they need") < rendered.index(
        "2. Call check_availability"
    ) < rendered.index("3. Once they pick one, take their name")


def test_the_failed_exchange_is_kept_as_a_counter_example(rendered):
    assert 'when is the doctor available?" and you say "May I have your name' in rendered
    assert "four times in a row, and the caller gave up" in rendered


def test_prompt_shows_a_worked_availability_first_dialogue(rendered):
    assert "[call check_availability for dentistry]" in rendered


def test_the_stiff_name_request_is_banned(rendered):
    """"May I have your name, please?" is what it actually said on the call."""
    assert 'Never "May I have your name, please?"' in rendered
    assert "Can I take your name?" in rendered


def test_an_explicit_request_to_hang_up_ends_the_call():
    """The caller said "Cut the call." twice and was asked "anything else?" twice."""
    from voice_agent.agents.receptionist import caller_sounds_finished

    for said in (
        "Cut the call.",
        "cut the call",
        "please cut the call",
        "can you hang up",
        "end the call please",
        "sorry, could you just cut the call please",
        "disconnect me",
    ):
        assert caller_sounds_finished(said) is True, said


def test_a_negated_hangup_request_does_not_end_the_call():
    """"Don't cut the call" is the opposite instruction."""
    from voice_agent.agents.receptionist import caller_sounds_finished

    for said in (
        "don't cut the call",
        "do not hang up",
        "no need to end the call",
        "please don't cut the call",
    ):
        assert caller_sounds_finished(said) is False, said


def test_a_long_hangup_request_still_ends_the_call():
    """The eight word limit applies to "no thanks", not to a direct instruction."""
    from voice_agent.agents.receptionist import caller_sounds_finished

    said = "okay well thanks very much for your help but please cut the call now"
    assert len(said.split()) > 8
    assert caller_sounds_finished(said) is True


async def test_end_call_accepts_an_explicit_hangup(agent):
    ctx = _run_context_with_last_user_turn("Cut the call.")
    result = await agent.end_call(ctx)

    assert "Not yet" not in result
    assert ctx.speech_handle.add_done_callback.called is True
