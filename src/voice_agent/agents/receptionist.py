"""The first concrete agent: a front-desk receptionist."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import Agent, RunContext, function_tool, get_job_context
from livekit.agents.beta.tools import EndCallTool

from voice_agent.business import BusinessProfile, load_profile
from voice_agent.config import Settings
from voice_agent.prompts import render_prompt
from voice_agent.storage import append_lead

logger = logging.getLogger(__name__)

# The business runs on India time, and callers speak in it.
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")


# The greeting is spoken verbatim rather than generated. A model-written opening
# varied on every call, and cost an LLM round trip plus TTS at the one moment a
# caller is least willing to hear silence. These say what has *already* been
# said, so the model does not greet a second time.
INBOUND_OPENING = """The call has just connected and you have ALREADY said this
out loud, word for word:

    "{greeting}"

Do not greet again, do not introduce yourself again, and do not repeat the
question. Say nothing further. Wait for the caller to speak, then answer what
they actually asked."""

OUTBOUND_OPENING = """You placed this call, so they are not expecting you. You
have ALREADY said this out loud, word for word:

    "{greeting}"

Do not greet again and do not repeat the question. Wait for their answer.

If they say it is a good moment, say briefly why you are calling. If they say it
is a bad time, offer to call back and ask when suits them. Do not push."""


def opening_line(profile: BusinessProfile, *, outbound: bool = False) -> str:
    """The exact words spoken as the call connects.

    Content, not code: edit business/profile.json. Falls back to a plain line
    built from the profile so a missing key cannot leave a caller in silence.
    """
    key = "greeting_outbound" if outbound else "greeting_inbound"
    try:
        greeting = str(profile[key]).strip()
    except KeyError:
        greeting = ""
    if greeting:
        return greeting
    return (
        f"Thank you for calling {profile['business_name']}. "
        f"My name is {profile['agent_name']}. How may I help you today?"
    )


def build_prompt_variables(
    profile: BusinessProfile, settings: Settings, *, outbound: bool = False
) -> dict[str, str]:
    """Business facts plus the things only known at call time."""
    variables = profile.as_prompt_vars()
    template = OUTBOUND_OPENING if outbound else INBOUND_OPENING
    variables["opening_instructions"] = template.format(
        greeting=opening_line(profile, outbound=outbound)
    )

    # The model has no idea what today is. Without this it invents dates when a
    # caller says "next Tuesday", confidently and wrongly.
    now = datetime.now(BUSINESS_TZ)
    variables["current_datetime"] = now.strftime("%A %d %B %Y, %I:%M %p")
    variables["current_date"] = now.strftime("%Y-%m-%d")

    # The language profile is the source of truth here, not profile.json,
    # because it is validated against the TTS at startup.
    variables["languages"] = settings.language.spoken_languages

    return variables


END_CALL_CONDITIONS = """
Before calling this, you MUST have asked "is there anything else I can help you
with?" and the caller must have answered no. If you have not asked that yet, do
not call this tool - ask it as a normal reply instead and wait for their answer.

Never call this while the caller is mid-sentence, has just asked a question, or
has gone quiet. Silence is not consent to hang up. If you are unsure whether
they are finished, do not call this tool.
"""


async def _log_end_call(event: object) -> None:
    """Record what the call looked like at the moment the model hung up.

    A caller reporting "it cut me off" is unfalsifiable from the metrics alone:
    an agent-initiated hangup and the caller hanging up produce almost the same
    log tail. Printing the last few turns here makes the difference visible in
    `lk agent logs` without needing the transcript file, which on LiveKit Cloud
    lives on an ephemeral disk.
    """
    try:
        history = event.ctx.session.history  # type: ignore[attr-defined]
        tail = []
        for item in list(history.items)[-6:]:
            role = getattr(item, "role", "?")
            text = (getattr(item, "text_content", "") or "").strip()
            if text:
                tail.append(f"{role}: {text}")
        logger.info("end_call requested by the model | recent turns: %s", " | ".join(tail))
    except Exception:
        logger.warning("end_call requested by the model (could not read history)", exc_info=True)


class ReceptionistAgent(Agent):
    def __init__(
        self,
        profile: BusinessProfile | None = None,
        settings: Settings | None = None,
        *,
        outbound: bool = False,
    ) -> None:
        self.profile = profile or load_profile()
        self.settings = settings or Settings.load()
        self.outbound = outbound

        super().__init__(
            instructions=render_prompt(
                "receptionist",
                build_prompt_variables(
                    self.profile, self.settings, outbound=outbound
                ),
            ),
            tools=[
                EndCallTool(
                    # Conditions live here as well as in the prompt because this
                    # text is in the tool schema, right where the model makes
                    # the decision. Prompt rules many turns back lose to it.
                    extra_description=END_CALL_CONDITIONS,
                    # The model says this closing line first; the session shuts
                    # down only once that speech has finished playing.
                    end_instructions=(
                        "Say a short, warm goodbye. One sentence. Do not ask "
                        "another question."
                    ),
                    # Deliberately False. The library would register its own
                    # shutdown callback to delete the room, and shutdown
                    # callbacks all run together under asyncio.gather - so it
                    # raced ours, tore the room down while the goodbye audio was
                    # still in flight, and left DisconnectWhatsAppCall with no
                    # participant to disconnect (404). main.py now drains, hangs
                    # up the WhatsApp leg, and deletes the room, in that order.
                    delete_room=False,
                    # Without this the model could end the call during its own
                    # greeting, before the caller has said anything.
                    ignore_on_enter=True,
                    on_tool_called=_log_end_call,
                ),
            ],
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
