"""Outbound calls: direction detection and the greeting that follows from it."""

from __future__ import annotations

import json

import pytest

from voice_agent.agents.receptionist import (
    INBOUND_OPENING,
    OUTBOUND_OPENING,
    ReceptionistAgent,
    build_prompt_variables,
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
    assert inbound["opening_instructions"] == INBOUND_OPENING
    assert outbound["opening_instructions"] == OUTBOUND_OPENING
    assert inbound != outbound


def test_outbound_agent_does_not_say_thanks_for_calling():
    """It placed the call, so it must not greet as if it were answering one."""
    agent = ReceptionistAgent(outbound=True)
    assert "You placed this call" in agent.instructions
    assert "which business they have reached" not in agent.instructions
    assert agent.outbound is True


def test_inbound_agent_greets_as_receptionist():
    agent = ReceptionistAgent(outbound=False)
    assert "which business they have reached" in agent.instructions
    assert "You placed this call" not in agent.instructions
    assert agent.outbound is False


def test_outbound_agent_offers_to_call_back():
    agent = ReceptionistAgent(outbound=True)
    assert "bad time" in agent.instructions
    assert "Do not\npush." in agent.instructions or "Do not push" in agent.instructions
