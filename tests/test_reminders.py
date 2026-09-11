"""The reminder pass: what it picks up, and what it refuses to do twice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fake_sheets import FakeSheetsClient
from voice_agent.config import Settings
from voice_agent.reminders import run_once
from voice_agent.reminders.channels import (
    DryRunChannel,
    WhatsAppCallChannel,
    build_channel,
)
from voice_agent.reminders.runner import ReminderRun
from voice_agent.storage.appointments import COLUMNS, Appointment, AppointmentStore

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


class RecordingChannel:
    name = "recording"

    def __init__(self, *, works: bool = True) -> None:
        self.works = works
        self.sent: list[str] = []

    async def send(self, appointment: Appointment) -> bool:
        self.sent.append(appointment.booking_ref)
        return self.works


class ExplodingChannel:
    name = "exploding"

    async def send(self, appointment: Appointment) -> bool:
        raise RuntimeError("the carrier fell over")


def sheet_with(*appointments: Appointment) -> FakeSheetsClient:
    return FakeSheetsClient([list(COLUMNS), *(item.to_row() for item in appointments)])


IST = ZoneInfo("Asia/Kolkata")


def due_at(minutes_ahead: int, **kwargs) -> Appointment:
    """An appointment `minutes_ahead` from NOW, stored in UTC."""
    slot = NOW + timedelta(minutes=minutes_ahead)
    return Appointment(
        booking_ref=kwargs.pop("booking_ref", "1234"),
        status=kwargs.pop("status", "booked"),
        patient_name="Asha",
        patient_number=kwargs.pop("patient_number", "+919876543210"),
        department=kwargs.pop("department", "dentistry"),
        slot_utc=slot.isoformat(timespec="minutes"),
        **kwargs,
    )


@pytest.fixture
def wired(monkeypatch):
    """run_once against an in-memory sheet."""

    def build(sheet: FakeSheetsClient):
        store = AppointmentStore(sheet, tab="appointments")
        monkeypatch.setattr("voice_agent.reminders.runner.build_store", lambda _: store)
        return store

    return build


async def test_a_due_reminder_is_sent_and_recorded(wired):
    sheet = sheet_with(due_at(30))
    wired(sheet)
    channel = RecordingChannel()

    result = await run_once(Settings.load(), now=NOW, channel=channel)

    assert result.due == 1
    assert result.sent == ["1234"]
    assert channel.sent == ["1234"]
    row = dict(zip(COLUMNS, sheet.rows[1], strict=False))
    assert row["reminder_status"] == "sent"
    assert row["reminder_attempts"] == "1"


async def test_a_second_pass_does_not_call_again(wired):
    """The whole point of claiming: a double trigger must not ring twice."""
    sheet = sheet_with(due_at(30))
    wired(sheet)
    channel = RecordingChannel()

    await run_once(Settings.load(), now=NOW, channel=channel)
    second = await run_once(Settings.load(), now=NOW, channel=channel)

    assert channel.sent == ["1234"]
    assert second.due == 0


async def test_an_appointment_outside_the_window_is_left_alone(wired):
    wired(sheet_with(due_at(120)))
    channel = RecordingChannel()

    result = await run_once(Settings.load(), now=NOW, channel=channel)

    assert result.due == 0
    assert channel.sent == []


async def test_a_cancelled_appointment_is_never_reminded(wired):
    wired(sheet_with(due_at(30, status="cancelled")))
    channel = RecordingChannel()

    result = await run_once(Settings.load(), now=NOW, channel=channel)

    assert result.due == 0
    assert channel.sent == []


async def test_a_failed_send_is_recorded_and_retried_next_pass(wired):
    sheet = sheet_with(due_at(30))
    wired(sheet)
    failing = RecordingChannel(works=False)

    first = await run_once(Settings.load(), now=NOW, channel=failing)
    assert first.failed == ["1234"]
    assert dict(zip(COLUMNS, sheet.rows[1], strict=False))["reminder_status"] == "failed"

    working = RecordingChannel()
    await run_once(Settings.load(), now=NOW, channel=working)

    assert working.sent == ["1234"]
    assert dict(zip(COLUMNS, sheet.rows[1], strict=False))["reminder_status"] == "sent"


async def test_retries_stop_at_the_attempt_limit(wired):
    sheet = sheet_with(due_at(30))
    wired(sheet)
    failing = RecordingChannel(works=False)

    for _ in range(4):
        await run_once(Settings.load(), now=NOW, channel=failing)

    # REMINDER_MAX_ATTEMPTS defaults to 2, so a broken number is not dialled forever.
    assert failing.sent == ["1234", "1234"]


async def test_a_channel_that_raises_is_treated_as_a_failure_not_a_crash(wired):
    sheet = sheet_with(due_at(30))
    wired(sheet)

    result = await run_once(Settings.load(), now=NOW, channel=ExplodingChannel())

    assert result.failed == ["1234"]
    assert dict(zip(COLUMNS, sheet.rows[1], strict=False))["reminder_status"] == "failed"


async def test_a_claim_stranded_by_a_crash_is_picked_up_again(wired):
    """A process killed between claiming and dialling must not lose the reminder."""
    stranded = due_at(
        30,
        reminder_status="sending",
        reminder_attempts=1,
        reminder_last_utc=(NOW - timedelta(minutes=30)).isoformat(),
    )
    wired(sheet_with(stranded))
    channel = RecordingChannel()

    result = await run_once(Settings.load(), now=NOW, channel=channel)

    assert result.sent == ["1234"]
    assert channel.sent == ["1234"]


async def test_a_fresh_claim_is_left_to_the_pass_that_made_it(wired):
    fresh = due_at(
        30,
        reminder_status="sending",
        reminder_attempts=1,
        reminder_last_utc=NOW.isoformat(),
    )
    wired(sheet_with(fresh))
    channel = RecordingChannel()

    result = await run_once(Settings.load(), now=NOW, channel=channel)

    assert result.due == 0
    assert channel.sent == []


async def test_a_missing_store_reports_an_error_rather_than_pretending(monkeypatch):
    monkeypatch.setattr("voice_agent.reminders.runner.build_store", lambda _: None)

    result = await run_once(Settings.load(), now=NOW)

    assert result.error
    assert result.sent == []


async def test_an_unreadable_sheet_does_not_raise(monkeypatch, wired):
    sheet = sheet_with(due_at(30))
    store = wired(sheet)

    async def boom():
        raise RuntimeError("sheets is down")

    monkeypatch.setattr(store, "all", boom)

    result = await run_once(Settings.load(), now=NOW, channel=RecordingChannel())

    assert "could not read" in result.error


async def test_the_dry_run_channel_actually_runs():
    """It is the default, so a broken one breaks every reminder silently."""
    channel = DryRunChannel(Settings.load())
    assert await channel.send(due_at(30)) is True


async def test_a_full_pass_works_through_the_real_default_channel(wired):
    """End to end on the channel that actually ships, not a stub."""
    sheet = sheet_with(due_at(30))
    wired(sheet)

    result = await run_once(Settings.load(), now=NOW)

    assert result.channel == "dry_run"
    assert result.sent == ["1234"]
    assert dict(zip(COLUMNS, sheet.rows[1], strict=False))["reminder_status"] == "sent"


def test_dry_run_is_the_default_channel():
    """A live channel needs a WhatsApp business number Meta will allow to dial."""
    assert isinstance(build_channel(Settings.load()), DryRunChannel)


def test_the_whatsapp_channel_is_selectable(monkeypatch):
    monkeypatch.setenv("REMINDER_CHANNEL", "whatsapp_call")
    assert isinstance(build_channel(Settings.load()), WhatsAppCallChannel)


def test_an_unknown_channel_fails_loudly(monkeypatch):
    monkeypatch.setenv("REMINDER_CHANNEL", "carrier-pigeon")
    with pytest.raises(ValueError, match="REMINDER_CHANNEL"):
        build_channel(Settings.load())


async def test_the_whatsapp_channel_refuses_a_booking_with_no_number(monkeypatch):
    """Nothing to dial. It must report failure, not raise mid-pass."""
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    channel = WhatsAppCallChannel(Settings.load())

    assert await channel.send(due_at(30, patient_number="")) is False


async def test_the_whatsapp_channel_refuses_without_meta_credentials(monkeypatch):
    for key in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    channel = WhatsAppCallChannel(Settings.load())

    assert await channel.send(due_at(30)) is False


def test_the_run_summary_is_json_serialisable():
    """The endpoint returns it, so it has to survive json.dumps."""
    import json

    json.dumps(ReminderRun(scanned=1, due=1, sent=["1234"]).as_dict())


def test_reminders_need_no_timezone_at_all():
    """Slots are stored in UTC, so the scan is timezone free by construction."""
    import inspect

    from voice_agent.storage.appointments import due_for_reminder

    assert "tz" not in inspect.signature(due_for_reminder).parameters
