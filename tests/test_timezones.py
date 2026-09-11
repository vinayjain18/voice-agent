"""Timezone resolution.

A wrong offset books someone at the wrong hour and nobody finds out until they
fail to turn up, so the rule here is: resolve confidently or return None and let
the caller be asked.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from voice_agent.timezones import describe, resolve_timezone, to_utc

UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("IST", "Asia/Kolkata"),
        ("ist", "Asia/Kolkata"),
        ("India time", "Asia/Kolkata"),
        ("two in the afternoon India time", "Asia/Kolkata"),
        ("South African time", "Africa/Johannesburg"),
        ("south africa", "Africa/Johannesburg"),
        ("SAST", "Africa/Johannesburg"),
        ("Pacific time", "America/Los_Angeles"),
        ("PST", "America/Los_Angeles"),
        ("eastern", "America/New_York"),
        ("I'm in London", "Europe/London"),
        ("UK", "Europe/London"),
        ("Sydney", "Australia/Sydney"),
        ("Asia/Kolkata", "Asia/Kolkata"),
        ("UTC", "UTC"),
        ("utc", "UTC"),
        ("gmt", "UTC"),
    ],
)
def test_common_ways_of_saying_a_timezone(spoken, expected):
    tz = resolve_timezone(spoken)
    assert tz is not None, spoken
    assert str(tz) == expected


@pytest.mark.parametrize("spoken", ["", "   ", "somewhere silly", "Narnia time", "asdf"])
def test_anything_unrecognised_returns_none_rather_than_guessing(spoken):
    assert resolve_timezone(spoken) is None


def test_three_letter_us_zones_observe_daylight_saving():
    """The tz database ships a legacy fixed-offset "EST" that never shifts.

    Resolving to it would put a New York caller an hour out for eight months of
    the year, so the alias table has to win over the raw IANA lookup.
    """
    tz = resolve_timezone("EST")
    assert str(tz) == "America/New_York"

    winter = describe(tz, at=datetime(2026, 1, 15, tzinfo=UTC))
    summer = describe(tz, at=datetime(2026, 7, 15, tzinfo=UTC))
    assert "UTC-05:00" in winter
    assert "UTC-04:00" in summer


def test_arizona_correctly_does_not_observe_daylight_saving():
    tz = resolve_timezone("arizona")
    winter = describe(tz, at=datetime(2026, 1, 15, tzinfo=UTC))
    summer = describe(tz, at=datetime(2026, 7, 15, tzinfo=UTC))
    assert "UTC-07:00" in winter and "UTC-07:00" in summer


def test_the_longest_match_wins():
    """"south africa" must beat a bare "africa" style partial."""
    assert str(resolve_timezone("south african time")) == "Africa/Johannesburg"


def test_to_utc_converts_a_wall_clock_time():
    """Two in the afternoon in India is 08:30 UTC.

    The naive datetimes here are the point: to_utc exists to attach a timezone
    to a wall-clock time the caller read off their own clock.
    """
    tz = resolve_timezone("IST")
    result = to_utc(datetime(2026, 9, 14, 14, 0), tz)  # noqa: DTZ001

    assert result == datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    assert result.tzinfo == UTC


def test_to_utc_respects_daylight_saving_on_the_day():
    tz = resolve_timezone("eastern")
    winter = to_utc(datetime(2026, 1, 15, 9, 0), tz)  # noqa: DTZ001
    summer = to_utc(datetime(2026, 7, 15, 9, 0), tz)  # noqa: DTZ001

    assert winter.hour == 14  # UTC-5
    assert summer.hour == 13  # UTC-4


def test_describe_reads_like_something_you_could_check():
    assert describe(ZoneInfo("Asia/Kolkata")) == "Asia/Kolkata (UTC+05:30)"
