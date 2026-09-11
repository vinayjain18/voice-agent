"""Departments, their opening hours, and the slot grid.

Two things make this different from a single-doctor diary:

  - Every department has its own grid, so dentistry and ophthalmology can hold
    the same instant without clashing, and each can keep its own hours.
  - Slots are anchored in UTC. Callers give times in their own timezone and the
    hospital takes calls around the clock, so UTC is the only representation
    that does not need a footnote.

A department with no `weekly` block is open continuously, which is the default
here. One with a `weekly` block is open only in those windows, expressed in the
hospital's local time and converted to UTC for storage and comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

DAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class ScheduleError(ValueError):
    """The schedule in the business profile is malformed."""


@dataclass(frozen=True)
class Department:
    name: str
    slot_minutes: int = 30
    description: str = ""
    # Empty means open around the clock.
    weekly: dict[str, list[tuple[time, time]]] = field(default_factory=dict)

    @property
    def is_always_open(self) -> bool:
        return not self.weekly

    def slots_on(self, day: date, *, hospital_tz: ZoneInfo) -> list[datetime]:
        """Every slot start touching this local day, as UTC instants."""
        step = timedelta(minutes=self.slot_minutes)

        if self.is_always_open:
            # Anchored to UTC midnight so the grid is stable regardless of which
            # timezone the caller happens to be describing it from.
            start = datetime.combine(day, time(0, 0), tzinfo=UTC)
            count = int(timedelta(days=1) / step)
            return [start + step * i for i in range(count)]

        out: list[datetime] = []
        for opens, closes in self.weekly.get(DAY_NAMES[day.weekday()], []):
            cursor = datetime.combine(day, opens, tzinfo=hospital_tz)
            end = datetime.combine(day, closes, tzinfo=hospital_tz)
            while cursor + step <= end:
                out.append(cursor.astimezone(UTC))
                cursor += step
        return out

    def is_open_on(self, day: date, *, hospital_tz: ZoneInfo) -> bool:
        return bool(self.slots_on(day, hospital_tz=hospital_tz))


@dataclass(frozen=True)
class Schedule:
    timezone: str = "America/New_York"
    booking_horizon_days: int = 60
    departments: dict[str, Department] = field(default_factory=dict)
    # Spoken names callers use ("eye doctor") mapped to a department key.
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def names(self) -> list[str]:
        return sorted(self.departments)

    def department(self, name: str) -> Department | None:
        """Match a department by name, tolerantly.

        Callers say "eye doctor" and "the dentist", not "ophthalmology". The
        profile carries the aliases; this only does the lookup.
        """
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        if wanted in self.departments:
            return self.departments[wanted]
        if wanted in self.aliases:
            return self.departments.get(self.aliases[wanted])

        # Longest alias appearing as whole words, so "I need to see the eye
        # doctor" resolves. Word boundaries are load-bearing: plain substring
        # matching let the alias "pt" fire inside "dept" and route a caller to
        # physical therapy, and "ent" would match "different".
        mentioned = [key for key in self.aliases if _mentions(wanted, key)]
        if mentioned:
            return self.departments.get(self.aliases[max(mentioned, key=len)])

        hits = [key for key in self.departments if _mentions(wanted, key)]
        if len(hits) == 1:
            return self.departments[hits[0]]
        return None

    def is_valid_slot(self, department: Department, moment: datetime) -> bool:
        instant = moment.astimezone(UTC)
        for day in _days_around(instant):
            if instant in set(department.slots_on(day, hospital_tz=self.tz)):
                return True
        return False

    def horizon_end(self, today: date) -> date:
        return today + timedelta(days=self.booking_horizon_days)


def _mentions(haystack: str, needle: str) -> bool:
    """True when `needle` appears in `haystack` as whole words."""
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def _days_around(instant: datetime) -> list[date]:
    """The local days a UTC instant could belong to, allowing for offsets."""
    day = instant.date()
    return [day - timedelta(days=1), day, day + timedelta(days=1)]


def available_slots(
    schedule: Schedule,
    department: Department,
    day: date,
    taken: set[datetime],
    *,
    now: datetime,
    min_notice_minutes: int = 0,
) -> list[datetime]:
    """Free slots for one department on one day, as UTC instants."""
    cutoff = now.astimezone(UTC) + timedelta(minutes=min_notice_minutes)
    return [
        slot
        for slot in department.slots_on(day, hospital_tz=schedule.tz)
        if slot not in taken and slot > cutoff
    ]


def next_available(
    schedule: Schedule,
    department: Department,
    taken: set[datetime],
    *,
    now: datetime,
    limit: int = 3,
    start: date | None = None,
    min_notice_minutes: int = 0,
) -> list[datetime]:
    """The soonest free slots for a department, searching forward."""
    first = start or now.astimezone(UTC).date()
    found: list[datetime] = []
    for offset in range(schedule.booking_horizon_days + 1):
        found.extend(
            available_slots(
                schedule,
                department,
                first + timedelta(days=offset),
                taken,
                now=now,
                min_notice_minutes=min_notice_minutes,
            )
        )
        if len(found) >= limit:
            break
    return sorted(found)[:limit]


def load_schedule(raw: dict, aliases: dict | None = None) -> Schedule:
    """Build a Schedule from the `schedule` object in the business profile."""
    if not isinstance(raw, dict):
        raise ScheduleError("`schedule` in the business profile must be an object.")

    departments: dict[str, Department] = {}
    for name, config in (raw.get("departments") or {}).items():
        key = str(name).strip().lower()
        if not key:
            raise ScheduleError("a department has an empty name.")
        if not isinstance(config, dict):
            raise ScheduleError(f"{key}: department config must be an object.")

        slot_minutes = int(config.get("slot_minutes", 30))
        if slot_minutes <= 0:
            raise ScheduleError(f"{key}: slot_minutes must be a positive number.")
        if 1440 % slot_minutes:
            raise ScheduleError(
                f"{key}: slot_minutes must divide into 24 hours, so the grid is "
                f"the same every day. Got {slot_minutes}."
            )

        departments[key] = Department(
            name=key,
            slot_minutes=slot_minutes,
            description=str(config.get("description", "")),
            weekly=_weekly(config.get("weekly") or {}, key),
        )

    if not departments:
        raise ScheduleError("`schedule.departments` must list at least one department.")

    timezone = str(raw.get("timezone", "America/New_York"))
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ScheduleError(f"`schedule.timezone` is not a valid timezone: {timezone}") from exc

    resolved: dict[str, str] = {}
    for spoken, target in (aliases or {}).items():
        key = str(target).strip().lower()
        if key not in departments:
            raise ScheduleError(
                f"department_aliases maps '{spoken}' to '{key}', which is not a department."
            )
        resolved[str(spoken).strip().lower()] = key

    return Schedule(
        timezone=timezone,
        booking_horizon_days=int(raw.get("booking_horizon_days", 60)),
        departments=departments,
        aliases=resolved,
    )


def _weekly(raw: dict, department: str) -> dict[str, list[tuple[time, time]]]:
    weekly: dict[str, list[tuple[time, time]]] = {}
    for name, windows in raw.items():
        key = str(name).strip().lower()
        if key not in DAY_NAMES:
            raise ScheduleError(f"{department}: '{name}' is not a weekday name.")
        parsed: list[tuple[time, time]] = []
        for window in windows or []:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                raise ScheduleError(f"{department} {key}: each window must be [open, close].")
            opens, closes = (_parse_time(str(part), f"{department} {key}") for part in window)
            if opens >= closes:
                raise ScheduleError(f"{department} {key}: {window[0]} is not before {window[1]}.")
            parsed.append((opens, closes))
        weekly[key] = parsed
    return weekly


def _parse_time(value: str, where: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except ValueError as exc:
        raise ScheduleError(f"{where}: '{value}' is not a HH:MM time.") from exc


def render_hours(schedule: Schedule) -> str:
    """The spoken opening-hours line, built from the schedule itself.

    Departments that keep identical hours are named together. Ten separate
    sentences is a wall of text to read down a phone line, and most of a
    hospital's clinics keep the same office hours anyway.
    """
    always = [name for name, dept in schedule.departments.items() if dept.is_always_open]
    limited = [name for name, dept in schedule.departments.items() if not dept.is_always_open]

    parts: list[str] = []
    if always:
        if not limited:
            return "We're open twenty four hours a day, every day."
        parts.append(f"{_join(always).capitalize()} run twenty four hours a day.")

    # Group by the hours themselves, keeping the largest group first so the
    # answer opens with what is true of most of the hospital.
    grouped: dict[str, list[str]] = {}
    for name in limited:
        grouped.setdefault(_window_text(schedule.departments[name]), []).append(name)

    for hours, names in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        verb = "are" if len(names) > 1 else "is"
        parts.append(f"{_join(names).capitalize()} {verb} {hours}.")
    return " ".join(parts)


def hours_for(schedule: Schedule, department: Department) -> str:
    """The spoken hours for one department."""
    if department.is_always_open:
        return f"{department.name.capitalize()} is open twenty four hours a day."
    return f"{department.name.capitalize()} is {_window_text(department)}."


def _window_text(department: Department) -> str:
    groups: list[tuple[list[str], list[tuple[time, time]]]] = []
    for name in DAY_NAMES:
        windows = department.weekly.get(name)
        if not windows:
            continue
        if groups and groups[-1][1] == windows and _is_next_day(groups[-1][0][-1], name):
            groups[-1][0].append(name)
        else:
            groups.append(([name], windows))
    if not groups:
        return "closed"
    return "; ".join(
        f"{_day_range(names)}, {_spans(windows)}" for names, windows in groups
    )


def _is_next_day(previous: str, candidate: str) -> bool:
    return DAY_NAMES.index(candidate) == DAY_NAMES.index(previous) + 1


def _day_range(names: list[str]) -> str:
    if len(names) == 1:
        return names[0].capitalize()
    if len(names) == 2:
        return f"{names[0].capitalize()} and {names[1].capitalize()}"
    return f"{names[0].capitalize()} to {names[-1].capitalize()}"


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _spans(windows: list[tuple[time, time]]) -> str:
    spoken = [f"{speak_time(opens)} to {speak_time(closes)}" for opens, closes in windows]
    return _join(spoken)


def speak_time(value: time | datetime) -> str:
    """Render a time the way it should be said: 10am, 1pm, 4:30pm."""
    moment = value.time() if isinstance(value, datetime) else value
    suffix = "am" if moment.hour < 12 else "pm"
    hour = moment.hour % 12 or 12
    if moment.minute:
        return f"{hour}:{moment.minute:02d}{suffix}"
    return f"{hour}{suffix}"


def speak_slot(moment: datetime, *, tz: ZoneInfo, today: date | None = None) -> str:
    """A UTC instant as the caller would say it, in their own timezone."""
    local = moment.astimezone(tz)
    when = speak_time(local)
    if today is not None:
        delta = (local.date() - today).days
        if delta == 0:
            return f"today at {when}"
        if delta == 1:
            return f"tomorrow at {when}"
    return f"{local.strftime('%A %d %B')} at {when}"
