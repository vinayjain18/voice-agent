from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from voice_agent.scheduling import (
    UTC,
    Department,
    Schedule,
    ScheduleError,
    available_slots,
    load_schedule,
    next_available,
    render_hours,
    speak_slot,
    speak_time,
)

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

RAW = {
    "timezone": "America/New_York",
    "booking_horizon_days": 60,
    "departments": {
        "family medicine": {"slot_minutes": 20, "description": "checkups"},
        "dentistry": {"slot_minutes": 30},
        # A department that keeps real office hours rather than running around
        # the clock, which is what the `weekly` block is for.
        "eye care": {
            "slot_minutes": 30,
            "weekly": {
                "monday": [["09:00", "12:00"]],
                "tuesday": [["09:00", "12:00"]],
                "sunday": [],
            },
        },
    },
}

ALIASES = {"dentist": "dentistry", "eye doctor": "eye care", "gp": "family medicine"}


def schedule() -> Schedule:
    return load_schedule(RAW, ALIASES)


def test_departments_load_with_their_own_slot_lengths():
    s = schedule()
    assert s.names == ["dentistry", "eye care", "family medicine"]
    assert s.departments["family medicine"].slot_minutes == 20
    assert s.departments["dentistry"].slot_minutes == 30


def test_a_department_with_no_hours_is_open_around_the_clock():
    dentistry = schedule().departments["dentistry"]
    assert dentistry.is_always_open
    slots = dentistry.slots_on(date(2026, 9, 14), hospital_tz=NY)
    assert len(slots) == 48
    assert all(slot.tzinfo == UTC for slot in slots)
    assert slots[0] == datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 9, 14, 23, 30, tzinfo=UTC)


def test_a_department_with_hours_only_opens_in_them():
    eye = schedule().departments["eye care"]
    assert not eye.is_always_open

    monday = eye.slots_on(date(2026, 9, 14), hospital_tz=NY)
    assert len(monday) == 6
    # 9am New York is 13:00 UTC in September (daylight saving).
    assert monday[0] == datetime(2026, 9, 14, 13, 0, tzinfo=UTC)

    assert eye.slots_on(date(2026, 9, 13), hospital_tz=NY) == []


def test_slot_length_must_divide_the_day():
    """Otherwise the 24 hour grid would not line up from one day to the next."""
    with pytest.raises(ScheduleError, match="divide into 24 hours"):
        load_schedule({"departments": {"x": {"slot_minutes": 7}}})


def test_a_schedule_with_no_departments_is_refused():
    with pytest.raises(ScheduleError, match="at least one department"):
        load_schedule({"departments": {}})


def test_an_alias_pointing_nowhere_is_refused():
    with pytest.raises(ScheduleError, match="not a department"):
        load_schedule(RAW, {"dentist": "nonexistent"})


def test_a_backwards_window_is_refused():
    with pytest.raises(ScheduleError, match="not before"):
        load_schedule({"departments": {"x": {"weekly": {"monday": [["18:00", "10:00"]]}}}})


def test_an_unknown_weekday_is_refused():
    with pytest.raises(ScheduleError, match="not a weekday"):
        load_schedule({"departments": {"x": {"weekly": {"funday": [["10:00", "11:00"]]}}}})


def test_an_invalid_timezone_is_refused():
    with pytest.raises(ScheduleError, match="not a valid timezone"):
        load_schedule({"timezone": "Mars/Olympus", "departments": {"x": {}}})


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("dentistry", "dentistry"),
        ("dentist", "dentistry"),
        ("eye doctor", "eye care"),
        ("I need to see the eye doctor", "eye care"),
        ("gp", "family medicine"),
        ("", None),
        ("astrophysics", None),
    ],
)
def test_departments_resolve_from_what_callers_actually_say(spoken, expected):
    found = schedule().department(spoken)
    assert (found.name if found else None) == expected


def test_a_short_alias_does_not_match_inside_another_word():
    """"gp" must not fire inside "gpu", and "pt" must not fire inside "dept"."""
    s = load_schedule(RAW, {**ALIASES, "pt": "dentistry"})
    assert s.department("my gpu is broken") is None
    assert s.department("wrong dept") is None


def test_departments_do_not_clash_with_each_other():
    """The same instant can be booked once per department."""
    s = schedule()
    instant = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    dentistry = s.departments["dentistry"]
    family = s.departments["family medicine"]

    free_family = available_slots(
        s, family, date(2026, 9, 14), {instant}, now=datetime(2026, 9, 1, tzinfo=UTC)
    )
    free_dentistry = available_slots(
        s, dentistry, date(2026, 9, 14), {instant}, now=datetime(2026, 9, 1, tzinfo=UTC)
    )

    # The set of taken instants is per department, so blocking one leaves the
    # other untouched at the same moment.
    assert instant not in free_dentistry
    assert instant not in free_family
    assert len(free_family) > len(free_dentistry)  # 20 minute slots vs 30


def test_available_slots_exclude_taken_and_past():
    s = schedule()
    dentistry = s.departments["dentistry"]
    now = datetime(2026, 9, 14, 11, 0, tzinfo=UTC)
    taken = {datetime(2026, 9, 14, 11, 30, tzinfo=UTC)}

    free = available_slots(s, dentistry, date(2026, 9, 14), taken, now=now)

    assert datetime(2026, 9, 14, 10, 0, tzinfo=UTC) not in free
    assert datetime(2026, 9, 14, 11, 30, tzinfo=UTC) not in free
    assert datetime(2026, 9, 14, 12, 0, tzinfo=UTC) in free


def test_min_notice_pushes_the_first_offer_out():
    s = schedule()
    now = datetime(2026, 9, 14, 11, 0, tzinfo=UTC)

    free = available_slots(
        s, s.departments["dentistry"], date(2026, 9, 14), set(), now=now, min_notice_minutes=60
    )

    assert free[0] == datetime(2026, 9, 14, 12, 30, tzinfo=UTC)


def test_next_available_rolls_into_following_days():
    s = schedule()
    # Sunday: eye care is shut, so the next offers must be Monday's.
    now = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)

    offers = next_available(s, s.departments["eye care"], set(), now=now, limit=2)

    assert [slot.date() for slot in offers] == [date(2026, 9, 14), date(2026, 9, 14)]


def test_next_available_returns_nothing_when_fully_booked():
    department = Department(name="x", slot_minutes=60, weekly={"monday": [(time(10, 0), time(11, 0))]})
    s = Schedule(timezone="America/New_York", booking_horizon_days=1, departments={"x": department})
    now = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
    taken = set(department.slots_on(date(2026, 9, 14), hospital_tz=NY))

    assert next_available(s, department, taken, now=now, limit=3) == []


def test_is_valid_slot_rejects_an_off_grid_time():
    s = schedule()
    dentistry = s.departments["dentistry"]
    assert s.is_valid_slot(dentistry, datetime(2026, 9, 14, 10, 30, tzinfo=UTC))
    assert not s.is_valid_slot(dentistry, datetime(2026, 9, 14, 10, 20, tzinfo=UTC))


def test_a_time_given_in_another_timezone_lands_on_the_grid():
    """Two in the afternoon India time is 08:30 UTC, which is a valid slot."""
    s = schedule()
    instant = datetime(2026, 9, 14, 14, 0, tzinfo=IST)
    assert instant.astimezone(UTC) == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert s.is_valid_slot(s.departments["dentistry"], instant)


def test_speak_time_reads_naturally():
    assert speak_time(time(10, 0)) == "10am"
    assert speak_time(time(13, 0)) == "1pm"
    assert speak_time(time(16, 30)) == "4:30pm"
    assert speak_time(time(0, 0)) == "12am"
    assert speak_time(time(12, 0)) == "12pm"


def test_speak_slot_renders_in_the_callers_timezone():
    """The whole point of storing UTC: everyone hears their own clock."""
    instant = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)

    assert speak_slot(instant, tz=IST, today=date(2026, 9, 14)) == "today at 2pm"
    assert speak_slot(instant, tz=NY, today=date(2026, 9, 14)) == "today at 4:30am"
    assert speak_slot(instant, tz=IST, today=date(2026, 9, 13)) == "tomorrow at 2pm"
    assert "Monday" in speak_slot(instant, tz=IST, today=date(2026, 9, 1))


def test_render_hours_says_round_the_clock_when_everything_is():
    s = load_schedule({"departments": {"a": {}, "b": {}}})
    assert "twenty four hours" in render_hours(s)


def test_render_hours_names_the_departments_that_keep_office_hours():
    spoken = render_hours(schedule())
    assert "twenty four hours" in spoken
    assert "Eye care is" in spoken
    assert "9am to 12pm" in spoken


def test_render_hours_has_nothing_a_voice_should_not_read():
    spoken = render_hours(schedule())
    for symbol in ("*", "#", "—", "–", "{", "}"):
        assert symbol not in spoken


def test_horizon_end_respects_the_booking_window():
    assert schedule().horizon_end(date(2026, 9, 14)) == date(2026, 9, 14) + timedelta(days=60)
