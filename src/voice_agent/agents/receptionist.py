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
        preferred_date: str,
        preferred_time: str,
        reason: str,
        raw_request: str = "",
    ) -> str:
        """Book a call with the team at a specific date and time.

        This is the tool you should be aiming for in almost every conversation.
        Call it once you have the caller's name, a day AND a time.

        Never ask for a phone number or an email. We already have the caller's
        number from the call itself and it is recorded automatically.

        Args:
            name: The caller's name.
            preferred_date: The date as YYYY-MM-DD. Work it out from today's
                date given in your instructions - never guess the year.
            preferred_time: 24-hour HH:MM in India time, e.g. "15:00". Required.
                If the caller has only given a day, ask what time suits them
                before calling this.
            reason: A one-line summary of what they want to discuss.
            raw_request: What the caller actually said about timing, e.g.
                "next Tuesday afternoon", so a human can double-check.
        """
        room_name, caller = _call_identity()

        if not (name or "").strip():
            return "Do not save yet - ask the caller for their name, then call this tool again."
        if not (preferred_time or "").strip():
            return (
                "Do not save yet - no time was given. Ask what time of day suits "
                "them, then call this tool again."
            )

        append_lead(
            self.settings.storage.leads_file,
            kind="booking",
            name=name,
            contact=caller,
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
        self,
        context: RunContext,
        name: str,
        reason: str,
        why_no_time: str,
    ) -> str:
        """Fallback only. Take a message when no time could be agreed.

        Do NOT use this as your first choice. Use book_callback instead. Only
        use this after you have actually asked the caller for a day and a time
        and they would not or could not give one.

        Never ask for a phone number or an email.

        Args:
            name: The caller's name.
            reason: A one-line summary of what they need.
            why_no_time: What the caller said when you asked for a time, e.g.
                "wants to check their calendar first". If you have not asked for
                a time yet, stop and ask before using this tool.
        """
        room_name, caller = _call_identity()

        if not (name or "").strip():
            return "Do not save yet - ask the caller for their name, then call this tool again."
        if not (why_no_time or "").strip():
            return (
                "Do not save yet - ask the caller what day and time suits them "
                "first. Use book_callback if they give you one."
            )

        append_lead(
            self.settings.storage.leads_file,
            kind="message",
            name=name,
            contact=caller,
            reason=reason,
            raw_request=why_no_time,
            caller_number=caller,
            room=room_name,
        )
        return f"Noted. Someone will get back to {name} shortly."


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
