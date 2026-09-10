from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from voice_agent.scheduling import (
    Schedule,
    ScheduleError,
    available_slots,
    load_schedule,
    next_available,
    render_hours,
    speak_slot,
    speak_time,
)

IST = ZoneInfo("Asia/Kolkata")

RAW = {
    "timezone": "Asia/Kolkata",
    "slot_minutes": 15,
    "booking_horizon_days": 30,
    "weekly": {
        "monday": [["10:00", "13:00"], ["17:00", "20:00"]],
        "tuesday": [["10:00", "13:00"], ["17:00", "20:00"]],
        "saturday": [["10:00", "13:00"]],
        "sunday": [],
    },
}


def test_load_schedule_parses_windows():
    schedule = load_schedule(RAW)
    assert schedule.slot_minutes == 15
    assert schedule.weekly["monday"] == [
        (time(10, 0), time(13, 0)),
        (time(17, 0), time(20, 0)),
    ]
    assert schedule.weekly["sunday"] == []


def test_load_schedule_rejects_a_backwards_window():
    with pytest.raises(ScheduleError, match="not before"):
        load_schedule({"weekly": {"monday": [["18:00", "10:00"]]}})


def test_load_schedule_rejects_an_unknown_day():
    with pytest.raises(ScheduleError, match="not a weekday"):
        load_schedule({"weekly": {"funday": [["10:00", "11:00"]]}})


def test_slots_cover_both_windows_and_stop_at_closing():
    schedule = load_schedule(RAW)
    monday = date(2026, 9, 14)
    slots = schedule.slots_for(monday)

    assert slots[0].hour == 10 and slots[0].minute == 0
    assert slots[-1].hour == 19 and slots[-1].minute == 45
    # 3 hours + 3 hours at 15 minutes each, and never a slot that runs past close.
    assert len(slots) == 24
    assert all(slot.tzinfo is not None for slot in slots)


def test_closed_days_have_no_slots():
    schedule = load_schedule(RAW)
    assert schedule.slots_for(date(2026, 9, 13)) == []
    assert not schedule.is_open_on(date(2026, 9, 13))


def test_available_slots_exclude_taken_and_past():
    schedule = load_schedule(RAW)
    monday = date(2026, 9, 14)
    now = datetime(2026, 9, 14, 11, 0, tzinfo=IST)
    taken = {datetime(2026, 9, 14, 11, 30, tzinfo=IST)}

    free = available_slots(schedule, monday, taken, now=now)

    assert datetime(2026, 9, 14, 10, 0, tzinfo=IST) not in free
    assert datetime(2026, 9, 14, 11, 30, tzinfo=IST) not in free
    assert datetime(2026, 9, 14, 11, 15, tzinfo=IST) in free


def test_min_notice_pushes_the_first_offer_out():
    schedule = load_schedule(RAW)
    now = datetime(2026, 9, 14, 11, 0, tzinfo=IST)

    free = available_slots(schedule, date(2026, 9, 14), set(), now=now, min_notice_minutes=60)

    assert free[0] == datetime(2026, 9, 14, 12, 15, tzinfo=IST)


def test_next_available_rolls_into_following_days():
    schedule = load_schedule(RAW)
    # Sunday: the clinic is shut, so the next offers must be Monday's.
    now = datetime(2026, 9, 13, 9, 0, tzinfo=IST)

    offers = next_available(schedule, set(), now=now, limit=2)

    assert [slot.date() for slot in offers] == [date(2026, 9, 14), date(2026, 9, 14)]
    assert offers[0].hour == 10


def test_next_available_returns_nothing_when_fully_booked():
    schedule = Schedule(slot_minutes=60, booking_horizon_days=1, weekly={"monday": [(time(10, 0), time(11, 0))]})
    now = datetime(2026, 9, 14, 8, 0, tzinfo=IST)
    taken = {datetime(2026, 9, 14, 10, 0, tzinfo=IST)}

    assert next_available(schedule, taken, now=now, limit=3) == []


def test_is_valid_slot_rejects_an_off_grid_time():
    schedule = load_schedule(RAW)
    assert schedule.is_valid_slot(datetime(2026, 9, 14, 10, 15, tzinfo=IST))
    assert not schedule.is_valid_slot(datetime(2026, 9, 14, 10, 20, tzinfo=IST))
    assert not schedule.is_valid_slot(datetime(2026, 9, 13, 10, 15, tzinfo=IST))


def test_speak_time_reads_naturally():
    assert speak_time(time(10, 0)) == "10am"
    assert speak_time(time(13, 0)) == "1pm"
    assert speak_time(time(16, 30)) == "4:30pm"
    assert speak_time(time(0, 0)) == "12am"
    assert speak_time(time(12, 0)) == "12pm"


def test_speak_slot_prefers_today_and_tomorrow():
    slot = datetime(2026, 9, 14, 16, 30, tzinfo=IST)
    assert speak_slot(slot, today=date(2026, 9, 14)) == "today at 4:30pm"
    assert speak_slot(slot, today=date(2026, 9, 13)) == "tomorrow at 4:30pm"
    assert "Monday" in speak_slot(slot, today=date(2026, 9, 1))


def test_render_hours_groups_consecutive_days_and_names_closed_ones():
    spoken = render_hours(load_schedule(RAW))

    assert "Monday and Tuesday, 10am to 1pm and 5pm to 8pm" in spoken
    assert "Saturday, 10am to 1pm" in spoken
    assert "closed on" in spoken
    assert "Sunday" in spoken


def test_render_hours_has_nothing_a_voice_should_not_read():
    spoken = render_hours(load_schedule(RAW))
    for symbol in ("*", "#", "-", "—", "–"):
        assert symbol not in spoken
