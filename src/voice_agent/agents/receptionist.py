"""The first concrete agent: a front-desk receptionist."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import (
    Agent,
    ChatContext,
    ChatMessage,
    RunContext,
    StopResponse,
    function_tool,
    get_job_context,
)

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


# What the model is told to say once end_call fires. It is the last thing the
# caller hears, so it is specified rather than left to "say goodbye": vague
# instructions produced either nothing at all or a four sentence farewell that
# ran past the hangup. One sentence, and direction-aware, because thanking
# someone for calling when we rang them is an obvious tell.
INBOUND_GOODBYE = """Close the call in ONE short sentence. Thank them for
calling and wish them well. Vary the wording rather than saying the same line
every time, for example "Thanks for calling, have a good day.", "Thanks for
calling us, take care.", or "Lovely, thanks for calling. Have a good one."

Say nothing else. Do not ask another question, do not recap the booking, and do
not add a second sentence."""

OUTBOUND_GOODBYE = """Close the call in ONE short sentence. Thank them for their
time and wish them well, for example "Thanks for your time, have a good day."

Never thank them for calling - you called them. Say nothing else, and do not ask
another question."""


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
    variables["language_guidance"] = settings.language.speaking_guidance
    variables["language_examples"] = settings.language.worked_example

    return variables


# Pure hesitation noises. Deliberately does NOT include "hmm", "mm" or "mhm":
# those are often a real answer to a yes/no question, and discarding them would
# leave the caller thinking they had replied.
HESITATION_ONLY = frozenset({"uh", "uhh", "um", "umm", "er", "err", "erm"})


def is_noise_turn(text: str) -> bool:
    """True when a user turn carries no actual speech.

    Deepgram flushes a turn when the VAD hears something the model cannot
    transcribe - a cough, line noise, a half word. That arrives as an empty or
    near-empty transcript, and answering it makes the agent talk into silence.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    # Punctuation or stray marks with no letters or digits anywhere.
    if not any(char.isalnum() for char in stripped):
        return True
    words = [w.strip(".,!?;:'\"").lower() for w in stripped.split()]
    words = [w for w in words if w]
    return bool(words) and all(word in HESITATION_ONLY for word in words)


# Short things a caller says when they are actually done. A closing answer is
# always brief; anything longer is a fresh request wearing a polite hat.
DONE_WORDS = frozenset(
    {
        "no", "nope", "nah", "nahi", "nothing", "none", "bas",
        "bye", "goodbye", "tata", "thanks", "thank", "cheers", "done",
    }
)

DONE_PHRASES = (
    "that's all", "thats all", "that is all", "nothing else", "no thanks",
    "all good", "all set", "i'm good", "im good", "we're good", "were good",
    "that's it", "thats it", "we are good",
)

# Above this, it is a sentence with content in it, not a sign-off. "You can set
# it up today at two PM South African time" is eleven words and must never read
# as permission to hang up.
MAX_CLOSING_ANSWER_WORDS = 8


def caller_sounds_finished(text: str) -> bool:
    """True when the caller's last turn reads as "no, nothing else".

    This is the gate on hanging up. It is deliberately strict: refusing to end a
    finished call costs one extra question, while ending an unfinished one cuts
    the caller off mid-sentence, which is what kept happening.
    """
    lowered = (text or "").lower()
    if any(phrase in lowered for phrase in DONE_PHRASES):
        return True
    words = re.findall(r"[a-z']+", lowered)
    if not words or len(words) > MAX_CLOSING_ANSWER_WORDS:
        return False
    return any(word in DONE_WORDS for word in words)


def last_caller_turn(session: object) -> str:
    """The most recent thing the caller said, or "" if they have not spoken."""
    try:
        for item in reversed(list(session.history.items)):  # type: ignore[attr-defined]
            if getattr(item, "role", None) == "user":
                return (getattr(item, "text_content", "") or "").strip()
    except Exception:
        logger.debug("could not read conversation history", exc_info=True)
    return ""


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
        self._goodbye_instructions = (
            OUTBOUND_GOODBYE if outbound else INBOUND_GOODBYE
        )

        super().__init__(
            instructions=render_prompt(
                "receptionist",
                build_prompt_variables(
                    self.profile, self.settings, outbound=outbound
                ),
            ),
        )

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Drop turns that contain no speech, instead of replying to them.

        Without this the agent answers its own silence. A real call showed the
        cost: the VAD fired, Deepgram returned nothing usable, and the agent
        generated a reply anyway. Because the caller still had not said
        anything, it did it again, and again, stacking four questions and a
        stray "No further response." into one breath before the caller could
        get a word in.

        Raising StopResponse discards the turn cleanly; the library documents
        this as the supported way to skip a generation.
        """
        text = new_message.text_content or ""
        if is_noise_turn(text):
            logger.info("ignoring a user turn with no speech in it: %r", text)
            raise StopResponse()

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
        # Confirm in the caller's words, not ours. The CSV needs India time so a
        # human can act on it; the caller needs to hear the time they actually
        # gave. A caller who said "two PM South African time" and hears "five
        # thirty" back has no idea whether they were understood.
        their_words = (raw_request or "").strip()
        spoken = their_words if their_words else f"{preferred_date} at {preferred_time}"
        return (
            f"Saved. Now say it back to {name} out loud using THEIR OWN words: "
            f"\"{spoken}\". Do not convert it, do not say India time, and do not "
            f"name a weekday if they said today or tomorrow. Then stop and wait "
            f"for them to reply. Do not end the call in this reply."
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


    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Hang up. Only after the caller has said they need nothing else.

        Before calling this you MUST have asked whether there is anything else
        you can help with, AND heard them answer. Asking the question and
        calling this in the same reply is wrong: the caller never gets to
        answer.

        Do not call this straight after saving a booking. Confirm the day and
        time out loud first and let them respond.
        """
        spoken = last_caller_turn(context.session)

        if not caller_sounds_finished(spoken):
            logger.info("refusing to end the call, caller last said: %r", spoken)
            return (
                "Not yet. They have not said they are finished. Do NOT call "
                "end_call again in this reply. Answer what they just said, or "
                "ask once whether there is anything else you can help with, "
                "then stop talking and wait for their answer."
            )

        logger.info("ending the call, caller last said: %r", spoken)

        # Mirrors what EndCallTool does, minus the room deletion: shut the
        # session down once this turn's speech (which includes the goodbye,
        # because a non-realtime LLM reuses the same speech handle for the tool
        # reply) has actually finished playing. main.py's shutdown callback then
        # drains, hangs up the WhatsApp leg and deletes the room, in that order.
        def _shutdown_session(_: object) -> None:
            context.session.shutdown()

        context.speech_handle.add_done_callback(_shutdown_session)

        def _shutdown_job(event: object) -> None:
            job = get_job_context(required=False)
            if job is not None:
                job.shutdown(reason=getattr(getattr(event, "reason", None), "value", "user_initiated"))

        context.session.once("close", _shutdown_job)

        return self._goodbye_instructions


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
