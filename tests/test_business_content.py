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
    assert "What does a visit cost?" in rendered
    assert "Can I cancel or change my appointment?" in rendered
    # Facts come from the profile, not from prose in the prompt.
    assert load_profile()["business_name"] in rendered
    assert load_profile()["address"] in rendered


def test_every_department_reaches_the_prompt(rendered):
    """The model cannot route to a department it was never told about."""
    for name in load_profile().schedule.names:
        assert name in rendered, name


def test_every_department_keeps_real_office_hours(rendered):
    """Round-the-clock outpatient clinics are not a thing in US healthcare.

    Only the emergency room never closes, and it is not a bookable department.
    """
    for name, department in load_profile().schedule.departments.items():
        assert not department.is_always_open, f"{name} is still open 24/7"
        assert department.slot_minutes == 30, name
        assert not department.weekly.get("sunday"), f"{name} opens on a Sunday"
        for windows in department.weekly.values():
            for opens, closes in windows:
                assert opens.hour >= 7, (name, opens)
                assert closes.hour <= 19, (name, closes)


def test_the_hospital_runs_on_ohio_time():
    """Columbus is Eastern. There is no Ohio-specific IANA zone."""
    assert load_profile().schedule.timezone == "America/New_York"


def test_the_emergency_room_is_still_always_open(rendered):
    """The one thing that genuinely is 24/7, and the agent must not lose it."""
    assert "twenty four hours a day" in load_profile()["emergency_room"]
    assert "twenty four hours" in rendered


def test_the_agent_is_told_not_to_recite_every_departments_hours(rendered):
    """Ten sets of opening hours read aloud is unusable on a phone call."""
    flat = " ".join(rendered.split())
    assert "Never read that whole list out" in flat
    assert "ask which department they need" in flat


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
