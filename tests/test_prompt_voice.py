"""The prompt has to sound like a person, and its examples have to obey its own rules.

The worked dialogues are few-shot: the model copies their register far more
faithfully than it follows a bullet point. So an example that breaks a rule is
worse than no example, and these tests police that rather than trusting review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from voice_agent.agents.receptionist import ReceptionistAgent, build_prompt_variables
from voice_agent.business import load_profile
from voice_agent.config import Settings

PROMPT = Path(__file__).resolve().parents[1] / "src/voice_agent/prompts/receptionist.md"
BUSINESS = Path(__file__).resolve().parents[1] / "src/voice_agent/business"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


@pytest.fixture
def rendered() -> str:
    from voice_agent.prompts import render_prompt

    return render_prompt(
        "receptionist", build_prompt_variables(load_profile(), Settings.load())
    )


@pytest.fixture
def flat(rendered) -> str:
    """The prompt with line wrapping collapsed, for phrase assertions."""
    return " ".join(rendered.split())


def _agent_lines(text: str) -> list[str]:
    """Every spoken example reply, including its wrapped continuation lines."""
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("You: "):
            continue
        block = [line.removeprefix("You: ")]
        for follow in lines[i + 1 :]:
            if not follow.strip() or follow.startswith(("You:", "Caller:", "[")):
                break
            block.append(follow.strip())
        out.append(" ".join(block))
    return out


def test_there_are_worked_examples_to_learn_from(rendered):
    assert len(_agent_lines(rendered)) >= 18


def test_prompt_demands_contractions(rendered):
    assert "Always use contractions" in rendered


def test_examples_actually_use_contractions(rendered):
    """A rule the examples contradict is a rule the model ignores."""
    spoken = " ".join(_agent_lines(rendered))
    assert len(re.findall(r"\b\w+'(s|t|re|ll|d|m|ve)\b", spoken)) >= 15


def test_examples_never_use_the_stiff_long_forms(rendered):
    """"I am not able to" is the single clearest tell of a bot on a phone."""
    stiff = ("I am ", "do not ", "does not ", "cannot ", "will not ", "I will ")
    offenders = [
        line for line in _agent_lines(rendered) if any(s in line for s in stiff)
    ]
    assert not offenders, offenders


def test_prompt_bans_call_centre_phrases(rendered):
    for phrase in (
        "How may I assist you today",
        "I'd be happy to help you with that",
        "I understand your concern",
        "Great question",
        "As an AI",
    ):
        assert phrase in rendered, phrase


def test_examples_do_not_use_the_banned_phrases(rendered):
    """Self-consistency: the demonstrations must not do what they forbid."""
    spoken = " ".join(_agent_lines(rendered)).lower()
    for phrase in (
        "how may i assist",
        "happy to help you with that",
        "thank you for your patience",
        "i understand your concern",
        "great question",
        "anything else i can assist you with",
        "please note that",
    ):
        assert phrase not in spoken, phrase


def test_examples_never_ask_for_contact_details(rendered):
    spoken = " ".join(_agent_lines(rendered)).lower()
    for phrase in ("your number", "phone number", "email address", "reach you on"):
        assert phrase not in spoken, phrase


def test_examples_never_quote_a_price(rendered):
    """No digits in a spoken example except clock times, which are words anyway."""
    for line in _agent_lines(rendered):
        assert not re.search(r"[₹$]\s*\d|\d+\s*(lakh|crore|k\b)", line.lower()), line


def test_examples_do_not_hardcode_profile_facts(rendered):
    """Facts belong in profile.json; an example that repeats one goes stale silently."""
    spoken = " ".join(_agent_lines(rendered)).lower()
    assert "websinova dot com" not in spoken
    assert "vinay" not in spoken


def test_prompt_teaches_leading_the_conversation(rendered):
    assert "Leading the conversation" in rendered
    assert "Offer, do not interrogate" in rendered
    assert "Never ask two questions in one breath" in rendered


def test_prompt_covers_the_calls_a_hospital_line_gets(flat):
    """A hospital line gets more than patients booking in."""
    for topic in (
        "Someone selling to us",
        "Someone asking for a job",
        "Asking for test results over the phone",
        "Asking for a specific provider by name",
        "Asking about a bill or what insurance covers",
        "Someone in a hurry",
    ):
        assert topic in flat, topic


def test_results_are_never_read_out_at_the_desk(flat):
    assert "no visibility into results" in flat
    assert "don't say whether a result is normal" in flat


def test_a_providers_whereabouts_are_never_disclosed(flat):
    assert "Don't say whether they're in, busy, or with a patient" in flat


def test_coverage_is_never_promised(flat):
    """Telling someone their insurance covers it, and being wrong, costs money."""
    assert "Don't quote coverage or promise what will be paid" in flat


def test_prompt_handles_the_awkward_times(rendered):
    """A day is not a time, and half the answers people give are not either."""
    for case in (
        '**"Today"**',
        '**"As soon as possible"**',
        "A date that has already passed",
        '**"Sometime next week"**',
        "They change their mind",
    ):
        assert case in rendered, case


def test_prompt_asks_for_the_day_with_options_not_an_empty_field(rendered):
    assert "Getting the day and time" in rendered
    assert "later this week" in rendered
    assert "Morning or afternoon?" in rendered


def test_prompt_varies_the_closing_question(rendered):
    """The same closing line on every call is the most robotic thing available."""
    assert "Vary how you ask it" in rendered
    assert "Anything else before I let you go?" in rendered


def test_prompt_demands_a_spoken_sign_off(rendered):
    """The last thing a caller hears must not be a hangup."""
    assert "Signing off" in rendered
    assert "Calling end_call is not the end of the conversation" in rendered
    assert "Thanks for calling, have a good day." in rendered


def test_the_goodbye_after_end_call_is_enforced_by_the_tool(rendered):
    """The sign-off has to come after the tool call, not instead of it.

    This used to be taught by a "[call end_call]" line in a worked example. That
    notation leaked: on a real call the model spoke
    'Which works for you?[call book_appointment {"date":"2026-09-12",...}]'
    out loud, having learned the syntax from the examples. The examples are now
    marker free, so the ordering is pinned where it is actually enforced: the
    tool's own return text, plus the rule in the prompt.
    """
    from voice_agent.agents.receptionist import INBOUND_GOODBYE, OUTBOUND_GOODBYE

    for text in (INBOUND_GOODBYE, OUTBOUND_GOODBYE):
        assert "ONE short sentence" in text
        assert "thank" in text.lower()

    flat = " ".join(rendered.split())
    assert "Calling end_call is not the end of the conversation" in flat
    assert "one last turn" in flat


def test_no_worked_example_writes_out_a_tool_call(rendered):
    """The notation the model copied into speech must not come back."""
    assert "[call " not in rendered
    flat = " ".join(rendered.split())
    assert "Never write a tool name, a function call or its arguments" in flat


def test_goodbye_instructions_are_direction_aware():
    """Thanking someone for calling when we rang them is an obvious tell."""
    from voice_agent.agents.receptionist import INBOUND_GOODBYE, OUTBOUND_GOODBYE

    # Collapse the line wrapping: where the text happens to break is not the point.
    inbound = " ".join(INBOUND_GOODBYE.split())
    outbound = " ".join(OUTBOUND_GOODBYE.split())

    assert "Thank them for calling" in inbound
    assert "Never thank them for calling" in outbound
    assert "their time" in outbound
    # One sentence, so it cannot outrun the hangup grace period.
    for text in (inbound, outbound):
        assert "ONE short sentence" in text
        assert "do not ask" in text.lower()


def test_the_end_call_tool_documents_waiting_for_the_answer():
    """The condition sits in the tool schema, where the decision is made."""
    agent_tools = ReceptionistAgent()._tools
    end_call = next(
        tool
        for tool in agent_tools
        if getattr(getattr(tool, "info", None), "name", None) == "end_call"
    )
    description = " ".join(end_call.info.description.split())
    assert "heard them answer" in description
    assert "same reply is wrong" in description
    assert "Do not call this straight after saving or cancelling" in description


def test_prompt_requires_the_callers_own_wording_for_the_time(rendered):
    assert "Say it back in their words, not yours" in rendered
    assert "do not name a weekday when they said" in rendered.replace(
        "and do not name a weekday when they said", "do not name a weekday when they said"
    )


def test_confirmation_is_one_short_line(rendered):
    assert "One short line. The day, the time, and nothing else." in rendered


def _spoken_values(path: Path) -> list[str]:
    """Every string a caller could hear, ignoring editorial keys."""
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not str(key).startswith("_"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            out.append(node)

    walk(json.loads(path.read_text(encoding="utf-8")))
    return out


@pytest.mark.parametrize("filename", ["profile.json", "faq.json"])
def test_spoken_content_is_not_written_stiffly(filename):
    """profile.json and faq.json are quoted almost verbatim on the call."""
    text = " ".join(_spoken_values(BUSINESS / filename))
    stiff = [
        w
        for w in ("do not", "does not", "we are", "I am", "we will", "cannot")
        if re.search(rf"\b{w}\b", text)
    ]
    assert not stiff, f"{filename}: {stiff}"


def test_prompt_forbids_stacking_questions(flat):
    """A real call produced several questions in one breath."""
    assert "One reply is one thought" in flat
    assert "If they have not said anything, say nothing" in flat
    # The actual failure is kept as a counter-example so it stays recognisable.
    assert "three questions in one breath" in flat
    assert "No further response." in flat


def test_prompt_limits_name_repetition(flat):
    assert "name in two replies in a row" in flat


def test_prompt_requires_confirming_the_booking_before_closing(rendered):
    assert "Never save a booking and hang up in the same breath" in rendered


