"""Outbound calls: direction detection and the greeting that follows from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_agent.agents.receptionist import (
    ReceptionistAgent,
    build_prompt_variables,
    opening_line,
)
from voice_agent.business import load_profile
from voice_agent.config import Settings
from voice_agent.main import _is_outbound


class _Ctx:
    class _Job:
        metadata = ""

    def __init__(self, metadata=""):
        self.job = self._Job()
        self.job.metadata = metadata


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


def test_outbound_detected_from_metadata():
    ctx = _Ctx(json.dumps({"direction": "outbound", "callee": "+919999999999"}))
    assert _is_outbound(ctx)


def test_inbound_when_no_metadata():
    assert not _is_outbound(_Ctx(""))


def test_inbound_when_direction_absent():
    assert not _is_outbound(_Ctx(json.dumps({"callee": "+91999"})))


def test_malformed_metadata_does_not_break_the_call():
    """A bad metadata string must never stop a call connecting."""
    assert not _is_outbound(_Ctx("not json at all"))
    assert not _is_outbound(_Ctx("[1,2,3]"))
    assert not _is_outbound(_Ctx(None))


def test_openings_differ():
    profile = load_profile()
    settings = Settings.load()
    inbound = build_prompt_variables(profile, settings, outbound=False)
    outbound = build_prompt_variables(profile, settings, outbound=True)
    assert inbound["opening_instructions"] != outbound["opening_instructions"]
    # Each carries the exact words already spoken, so the model cannot re-greet.
    assert opening_line(profile) in inbound["opening_instructions"]
    assert opening_line(profile, outbound=True) in outbound["opening_instructions"]


def test_greeting_is_fixed_text_from_the_profile():
    """The opening is spoken verbatim, not composed by the model."""
    profile = load_profile()
    assert opening_line(profile) == profile["greeting_inbound"]
    assert opening_line(profile, outbound=True) == profile["greeting_outbound"]
    # Answering a call, not placing one.
    assert "calling" in opening_line(profile).lower()
    assert profile["agent_name"] in opening_line(profile)
    assert profile["business_name"] in opening_line(profile)


def test_greeting_falls_back_when_the_profile_key_is_missing():
    """A missing key must never leave a connected caller in silence."""
    from voice_agent.business import BusinessProfile

    bare = BusinessProfile(
        {"business_name": "Acme", "agent_name": "Emma"}, []
    )
    line = opening_line(bare)
    assert "Acme" in line and "Emma" in line


def test_entrypoint_speaks_the_greeting_rather_than_generating_it():
    source = (
        Path(__file__).resolve().parents[1] / "src/voice_agent/main.py"
    ).read_text()
    assert "session.say(opening_line(" in source
    assert "generate_reply" not in source


def test_outbound_agent_does_not_say_thanks_for_calling():
    """It placed the call, so it must not greet as if it were answering one."""
    profile = load_profile()
    agent = ReceptionistAgent(outbound=True)
    assert "You placed this call" in agent.instructions
    assert opening_line(profile, outbound=True) in agent.instructions
    assert profile["greeting_inbound"] not in agent.instructions
    assert agent.outbound is True


def test_inbound_agent_greets_as_receptionist():
    profile = load_profile()
    agent = ReceptionistAgent(outbound=False)
    assert opening_line(profile) in agent.instructions
    assert "Do not greet again" in agent.instructions
    assert "You placed this call" not in agent.instructions
    assert agent.outbound is False


def test_outbound_agent_offers_to_call_back():
    agent = ReceptionistAgent(outbound=True)
    assert "bad time" in agent.instructions
    assert "Do not\npush." in agent.instructions or "Do not push" in agent.instructions
