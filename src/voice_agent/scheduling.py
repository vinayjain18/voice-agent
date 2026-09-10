"""Consulting hours and the slot grid.

The grid is derived, never stored: clinic hours plus a slot length produce every
possible appointment time, and availability is that grid minus what is already
booked. Nothing to maintain by hand and nothing to fall out of step.

The spoken "we are open ..." line is rendered from the same schedule, so the
hours the agent says and the slots it will actually offer cannot disagree. That
is the same failure the language profile exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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
class Schedule:
    timezone: str = "Asia/Kolkata"
    slot_minutes: int = 15
    booking_horizon_days: int = 30
    # weekday name -> list of (open, close) windows. An absent or empty list
    # means closed that day.
    weekly: dict[str, list[tuple[time, time]]] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_open_on(self, day: date) -> bool:
        return bool(self.weekly.get(DAY_NAMES[day.weekday()]))

    def slots_for(self, day: date) -> list[datetime]:
        """Every slot start on this day, whether taken or not."""
        out: list[datetime] = []
        for opens, closes in self.weekly.get(DAY_NAMES[day.weekday()], []):
            cursor = datetime.combine(day, opens, tzinfo=self.tz)
            end = datetime.combine(day, closes, tzinfo=self.tz)
            step = timedelta(minutes=self.slot_minutes)
            while cursor + step <= end:
                out.append(cursor)
                cursor += step
        return out

    def is_valid_slot(self, moment: datetime) -> bool:
        return moment in set(self.slots_for(moment.date()))

    def horizon_end(self, today: date) -> date:
        return today + timedelta(days=self.booking_horizon_days)


def available_slots(
    schedule: Schedule,
    day: date,
    taken: set[datetime],
    *,
    now: datetime,
    min_notice_minutes: int = 0,
) -> list[datetime]:
    """Free slots on a day, excluding any already in the past."""
    cutoff = now.astimezone(schedule.tz) + timedelta(minutes=min_notice_minutes)
    return [
        slot for slot in schedule.slots_for(day) if slot not in taken and slot > cutoff
    ]


def next_available(
    schedule: Schedule,
    taken: set[datetime],
    *,
    now: datetime,
    limit: int = 3,
    start: date | None = None,
    min_notice_minutes: int = 0,
) -> list[datetime]:
    """The soonest free slots, searching forward across the booking horizon."""
    today = (start or now.astimezone(schedule.tz).date())
    found: list[datetime] = []
    for offset in range(schedule.booking_horizon_days + 1):
        day = today + timedelta(days=offset)
        found.extend(
            available_slots(
                schedule, day, taken, now=now, min_notice_minutes=min_notice_minutes
            )
        )
        if len(found) >= limit:
            break
    return found[:limit]


def load_schedule(raw: dict) -> Schedule:
    """Build a Schedule from the `schedule` object in the business profile."""
    if not isinstance(raw, dict):
        raise ScheduleError("`schedule` in the business profile must be an object.")

    weekly: dict[str, list[tuple[time, time]]] = {}
    for name, windows in (raw.get("weekly") or {}).items():
        key = str(name).strip().lower()
        if key not in DAY_NAMES:
            raise ScheduleError(f"'{name}' is not a weekday name in `schedule.weekly`.")
        parsed: list[tuple[time, time]] = []
        for window in windows or []:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                raise ScheduleError(f"{key}: each window must be a [open, close] pair.")
            opens, closes = (_parse_time(str(part), key) for part in window)
            if opens >= closes:
                raise ScheduleError(f"{key}: {window[0]} is not before {window[1]}.")
            parsed.append((opens, closes))
        weekly[key] = parsed

    slot_minutes = int(raw.get("slot_minutes", 15))
    if slot_minutes <= 0:
        raise ScheduleError("`schedule.slot_minutes` must be a positive number.")

    return Schedule(
        timezone=str(raw.get("timezone", "Asia/Kolkata")),
        slot_minutes=slot_minutes,
        booking_horizon_days=int(raw.get("booking_horizon_days", 30)),
        weekly=weekly,
    )


def _parse_time(value: str, day: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except ValueError as exc:
        raise ScheduleError(f"{day}: '{value}' is not a HH:MM time.") from exc


def render_hours(schedule: Schedule) -> str:
    """The spoken opening-hours line, built from the schedule itself."""
    open_days = [name for name in DAY_NAMES if schedule.weekly.get(name)]
    if not open_days:
        return "We're closed at the moment."

    groups: list[tuple[list[str], list[tuple[time, time]]]] = []
    for name in open_days:
        windows = schedule.weekly[name]
        if groups and groups[-1][1] == windows and _is_next_day(groups[-1][0][-1], name):
            groups[-1][0].append(name)
        else:
            groups.append(([name], windows))

    parts = [
        f"{_day_range(names)}, {_window_text(windows)}" for names, windows in groups
    ]
    sentence = "We're open " + "; ".join(parts) + "."

    closed = [name for name in DAY_NAMES if not schedule.weekly.get(name)]
    if closed:
        sentence += f" We're closed on {_join_names(closed)}."
    return sentence


def _is_next_day(previous: str, candidate: str) -> bool:
    return DAY_NAMES.index(candidate) == DAY_NAMES.index(previous) + 1


def _day_range(names: list[str]) -> str:
    if len(names) == 1:
        return names[0].capitalize()
    if len(names) == 2:
        return f"{names[0].capitalize()} and {names[1].capitalize()}"
    return f"{names[0].capitalize()} to {names[-1].capitalize()}"


def _join_names(names: list[str]) -> str:
    titled = [name.capitalize() for name in names]
    if len(titled) == 1:
        return titled[0]
    return f"{', '.join(titled[:-1])} and {titled[-1]}"


def _window_text(windows: list[tuple[time, time]]) -> str:
    spoken = [f"{speak_time(opens)} to {speak_time(closes)}" for opens, closes in windows]
    if len(spoken) == 1:
        return spoken[0]
    return f"{', '.join(spoken[:-1])} and {spoken[-1]}"


def speak_time(value: time | datetime) -> str:
    """Render a time the way it should be said: 10am, 1pm, 4:30pm."""
    moment = value.time() if isinstance(value, datetime) else value
    suffix = "am" if moment.hour < 12 else "pm"
    hour = moment.hour % 12 or 12
    if moment.minute:
        return f"{hour}:{moment.minute:02d}{suffix}"
    return f"{hour}{suffix}"


def speak_slot(moment: datetime, *, today: date | None = None) -> str:
    """A slot as a person would say it: "tomorrow at 4:30pm"."""
    when = speak_time(moment)
    if today is not None:
        delta = (moment.date() - today).days
        if delta == 0:
            return f"today at {when}"
        if delta == 1:
            return f"tomorrow at {when}"
    return f"{moment.strftime('%A %d %B')} at {when}"
