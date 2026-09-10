"""The knowledge base has to stay speakable and complete."""

from __future__ import annotations

import pytest

from voice_agent.agents.receptionist import build_prompt_variables
from voice_agent.business import load_profile
from voice_agent.config import Settings
from voice_agent.prompts import render_prompt


@pytest.fixture
def rendered(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    profile = load_profile()
    return render_prompt(
        "receptionist", build_prompt_variables(profile, Settings.load())
    )


def test_faqs_load():
    profile = load_profile()
    assert len(profile.faqs) >= 10
    for item in profile.faqs:
        assert item["q"] and item["a"]


def test_prompt_includes_the_faqs_and_leaves_no_placeholders(rendered):
    assert "{" not in rendered
    assert "How much is the consultation?" in rendered
    assert "Can I cancel or change my appointment?" in rendered
    # Facts come from the profile, not from prose in the prompt.
    assert load_profile()["doctor_name"] in rendered
    assert load_profile()["address"] in rendered


def test_content_has_no_dashes_or_markup(rendered):
    """Brand rule: no em or en dashes. They also read badly aloud."""
    assert "—" not in rendered
    assert "–" not in rendered


def test_editorial_comment_keys_are_not_sent_to_the_model():
    profile = load_profile()
    assert "_comment" not in profile.as_prompt_vars()


def test_agent_is_told_never_to_give_medical_advice(rendered):
    """A clinic may quote a consultation fee. It may never advise on treatment."""
    flat = " ".join(rendered.split())
    assert "You are reception, not a clinician" in flat
    assert "Never diagnose" in flat
