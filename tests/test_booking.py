"""Booking flow: date injection and the incomplete-lead guard."""

from __future__ import annotations

import csv

import pytest

from voice_agent.agents.receptionist import (
    ReceptionistAgent,
    _missing_contact_fields,
    build_prompt_variables,
)
from voice_agent.business import load_profile
from voice_agent.config import Settings


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")


def test_prompt_carries_todays_date():
    """Without this the model invents a date for 'next Tuesday'."""
    variables = build_prompt_variables(load_profile(), Settings.load())
    assert variables["current_date"].count("-") == 2
    assert variables["current_datetime"]


def test_missing_fields_detected():
    assert _missing_contact_fields("", "") == "name and phone number or email"
    assert _missing_contact_fields("Raj", "") == "phone number or email"
    assert _missing_contact_fields("", "a@b.com") == "name"
    assert _missing_contact_fields("Raj", "a@b.com") == ""


async def test_booking_saves_a_row(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = ReceptionistAgent(settings=Settings.load())
    result = await agent.book_callback(
        None, name="Raj", phone_or_email="raj@x.com",
        preferred_date="2026-08-25", preferred_time="15:00",
        reason="voice agent", raw_request="next Tuesday at 3",
    )
    assert "Booked" in result
    rows = list(csv.DictReader((tmp_path / "leads.csv").open()))
    assert rows[0]["kind"] == "booking"
    assert rows[0]["preferred_date"] == "2026-08-25"
    assert rows[0]["preferred_time"] == "15:00"
    assert rows[0]["raw_request"] == "next Tuesday at 3"


async def test_booking_refuses_without_contact(tmp_path, monkeypatch):
    """The junk row seen in real testing: tool fired before details collected."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = ReceptionistAgent(settings=Settings.load())
    result = await agent.book_callback(
        None, name="", phone_or_email="",
        preferred_date="2026-08-25", preferred_time="15:00", reason="x",
    )
    assert "Do not save yet" in result
    assert not (tmp_path / "leads.csv").exists()


async def test_message_refuses_without_contact(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    agent = ReceptionistAgent(settings=Settings.load())
    result = await agent.take_callback_details(None, name="Raj", phone_or_email="", reason="x")
    assert "Do not save yet" in result
    assert not (tmp_path / "leads.csv").exists()
