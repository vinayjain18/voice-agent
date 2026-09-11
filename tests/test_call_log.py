"""The calls tab: one row per call, so an admin can see why someone rang."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fake_sheets import FakeSheetsClient
from voice_agent.observability.summary import (
    conversation_lines,
    fallback_summary,
    infer_outcome,
    render_transcript,
    summarise_call,
)
from voice_agent.storage.calls import (
    COLUMNS,
    MAX_SUMMARY_CHARS,
    OUTCOME_BOOKED,
    OUTCOME_CANCELLED,
    OUTCOME_ENQUIRY,
    OUTCOME_NO_ACTION,
    OUTCOME_RESCHEDULED,
    CallLog,
    build_record,
)


class FakeHistory:
    def __init__(self, items):
        self.items = items


def message(role: str, text: str):
    return SimpleNamespace(role=role, text_content=text, type="message", name=None)


def tool(name: str):
    return SimpleNamespace(role="tool", text_content="", type="function_call", name=name)


CONVERSATION = FakeHistory(
    [
        message("assistant", "Thank you for calling. This is Emma."),
        message("user", "I need to see a dentist, my tooth is aching."),
        tool("check_availability"),
        message("assistant", "I've got half four tomorrow."),
        message("user", "That works."),
        tool("book_appointment"),
    ]
)


def test_conversation_lines_keep_only_what_was_said():
    lines = conversation_lines(CONVERSATION)
    assert [role for role, _ in lines] == ["assistant", "user", "assistant", "user"]
    assert "tooth is aching" in lines[1][1]


def test_transcript_is_labelled_for_a_reader():
    text = render_transcript(CONVERSATION)
    assert "Caller: I need to see a dentist" in text
    assert "Receptionist: Thank you for calling" in text


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["book_appointment"], OUTCOME_BOOKED),
        (["cancel_appointment"], OUTCOME_CANCELLED),
        (["reschedule_appointment"], OUTCOME_RESCHEDULED),
        (["check_availability"], OUTCOME_ENQUIRY),
        ([], OUTCOME_ENQUIRY),
    ],
)
def test_outcome_comes_from_the_tools_that_actually_ran(tools, expected):
    history = FakeHistory([message("user", "hello"), *[tool(name) for name in tools]])
    assert infer_outcome(history) == expected


def test_a_reschedule_outranks_the_booking_it_creates():
    """reschedule_appointment books internally, so order of checks matters."""
    history = FakeHistory(
        [message("user", "move it"), tool("book_appointment"), tool("reschedule_appointment")]
    )
    assert infer_outcome(history) == OUTCOME_RESCHEDULED


def test_a_silent_call_is_marked_as_such():
    assert infer_outcome(FakeHistory([])) == OUTCOME_NO_ACTION


def test_the_fallback_summary_is_what_the_caller_first_said():
    assert "tooth is aching" in fallback_summary(CONVERSATION)


async def test_summarise_uses_the_llm_when_it_works():
    class GoodLLM:
        def chat(self, chat_ctx=None):
            async def stream():
                for part in ("Caller booked ", "a dental appointment."):
                    yield SimpleNamespace(delta=SimpleNamespace(content=part))

            return stream()

    session = SimpleNamespace(history=CONVERSATION)
    assert await summarise_call(session, GoodLLM()) == "Caller booked a dental appointment."


async def test_summarise_falls_back_when_the_llm_fails():
    """A failed summary must never lose the record of the call."""

    class BrokenLLM:
        def chat(self, chat_ctx=None):
            raise RuntimeError("model is down")

    session = SimpleNamespace(history=CONVERSATION)
    summary = await summarise_call(session, BrokenLLM())

    assert "tooth is aching" in summary


async def test_summarise_handles_a_call_where_nothing_was_said():
    session = SimpleNamespace(history=FakeHistory([]))
    assert "nothing was said" in await summarise_call(session, object())


async def test_a_row_is_appended_with_a_header_on_first_use():
    client = FakeSheetsClient([])
    log = CallLog(client, tab="calls")

    ok = await log.append(
        build_record(
            direction="inbound",
            caller_number="+15551234567",
            duration_seconds=91.4,
            summary="Caller booked a dental appointment for tomorrow.",
            outcome=OUTCOME_BOOKED,
            department="dentistry",
            booking_ref="4291",
            turns=6,
            cost_usd=0.0123,
            room="wa-1",
        )
    )

    assert ok is True
    assert client.rows[0] == COLUMNS
    row = dict(zip(COLUMNS, client.rows[1], strict=False))
    assert row["outcome"] == OUTCOME_BOOKED
    assert row["booking_ref"] == "4291"
    assert row["duration_seconds"] == "91"
    assert row["cost_usd"] == "0.01230"


async def test_existing_call_rows_are_never_disturbed():
    client = FakeSheetsClient([list(COLUMNS), ["old", "inbound", "+1", "10", "enquiry", "", "", "x", "2", "", ""]])
    log = CallLog(client, tab="calls")

    await log.append(
        build_record(
            direction="inbound",
            caller_number="+1",
            duration_seconds=5,
            summary="Asked about parking.",
            outcome=OUTCOME_ENQUIRY,
        )
    )

    assert client.rows[1][0] == "old"
    assert len(client.rows) == 3


async def test_a_foreign_header_is_refused_and_logged_not_raised():
    """This runs after the call; it must never throw."""
    client = FakeSheetsClient([["something", "else"], ["keep", "me"]])

    ok = await CallLog(client, tab="calls").append(
        build_record(
            direction="inbound",
            caller_number="+1",
            duration_seconds=5,
            summary="x",
            outcome=OUTCOME_ENQUIRY,
        )
    )

    assert ok is False
    assert client.rows[1] == ["keep", "me"]


async def test_a_broken_sheet_does_not_raise():
    class BrokenClient(FakeSheetsClient):
        async def read_rows(self, tab):
            raise RuntimeError("sheets is down")

    ok = await CallLog(BrokenClient([]), tab="calls").append(
        build_record(
            direction="inbound",
            caller_number="+1",
            duration_seconds=1,
            summary="x",
            outcome=OUTCOME_ENQUIRY,
        )
    )
    assert ok is False


def test_a_long_summary_is_trimmed_to_fit_a_cell():
    record = build_record(
        direction="inbound",
        caller_number="+1",
        duration_seconds=1,
        summary="word " * 500,
        outcome=OUTCOME_ENQUIRY,
    )
    assert len(record.to_row()[COLUMNS.index("summary")]) <= MAX_SUMMARY_CHARS


def test_newlines_are_flattened_so_one_call_is_one_row():
    record = build_record(
        direction="inbound",
        caller_number="+1",
        duration_seconds=1,
        summary="line one\nline two\n\nline three",
        outcome=OUTCOME_ENQUIRY,
    )
    assert "\n" not in record.summary
