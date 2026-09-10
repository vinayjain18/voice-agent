from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fake_sheets import FakeSheetsClient
from voice_agent.storage.appointments import (
    COLUMNS,
    REMINDER_FAILED,
    REMINDER_PENDING,
    REMINDER_SENDING,
    REMINDER_SENT,
    REMINDER_SKIPPED,
    STATUS_CANCELLED,
    Appointment,
    AppointmentError,
    AppointmentStore,
    SlotTaken,
    due_for_reminder,
    normalise_number,
)
from voice_agent.storage.sheets import SheetsError

IST = ZoneInfo("Asia/Kolkata")


def store(rows: list[list[str]] | None = None) -> tuple[AppointmentStore, FakeSheetsClient]:
    client = FakeSheetsClient(rows if rows is not None else [list(COLUMNS)])
    return AppointmentStore(client, tab="appointments"), client


def row_for(
    ref: str,
    *,
    status: str = "booked",
    number: str = "+919876543210",
    day: str = "2026-09-14",
    at: str = "16:00",
    reminder: str = REMINDER_PENDING,
    attempts: int = 0,
    reminder_last: str = "",
) -> list[str]:
    return Appointment(
        booking_ref=ref,
        status=status,
        patient_name="Asha",
        patient_number=number,
        slot_date=day,
        slot_time=at,
        reminder_status=reminder,
        reminder_attempts=attempts,
        reminder_last_utc=reminder_last,
        created_utc="2026-09-10T09:00:00+00:00",
    ).to_row()


async def test_empty_tab_gets_a_header_and_no_rows_are_invented():
    appointments, client = store(rows=[])

    await appointments.ensure_ready()

    assert client.rows == [COLUMNS]
    assert await appointments.all() == []


async def test_existing_rows_survive_ensure_ready():
    existing = [list(COLUMNS), row_for("1234")]
    appointments, client = store(existing)

    await appointments.ensure_ready()

    assert len(client.rows) == 2
    assert client.rows[1][0] == "1234"


async def test_a_foreign_header_is_refused_rather_than_overwritten():
    appointments, client = store([["name", "when", "phone"], ["Asha", "x", "y"]])

    with pytest.raises(SheetsError, match="unexpected header"):
        await appointments.all()

    assert client.rows[1] == ["Asha", "x", "y"]


async def test_create_inserts_below_the_header_and_returns_a_reference():
    appointments, client = store()

    made = await appointments.create(
        patient_name="Asha",
        patient_number="+919876543210",
        slot=datetime(2026, 9, 14, 16, 0, tzinfo=IST),
        reason="fever",
        raw_request="Monday evening",
    )

    assert made.booking_ref.isdigit() and len(made.booking_ref) == 4
    assert client.rows[0] == COLUMNS
    assert client.rows[1][0] == made.booking_ref
    assert made.slot_date == "2026-09-14" and made.slot_time == "16:00"
    assert made.reminder_status == REMINDER_PENDING


async def test_create_refuses_a_slot_that_is_already_booked():
    appointments, _ = store([list(COLUMNS), row_for("1234", day="2026-09-14", at="16:00")])

    with pytest.raises(SlotTaken):
        await appointments.create(
            patient_name="Ravi",
            patient_number="+919000000000",
            slot=datetime(2026, 9, 14, 16, 0, tzinfo=IST),
        )


async def test_a_cancelled_booking_frees_its_slot():
    appointments, _ = store(
        [list(COLUMNS), row_for("1234", status=STATUS_CANCELLED, day="2026-09-14", at="16:00")]
    )

    made = await appointments.create(
        patient_name="Ravi",
        patient_number="+919000000000",
        slot=datetime(2026, 9, 14, 16, 0, tzinfo=IST),
    )

    assert made.slot_time == "16:00"


async def test_a_simultaneous_booking_for_one_slot_leaves_a_single_winner():
    """Sheets has no transactions, so both writes land. The later one stands down."""

    class RacingClient(FakeSheetsClient):
        def __init__(self, rows):
            super().__init__(rows)
            self.raced = False

        async def insert_row(self, tab, values, *, at=1):
            await super().insert_row(tab, values, at=at)
            if not self.raced:
                self.raced = True
                # A rival booked the same slot a moment earlier.
                rival = row_for("9999", day="2026-09-14", at="16:00")
                rival[COLUMNS.index("created_utc")] = "2026-09-10T08:00:00+00:00"
                await super().insert_row(tab, rival, at=1)

    client = RacingClient([list(COLUMNS)])
    appointments = AppointmentStore(client, tab="appointments")

    with pytest.raises(SlotTaken, match="taken during booking"):
        await appointments.create(
            patient_name="Ravi",
            patient_number="+919000000000",
            slot=datetime(2026, 9, 14, 16, 0, tzinfo=IST),
        )

    live = [item for item in await appointments.all() if item.is_active]
    assert [item.booking_ref for item in live] == ["9999"]


async def test_cancel_marks_the_row_and_stops_its_reminder():
    appointments, client = store([list(COLUMNS), row_for("1234")])

    cancelled = await appointments.cancel("1234", reason="patient called")

    assert cancelled is not None
    assert cancelled.status == STATUS_CANCELLED
    assert cancelled.reminder_status == REMINDER_SKIPPED
    assert cancelled.cancelled_utc
    # The row is amended, not removed.
    assert len(client.rows) == 2


async def test_cancelling_an_unknown_reference_is_not_an_error():
    appointments, _ = store([list(COLUMNS), row_for("1234")])
    assert await appointments.cancel("0000") is None


async def test_find_by_number_ignores_formatting_differences():
    appointments, _ = store(
        [list(COLUMNS), row_for("1234", number="+919876543210", day="2026-09-14")]
    )

    for spelling in ("9876543210", "+91 98765 43210", "919876543210"):
        found = await appointments.find_by_number(spelling, tz=IST)
        assert [item.booking_ref for item in found] == ["1234"], spelling


async def test_find_by_number_skips_cancelled_and_past_appointments():
    appointments, _ = store(
        [
            list(COLUMNS),
            row_for("1111", day="2020-01-01"),
            row_for("2222", status=STATUS_CANCELLED, day="2099-01-01"),
            row_for("3333", day="2099-01-01"),
        ]
    )

    found = await appointments.find_by_number("+919876543210", tz=IST)

    assert [item.booking_ref for item in found] == ["3333"]


async def test_claim_reminder_counts_attempts_and_stops_at_the_limit():
    appointments, _ = store([list(COLUMNS), row_for("1234")])

    first = await appointments.claim_reminder("1234", max_attempts=2)
    assert first is not None
    assert first.reminder_status == REMINDER_SENDING
    assert first.reminder_attempts == 1

    second = await appointments.claim_reminder("1234", max_attempts=2)
    assert second is not None and second.reminder_attempts == 2

    assert await appointments.claim_reminder("1234", max_attempts=2) is None


async def test_finish_reminder_records_the_outcome():
    appointments, _ = store([list(COLUMNS), row_for("1234")])

    await appointments.claim_reminder("1234", max_attempts=2)
    done = await appointments.finish_reminder("1234", sent=True)
    assert done is not None and done.reminder_status == REMINDER_SENT

    failed = await appointments.finish_reminder("1234", sent=False)
    assert failed is not None and failed.reminder_status == REMINDER_FAILED


async def test_new_bookings_are_appended_so_existing_rows_never_move():
    """The clobber guard.

    Amendments write to a row index read a moment earlier. If new rows went in at
    the top, every index below would shift and that write would land on somebody
    else's appointment and destroy it. Appending keeps indices stable.
    """
    appointments, client = store([list(COLUMNS), row_for("1111", day="2026-09-14", at="16:00")])
    before = client.rows[1]

    await appointments.create(
        patient_name="Ravi",
        patient_number="+919000000000",
        slot=datetime(2026, 9, 15, 10, 0, tzinfo=IST),
    )

    assert client.rows[1] == before, "an existing row moved"
    assert client.rows[-1][COLUMNS.index("patient_name")] == "Ravi"


async def test_a_concurrent_booking_does_not_corrupt_an_amendment():
    """A booking landing mid-cancel must not cost anyone their appointment."""

    class BusyClient(FakeSheetsClient):
        def __init__(self, rows):
            super().__init__(rows)
            self.interfered = False

        async def read_rows(self, tab):
            rows = await super().read_rows(tab)
            if not self.interfered:
                self.interfered = True
                # Someone else books while we are mid-amendment.
                self.rows.append(row_for("2222", day="2026-09-20", at="11:00"))
            return rows

    client = BusyClient([list(COLUMNS), row_for("1111", day="2026-09-14", at="16:00")])
    appointments = AppointmentStore(client, tab="appointments")

    await appointments.cancel("1111")

    live = {item.booking_ref: item.status for item in await appointments.all()}
    assert live["1111"] == STATUS_CANCELLED
    assert live["2222"] == "booked", "the concurrent booking was destroyed"


async def test_a_duplicated_reference_is_refused_rather_than_guessed_at():
    """Two rows with one reference means a human edited the sheet."""
    appointments, _ = store([list(COLUMNS), row_for("1111"), row_for("1111", at="17:00")])

    with pytest.raises(AppointmentError, match="appears on 2 rows"):
        await appointments.cancel("1111")


async def test_amend_retries_when_a_new_booking_shifts_the_row():
    class ShiftingClient(FakeSheetsClient):
        def __init__(self, rows):
            super().__init__(rows)
            self.shifted = False

        async def write_row(self, tab, row_index, values):
            await super().write_row(tab, row_index, values)
            if not self.shifted:
                self.shifted = True
                self.rows.insert(1, row_for("5555", day="2026-10-01"))

    client = ShiftingClient([list(COLUMNS), row_for("1234")])
    appointments = AppointmentStore(client, tab="appointments")

    cancelled = await appointments.cancel("1234")

    assert cancelled is not None and cancelled.status == STATUS_CANCELLED
    stored = await appointments.find_by_ref("1234")
    assert stored is not None and stored.status == STATUS_CANCELLED


async def test_rows_with_missing_trailing_cells_still_parse():
    appointments, _ = store([list(COLUMNS), ["1234", "booked", "Asha", "+919876543210", "2026-09-14", "16:00"]])

    found = await appointments.find_by_ref("1234")

    assert found is not None
    assert found.reason == ""
    assert found.reminder_attempts == 0


def test_normalise_number_keeps_the_last_ten_digits():
    assert normalise_number("+91 98765-43210") == "9876543210"
    assert normalise_number("09876543210") == "9876543210"
    assert normalise_number("") == ""
    assert normalise_number("123") == "123"


def make(ref: str, *, minutes_ahead: int, now: datetime, **kwargs) -> Appointment:
    slot = now.astimezone(IST) + timedelta(minutes=minutes_ahead)
    return Appointment(
        booking_ref=ref,
        status=kwargs.pop("status", "booked"),
        patient_name="Asha",
        patient_number="+919876543210",
        slot_date=slot.strftime("%Y-%m-%d"),
        slot_time=slot.strftime("%H:%M"),
        **kwargs,
    )


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
WINDOW = {
    "lead_minutes": 30,
    "tolerance_minutes": 5,
    "max_attempts": 2,
    "claim_timeout_minutes": 10,
}


def due(items: list[Appointment]) -> list[str]:
    return [item.booking_ref for item in due_for_reminder(items, now=NOW, tz=IST, **WINDOW)]


def test_due_selects_only_appointments_inside_the_lead_window():
    items = [
        make("1111", minutes_ahead=30, now=NOW),
        make("2222", minutes_ahead=90, now=NOW),
        make("3333", minutes_ahead=5, now=NOW),
    ]
    assert due(items) == ["1111"]


def test_due_tolerates_the_scan_interval():
    items = [
        make("1111", minutes_ahead=26, now=NOW),
        make("2222", minutes_ahead=34, now=NOW),
        make("3333", minutes_ahead=20, now=NOW),
    ]
    assert sorted(due(items)) == ["1111", "2222"]


def test_due_skips_cancelled_and_already_sent():
    items = [
        make("1111", minutes_ahead=30, now=NOW, status=STATUS_CANCELLED),
        make("2222", minutes_ahead=30, now=NOW, reminder_status=REMINDER_SENT),
        make("3333", minutes_ahead=30, now=NOW),
    ]
    assert due(items) == ["3333"]


def test_due_retries_a_failed_reminder_until_the_attempt_limit():
    retryable = make("1111", minutes_ahead=30, now=NOW, reminder_status=REMINDER_FAILED, reminder_attempts=1)
    exhausted = make("2222", minutes_ahead=30, now=NOW, reminder_status=REMINDER_FAILED, reminder_attempts=2)

    assert due([retryable, exhausted]) == ["1111"]


def test_a_fresh_claim_is_left_alone_so_a_double_trigger_cannot_call_twice():
    just_claimed = make(
        "1111",
        minutes_ahead=30,
        now=NOW,
        reminder_status=REMINDER_SENDING,
        reminder_attempts=1,
        reminder_last_utc=NOW.isoformat(),
    )
    assert due([just_claimed]) == []


def test_a_stale_claim_is_picked_up_again_after_a_crash():
    stranded = make(
        "1111",
        minutes_ahead=30,
        now=NOW,
        reminder_status=REMINDER_SENDING,
        reminder_attempts=1,
        reminder_last_utc=(NOW - timedelta(minutes=30)).isoformat(),
    )
    assert due([stranded]) == ["1111"]


def test_a_claim_with_an_unreadable_timestamp_is_reclaimed():
    broken = make(
        "1111",
        minutes_ahead=30,
        now=NOW,
        reminder_status=REMINDER_SENDING,
        reminder_attempts=1,
        reminder_last_utc="not a date",
    )
    assert due([broken]) == ["1111"]


def test_an_unreadable_slot_is_skipped_rather_than_crashing_the_scan():
    broken = Appointment(
        booking_ref="1111",
        status="booked",
        patient_name="Asha",
        patient_number="+919876543210",
        slot_date="14/09/2026",
        slot_time="4pm",
    )
    assert due([broken]) == []
