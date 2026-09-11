"""Work out which timezone a caller means.

Appointment times are stored in UTC, so a caller who says "two in the afternoon,
South African time" has to be converted before anything is saved. Doing that
conversion in Python rather than asking the model for a UTC timestamp is
deliberate: models are confident and frequently wrong about offsets, and a
booking an hour out is worse than one that was never made.

Resolution is conservative. Anything not recognised returns None so the tool can
ask which city they are in, rather than silently assuming the hospital's own
timezone and putting someone on the wrong side of the world.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Abbreviations and the phrases people actually say on a call. Daylight variants
# map to the same IANA zone: ZoneInfo applies the right offset for the date, so
# "EST" in July still resolves correctly to Eastern Daylight Time.
ALIASES: dict[str, str] = {
    "utc": "UTC",
    "gmt": "UTC",
    "zulu": "UTC",
    # United States
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "eastern": "America/New_York",
    "eastern time": "America/New_York",
    "new york": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "central": "America/Chicago",
    "central time": "America/Chicago",
    "chicago": "America/Chicago",
    "texas": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "mountain": "America/Denver",
    "mountain time": "America/Denver",
    "denver": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "california": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "arizona": "America/Phoenix",
    "phoenix": "America/Phoenix",
    # India
    "ist": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "india time": "Asia/Kolkata",
    "indian": "Asia/Kolkata",
    "indian time": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata",
    "calcutta": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    # Africa
    "sast": "Africa/Johannesburg",
    "south africa": "Africa/Johannesburg",
    "south african": "Africa/Johannesburg",
    "south african time": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    "nigeria": "Africa/Lagos",
    "lagos": "Africa/Lagos",
    "kenya": "Africa/Nairobi",
    "nairobi": "Africa/Nairobi",
    "egypt": "Africa/Cairo",
    "cairo": "Africa/Cairo",
    # Europe
    "bst": "Europe/London",
    "uk": "Europe/London",
    "uk time": "Europe/London",
    "britain": "Europe/London",
    "british": "Europe/London",
    "british time": "Europe/London",
    "england": "Europe/London",
    "london": "Europe/London",
    "ireland": "Europe/Dublin",
    "dublin": "Europe/Dublin",
    "cet": "Europe/Paris",
    "cest": "Europe/Paris",
    "france": "Europe/Paris",
    "paris": "Europe/Paris",
    "germany": "Europe/Berlin",
    "berlin": "Europe/Berlin",
    "spain": "Europe/Madrid",
    "madrid": "Europe/Madrid",
    "italy": "Europe/Rome",
    "rome": "Europe/Rome",
    "netherlands": "Europe/Amsterdam",
    "amsterdam": "Europe/Amsterdam",
    "portugal": "Europe/Lisbon",
    "lisbon": "Europe/Lisbon",
    "poland": "Europe/Warsaw",
    "warsaw": "Europe/Warsaw",
    "greece": "Europe/Athens",
    "athens": "Europe/Athens",
    "moscow": "Europe/Moscow",
    "russia": "Europe/Moscow",
    "turkey": "Europe/Istanbul",
    "istanbul": "Europe/Istanbul",
    # Middle East
    "gst": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "saudi": "Asia/Riyadh",
    "saudi arabia": "Asia/Riyadh",
    "riyadh": "Asia/Riyadh",
    "qatar": "Asia/Qatar",
    "doha": "Asia/Qatar",
    "israel": "Asia/Jerusalem",
    # Asia Pacific
    "pkt": "Asia/Karachi",
    "pakistan": "Asia/Karachi",
    "karachi": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka",
    "dhaka": "Asia/Dhaka",
    "sri lanka": "Asia/Colombo",
    "colombo": "Asia/Colombo",
    "nepal": "Asia/Kathmandu",
    "singapore": "Asia/Singapore",
    "malaysia": "Asia/Kuala_Lumpur",
    "hong kong": "Asia/Hong_Kong",
    "china": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "jst": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "korea": "Asia/Seoul",
    "seoul": "Asia/Seoul",
    "thailand": "Asia/Bangkok",
    "bangkok": "Asia/Bangkok",
    "vietnam": "Asia/Ho_Chi_Minh",
    "philippines": "Asia/Manila",
    "manila": "Asia/Manila",
    "indonesia": "Asia/Jakarta",
    "jakarta": "Asia/Jakarta",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
    "australia": "Australia/Sydney",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "perth": "Australia/Perth",
    "brisbane": "Australia/Brisbane",
    "new zealand": "Pacific/Auckland",
    "auckland": "Pacific/Auckland",
    # Americas
    "canada": "America/Toronto",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "mexico": "America/Mexico_City",
    "brazil": "America/Sao_Paulo",
    "sao paulo": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires",
}

# Words that carry no information and should not stop a phrase from matching.
NOISE = ("time", "timezone", "time zone", "zone", "standard", "daylight", "saving", "savings")


def resolve_timezone(value: str) -> ZoneInfo | None:
    """Best-effort ZoneInfo for whatever the caller said, or None.

    None means "ask them", never "assume". Guessing puts the appointment on the
    wrong side of the world and nobody finds out until they fail to turn up.
    """
    raw = (value or "").strip()
    if not raw:
        return None

    cleaned = _normalise(raw)

    # Aliases are checked before IANA on purpose. The tz database also ships
    # legacy fixed-offset zones named "EST" and "MST" which never observe
    # daylight saving, so ZoneInfo("EST") silently puts a New York caller an
    # hour out for eight months of the year.
    if cleaned in ALIASES:
        return ZoneInfo(ALIASES[cleaned])

    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        pass

    if not cleaned:
        return None

    # Longest alias contained in the phrase, so "I'm on the west coast, pacific
    # time" still resolves, and "south africa" beats "africa".
    hits = [key for key in ALIASES if key in cleaned]
    if hits:
        return ZoneInfo(ALIASES[max(hits, key=len)])

    return None


def _normalise(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"[^a-z_/\s]", " ", text)
    for word in NOISE:
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Read a wall-clock time as being in `tz`, and return it in UTC."""
    return naive.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def describe(tz: ZoneInfo, *, at: datetime | None = None) -> str:
    """A short label for a zone, e.g. "Asia/Kolkata (UTC+05:30)"."""
    moment = (at or datetime.now(ZoneInfo("UTC"))).astimezone(tz)
    offset = moment.utcoffset()
    if offset is None:
        return str(tz)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{tz} (UTC{sign}{hours:02d}:{minutes:02d})"
