"""The first concrete agent: a front-desk receptionist."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import Agent, RunContext, function_tool, get_job_context

from voice_agent.business import BusinessProfile, load_profile
from voice_agent.config import Settings
from voice_agent.prompts import render_prompt
from voice_agent.storage import append_lead

logger = logging.getLogger(__name__)

# The business runs on India time, and callers speak in it.
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")


def build_prompt_variables(
    profile: BusinessProfile, settings: Settings
) -> dict[str, str]:
    """Business facts plus the things only known at call time."""
    variables = profile.as_prompt_vars()

    # The model has no idea what today is. Without this it invents dates when a
    # caller says "next Tuesday", confidently and wrongly.
    now = datetime.now(BUSINESS_TZ)
    variables["current_datetime"] = now.strftime("%A %d %B %Y, %I:%M %p")
    variables["current_date"] = now.strftime("%Y-%m-%d")

    # The language profile is the source of truth here, not profile.json,
    # because it is validated against the TTS at startup.
    variables["languages"] = settings.language.spoken_languages

    return variables


class ReceptionistAgent(Agent):
    def __init__(
        self,
        profile: BusinessProfile | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.profile = profile or load_profile()
        self.settings = settings or Settings.load()

        super().__init__(
            instructions=render_prompt(
                "receptionist", build_prompt_variables(self.profile, self.settings)
            )
        )

    @function_tool
    async def get_business_hours(self, context: RunContext) -> str:
        """Return the opening hours of the business.

        Call this when the caller asks when the business is open or available.
        """
        return f"We're open {self.profile['hours']}."

    @function_tool
    async def book_callback(
        self,
        context: RunContext,
        name: str,
        phone_or_email: str,
        preferred_date: str,
        preferred_time: str,
        reason: str,
        raw_request: str = "",
    ) -> str:
        """Book a call with the team at a specific date and time.

        Call this ONLY once you have all of: the caller's name, a phone number
        or email, and a date and time they want the call.

        Args:
            name: The caller's name.
            phone_or_email: How to reach them.
            preferred_date: The date as YYYY-MM-DD. Work it out from today's
                date given in your instructions - never guess the year.
            preferred_time: 24-hour HH:MM in India time, e.g. "15:00".
            reason: A one-line summary of what they want to discuss.
            raw_request: What the caller actually said about timing, e.g.
                "next Tuesday afternoon", so a human can double-check.
        """
        missing = _missing_contact_fields(name, phone_or_email)
        if missing:
            return (
                f"Do not save yet - still missing the caller's {missing}. "
                f"Ask for it, then call this tool again."
            )

        room_name, caller = _call_identity()
        append_lead(
            self.settings.storage.leads_file,
            kind="booking",
            name=name,
            contact=phone_or_email,
            reason=reason,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            raw_request=raw_request,
            caller_number=caller,
            room=room_name,
        )
        return (
            f"Booked. Confirm back to {name} that the team will call on "
            f"{preferred_date} at {preferred_time} India time."
        )

    @function_tool
    async def take_callback_details(
        self, context: RunContext, name: str, phone_or_email: str, reason: str
    ) -> str:
        """Record a caller's details when no specific time was agreed.

        Use book_callback instead whenever the caller will give you a date and
        time. Use this one only for "just have someone call me" or to leave a
        message.

        Call this ONLY once you have both the caller's name and a phone number
        or email.

        Args:
            name: The caller's name.
            phone_or_email: How to reach them back.
            reason: A one-line summary of what they need.
        """
        missing = _missing_contact_fields(name, phone_or_email)
        if missing:
            return (
                f"Do not save yet - still missing the caller's {missing}. "
                f"Ask for it, then call this tool again."
            )

        room_name, caller = _call_identity()
        append_lead(
            self.settings.storage.leads_file,
            kind="message",
            name=name,
            contact=phone_or_email,
            reason=reason,
            caller_number=caller,
            room=room_name,
        )
        return f"Noted. Someone will get back to {name} shortly."


def _missing_contact_fields(name: str, phone_or_email: str) -> str:
    """Guard against saving a row with nobody to contact.

    Models call tools eagerly, sometimes before they have asked for a name or a
    number, which produces a useless row. Returning an instruction instead of
    saving lets the model recover mid-call.
    """
    missing = []
    if not name or not name.strip():
        missing.append("name")
    if not phone_or_email or not phone_or_email.strip():
        missing.append("phone number or email")
    return " and ".join(missing)


def _call_identity() -> tuple[str, str]:
    """Best-effort room name and caller number for the current call.

    The SDK exposes no constant for the SIP caller attribute, so rather than
    hardcode a key that may not exist we scan the SIP participant's attributes
    for anything phone-shaped and fall back to its identity (which for inbound
    SIP encodes the number). Console and browser sessions have neither, and
    return empty strings.
    """
    ctx = get_job_context(required=False)
    if ctx is None:
        return "", ""

    room = getattr(ctx.room, "name", "") or ""

    try:
        for participant in ctx.room.remote_participants.values():
            attributes = dict(getattr(participant, "attributes", {}) or {})
            for key, value in attributes.items():
                if "phone" in key.lower() and value:
                    return room, str(value)
            identity = getattr(participant, "identity", "") or ""
            if identity.startswith("sip_"):
                return room, identity.removeprefix("sip_")
    except Exception:
        logger.debug("could not resolve caller identity", exc_info=True)

    return room, ""
