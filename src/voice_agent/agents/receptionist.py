"""The hospital receptionist: answers questions and manages appointments."""

from __future__ import annotations

import json
import logging
import re
import time as clock
from datetime import date, datetime
from typing import Any

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
from voice_agent.scheduling import (
    UTC,
    Department,
    Schedule,
    available_slots,
    hours_for,
    next_available,
    render_hours,
    speak_slot,
)
from voice_agent.storage import (
    Appointment,
    AppointmentStore,
    SlotTaken,
    build_store,
)
from voice_agent.timezones import describe, resolve_timezone

logger = logging.getLogger(__name__)

# Nothing is offered closer than this to now. A patient needs time to travel,
# and a slot starting in four minutes helps nobody.
MIN_NOTICE_MINUTES = 30

# How many free times to read out. More than three is a list, and a list read
# aloud is impossible to hold in your head.
MAX_OFFERS = 3

# How long the cached view of the sheet is trusted before a tool re-reads it.
CACHE_SECONDS = 45.0

# Distinguishes "work the store out from settings" from "there is no store",
# which None alone cannot express.
AUTO_STORE: Any = object()


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

REMINDER_OPENING = """You placed this call to remind a patient about an
appointment. You have ALREADY said this out loud, word for word:

    "{greeting}"

{appointment}

Do not greet again. Once they answer, say which appointment it is and ask
whether they can still make it. If they can, confirm and let them go. If they
cannot, offer to move or cancel it on this call. Keep it short: they did not
ask to be rung."""


INBOUND_GOODBYE = """Close the call in ONE short sentence. Thank them for
calling and wish them well. Vary the wording rather than saying the same line
every time, for example "Thanks for calling, have a good day.", "Thanks for
calling us, take care.", or "Lovely, thanks for calling. Have a good one."

Say nothing else. Do not ask another question, do not recap the appointment, and
do not add a second sentence."""

OUTBOUND_GOODBYE = """Close the call in ONE short sentence. Thank them for their
time and wish them well, for example "Thanks for your time, have a good day."

Never thank them for calling - you called them. Say nothing else, and do not ask
another question."""


def opening_line(
    profile: BusinessProfile, *, outbound: bool = False, reminder: bool = False
) -> str:
    """The exact words spoken as the call connects."""
    if reminder:
        key = "greeting_reminder"
    elif outbound:
        key = "greeting_outbound"
    else:
        key = "greeting_inbound"

    try:
        greeting = str(profile[key]).strip()
    except KeyError:
        greeting = ""
    if greeting:
        return greeting
    return (
        f"Thanks for calling {profile['business_name']}. "
        f"This is {profile['agent_name']}. How can I help you?"
    )


def build_prompt_variables(
    profile: BusinessProfile,
    settings: Settings,
    *,
    outbound: bool = False,
    reminder_for: Appointment | None = None,
) -> dict[str, str]:
    """Business facts plus the things only known at call time."""
    variables = profile.as_prompt_vars()

    schedule = profile.schedule
    # Say times back in the patient's own timezone when we recorded one, which
    # is the whole point of storing it.
    caller_tz = resolve_timezone(reminder_for.caller_timezone) if reminder_for else None
    speak_tz = caller_tz or schedule.tz

    if reminder_for is not None:
        starts = reminder_for.starts_at()
        when = (
            speak_slot(starts, tz=speak_tz, today=datetime.now(speak_tz).date())
            if starts
            else reminder_for.slot_utc
        )
        variables["opening_instructions"] = REMINDER_OPENING.format(
            greeting=opening_line(profile, reminder=True),
            appointment=(
                f"The appointment is {when} ({describe(speak_tz)}), in "
                f"{reminder_for.department or 'the clinic'}, booked under the name "
                f"{reminder_for.patient_name or 'unknown'}, booking number "
                f"{reminder_for.booking_ref}."
            ),
        )
    else:
        template = OUTBOUND_OPENING if outbound else INBOUND_OPENING
        variables["opening_instructions"] = template.format(
            greeting=opening_line(profile, outbound=outbound)
        )

    # The model has no idea what today is. Without this it invents dates when a
    # caller says "next Tuesday", confidently and wrongly.
    now = datetime.now(schedule.tz)
    variables["current_datetime"] = now.strftime("%A %d %B %Y, %I:%M %p")
    variables["current_date"] = now.strftime("%Y-%m-%d")
    variables["current_utc"] = datetime.now(UTC).strftime("%A %d %B %Y, %H:%M UTC")

    # The language profile is the source of truth here, not profile.json,
    # because it is validated against the TTS at startup.
    variables["languages"] = settings.language.spoken_languages
    variables["language_guidance"] = settings.language.speaking_guidance
    variables["language_examples"] = settings.language.worked_example

    return variables


HESITATION_ONLY = frozenset({"uh", "uhh", "um", "umm", "er", "err", "erm"})


def is_noise_turn(text: str) -> bool:
    """True when a user turn carries no actual speech.

    Deepgram flushes a turn when the VAD hears something the model cannot
    transcribe. Answering that makes the agent talk into silence, repeatedly.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not any(char.isalnum() for char in stripped):
        return True
    words = [w.strip(".,!?;:'\"").lower() for w in stripped.split()]
    words = [w for w in words if w]
    return bool(words) and all(word in HESITATION_ONLY for word in words)


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

MAX_CLOSING_ANSWER_WORDS = 8

# A direct instruction to hang up, which is not the same as answering "no" to
# "anything else?". A real call had the caller say "Cut the call." twice and the
# agent answer "Anything else I can help you with?" both times.
HANGUP_PHRASES = (
    "cut the call", "cut call", "end the call", "end call", "hang up",
    "hangup", "disconnect the call", "disconnect me", "drop the call",
    "please cut", "just hang",
)

# "Don't cut the call" means the opposite, and the word before matters more than
# the phrase itself.
NEGATIONS = ("don't", "dont", "do not", "never", "no need to", "not")


def asks_to_hang_up(text: str) -> bool:
    """True when the caller has directly told you to end the call.

    Separate from the "no, nothing else" test because it is an instruction, not
    an answer, and length does not matter: "sorry, could you just cut the call
    please" is still a request to hang up.
    """
    lowered = (text or "").lower()
    return any(
        phrase in lowered and not _negated(lowered, phrase) for phrase in HANGUP_PHRASES
    )


def _negated(text: str, phrase: str) -> bool:
    """True when a negation sits just before the phrase.

    "Don't cut the call" is the opposite instruction, and hanging up on it would
    be the worst possible reading.
    """
    before = text[: text.find(phrase)]
    tail = " ".join(before.split()[-3:])
    return any(word in tail for word in NEGATIONS)


def caller_sounds_finished(text: str) -> bool:
    """True when the caller's last turn reads as "no, nothing else".

    Deliberately strict: refusing to end a finished call costs one extra
    question, while ending an unfinished one cuts the caller off mid-sentence.
    A direct "hang up" is the exception, and bypasses the length limit.
    """
    lowered = (text or "").lower()
    if asks_to_hang_up(lowered):
        return True
    # "No need to end the call" contains a closing word and means the opposite.
    # A negated hangup request vetoes everything below it.
    if any(
        phrase in lowered and _negated(lowered, phrase) for phrase in HANGUP_PHRASES
    ):
        return False
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


class AppointmentCache:
    """A short-lived view of the sheet, so reads during a call are not network calls.

    Availability is checked several times in a normal booking conversation and a
    Sheets round trip lands inside a voice turn. Writes always go straight to the
    sheet and then invalidate this.
    """

    def __init__(self, store: AppointmentStore) -> None:
        self._store = store
        self._items: list[Appointment] = []
        self._loaded_at = 0.0

    async def items(self, *, force: bool = False) -> list[Appointment]:
        if force or clock.monotonic() - self._loaded_at > CACHE_SECONDS:
            self._items = await self._store.all()
            self._loaded_at = clock.monotonic()
        return self._items

    async def taken(self, department: str, *, force: bool = False) -> set[datetime]:
        """Booked instants for one department. Departments never clash."""
        wanted = (department or "").strip().lower()
        moments = (
            item.starts_at()
            for item in await self.items(force=force)
            if item.is_active and item.department.strip().lower() == wanted
        )
        return {moment for moment in moments if moment is not None}

    def invalidate(self) -> None:
        self._loaded_at = 0.0


class ReceptionistAgent(Agent):
    def __init__(
        self,
        profile: BusinessProfile | None = None,
        settings: Settings | None = None,
        *,
        outbound: bool = False,
        store: AppointmentStore | None = AUTO_STORE,
        reminder_for: Appointment | None = None,
    ) -> None:
        self.profile = profile or load_profile()
        self.settings = settings or Settings.load()
        self.outbound = outbound or reminder_for is not None
        self.schedule: Schedule = self.profile.schedule
        self.store = build_store(self.settings) if store is AUTO_STORE else store
        self.cache = AppointmentCache(self.store) if self.store else None
        self.reminder_for = reminder_for
        # Remembered once the caller states a timezone, so later times in the
        # same call are spoken back in theirs rather than the hospital's.
        self.caller_tz = (
            resolve_timezone(reminder_for.caller_timezone) if reminder_for else None
        ) or self.schedule.tz
        # Captured for the call log, which runs after the room is gone and can
        # no longer ask the job context who was on the line.
        self.caller_number = ""
        self.last_booking: Appointment | None = None
        self._goodbye_instructions = (
            OUTBOUND_GOODBYE if self.outbound else INBOUND_GOODBYE
        )

        super().__init__(
            instructions=render_prompt(
                "receptionist",
                build_prompt_variables(
                    self.profile,
                    self.settings,
                    outbound=outbound,
                    reminder_for=reminder_for,
                ),
            ),
        )

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Drop turns that contain no speech, instead of replying to them."""
        text = new_message.text_content or ""
        if is_noise_turn(text):
            logger.info("ignoring a user turn with no speech in it: %r", text)
            raise StopResponse()

    def _resolve_department(self, name: str) -> Department | str:
        """The department the caller means, or what to ask them."""
        department = self.schedule.department(name)
        if department is not None:
            return department
        options = ", ".join(self.schedule.names)
        if not (name or "").strip():
            return (
                f"No department was given. Ask what they need to be seen about, "
                f"then pick from: {options}."
            )
        return (
            f"'{name}' does not match a department. Do not guess. If they told you "
            f"earlier in this call which one they wanted, pass that. Otherwise ask "
            f"what they need to be seen about and pick from: {options}."
        )

    def _resolve_timezone(self, spoken: str):
        """The timezone to read the caller's time in, or what to ask them.

        An unrecognised timezone is never assumed away. Booking someone into the
        wrong offset is invisible until they fail to turn up.
        """
        if not (spoken or "").strip():
            return self.caller_tz
        tz = resolve_timezone(spoken)
        if tz is None:
            return (
                f"'{spoken}' is not a timezone I can place. Do not guess and do "
                f"not assume ours. Ask which city or country they are in, then "
                f"call this again."
            )
        self.caller_tz = tz
        return tz

    def _offers(self, slots: list[datetime], tz) -> str:
        today = datetime.now(tz).date()
        return ", ".join(speak_slot(slot, tz=tz, today=today) for slot in slots)

    def _hours_note(self, department: Department) -> str:
        """The department's opening hours, carried on every availability result.

        "When is the doctor available?" is a question about opening hours, but
        the model reads it as a booking step and calls check_availability. On a
        real call it answered with two slot times five times running while the
        caller kept rephrasing. Shipping the hours alongside the slots means
        either reading of the question can be answered from one tool result.
        """
        return (
            f"{hours_for(self.schedule, department)} Say that if they asked what "
            f"hours we keep or when someone works. If they asked what is free, "
            f"offer two of the times above, not all."
        )

    @function_tool
    async def get_business_hours(self, context: RunContext, department: str = "") -> str:
        """Return opening hours.

        Prefer passing a department. Reading all ten out loud is a wall of text
        nobody can hold in their head.

        Args:
            department: Which department they asked about, if they named one or
                you can tell from what they need. Leave empty only when they
                genuinely asked about the hospital as a whole.
        """
        if (department or "").strip():
            target = self._resolve_department(department)
            if isinstance(target, str):
                return target
            return f"{hours_for(self.schedule, target)} {self.profile['emergency_room']}"
        return (
            f"{render_hours(self.schedule)} {self.profile['emergency_room']} "
            "Do not read all of that out. Ask which department they need and give "
            "just those hours."
        )

    @function_tool
    async def check_availability(
        self,
        context: RunContext,
        department: str,
        day: str = "",
        timezone: str = "",
    ) -> str:
        """Find appointment times that are actually free in a department.

        Call this BEFORE offering any time. Never invent a slot and never
        promise one you have not checked here first.

        Args:
            department: Which department they need, for example "dentistry",
                "eye care" or "family medicine". Work it out from what they say
                they need. Ask if it is not clear; do not guess.
            day: The day to check, as YYYY-MM-DD in the caller's own timezone.
                Leave empty for the soonest times on any day.
            timezone: The caller's timezone, only if they have said one, for
                example "IST", "Pacific time" or "South African time". Leave
                empty to use the one already established on this call.
        """
        if self.cache is None:
            return _no_store()

        target = self._resolve_department(department)
        if isinstance(target, str):
            return target

        tz = self._resolve_timezone(timezone)
        if isinstance(tz, str):
            return tz

        try:
            taken = await self.cache.taken(target.name)
        except Exception:
            logger.exception("could not read availability")
            return (
                "The schedule did not load. Apologise, say you cannot see the "
                "appointment book right now, and offer to have the desk call "
                "them straight back. Do not guess at any times."
            )

        now = datetime.now(UTC)
        today = now.astimezone(tz).date()

        if day.strip():
            wanted = _parse_date(day)
            if wanted is None:
                return f"'{day}' is not a date. Work out YYYY-MM-DD and call this again."
            if wanted < today:
                return (
                    f"{day} has already gone. Do not book it. Assume they meant the "
                    "next one and check which date they mean."
                )
            if wanted > self.schedule.horizon_end(today):
                return (
                    f"We only book {self.schedule.booking_horizon_days} days ahead. "
                    "Tell them that warmly and offer something sooner."
                )

            free = available_slots(
                self.schedule,
                target,
                wanted,
                taken,
                now=now,
                min_notice_minutes=MIN_NOTICE_MINUTES,
            )
            if not free:
                fallback = next_available(
                    self.schedule,
                    target,
                    taken,
                    now=now,
                    limit=MAX_OFFERS,
                    min_notice_minutes=MIN_NOTICE_MINUTES,
                )
                if not fallback:
                    return (
                        f"{target.name} has nothing free at all in the next few weeks. "
                        "Offer to have the desk call them back."
                    )
                return (
                    f"{target.name} is full on {wanted.strftime('%A %d %B')}. Say so, "
                    f"then offer these instead: {self._offers(fallback, tz)}. "
                    f"{self._hours_note(target)}"
                )
            return (
                f"{target.name} on {wanted.strftime('%A %d %B')}: "
                f"{self._offers(free[:MAX_OFFERS], tz)}. {self._hours_note(target)}"
            )

        soonest = next_available(
            self.schedule,
            target,
            taken,
            now=now,
            limit=MAX_OFFERS,
            min_notice_minutes=MIN_NOTICE_MINUTES,
        )
        if not soonest:
            return (
                f"{target.name} has nothing free in the next few weeks. Offer to "
                "have the desk call them back."
            )
        return (
            f"Soonest free in {target.name}: {self._offers(soonest, tz)}. "
            f"{self._hours_note(target)}"
        )

    @function_tool
    async def book_appointment(
        self,
        context: RunContext,
        name: str,
        department: str,
        date: str,
        time: str,
        timezone: str = "",
        reason: str = "",
        raw_request: str = "",
    ) -> str:
        """Book an appointment in a department at a specific date and time.

        Call this once you have the caller's name, the department, a day AND a
        time, and only after check_availability confirmed that time is free.

        Never ask for a phone number or an email. We already have the caller's
        number from the call itself and it is recorded automatically.

        Args:
            name: The patient's name.
            department: Which department, for example "dentistry" or "eye care".
            date: The date as YYYY-MM-DD, in the caller's own timezone.
            time: 24-hour HH:MM in the caller's own timezone, e.g. "17:30".
            timezone: The caller's timezone if they stated one, for example
                "IST" or "Pacific time". Leave empty to use the one already
                established on this call. Never guess one.
            reason: One short line on what they need to be seen about. Never a
                diagnosis, just what they told you.
            raw_request: What the caller actually said about timing, e.g.
                "tomorrow evening", so you can say it back in their own words.
        """
        if self.store is None or self.cache is None:
            return _no_store()

        if not (name or "").strip():
            return "Do not save yet - ask the caller for their name, then call this tool again."
        if not (time or "").strip():
            return (
                "Do not save yet - no time was given. Ask what time of day suits "
                "them, then call this tool again."
            )

        target = self._resolve_department(department)
        if isinstance(target, str):
            return target

        tz = self._resolve_timezone(timezone)
        if isinstance(tz, str):
            return tz

        slot = _parse_slot(date, time, tz)
        if slot is None:
            return f"'{date} {time}' is not a date and time. Work them out and call this again."

        now = datetime.now(UTC)
        if slot <= now:
            return "That time has already passed. Do not book it. Offer the next free slot instead."
        if not self.schedule.is_valid_slot(target, slot):
            return (
                f"{time} is not one of the appointment times in {target.name}. Call "
                "check_availability and offer a time it gives you."
            )

        room_name, caller = _call_identity()
        self.caller_number = caller or self.caller_number

        try:
            appointment = await self.store.create(
                patient_name=name,
                patient_number=caller,
                department=target.name,
                slot=slot,
                slot_local=speak_slot(slot, tz=self.schedule.tz),
                caller_timezone=str(tz),
                reason=reason,
                raw_request=raw_request,
                room=room_name,
            )
        except SlotTaken:
            self.cache.invalidate()
            return (
                "Someone just took that slot. Apologise briefly, call "
                "check_availability again and offer what is actually free."
            )
        except Exception:
            logger.exception("could not save the appointment")
            return (
                "The appointment did NOT save. Do not tell them it is booked. "
                "Apologise, say the system is not letting you book right now, and "
                "say the desk will call them straight back to confirm."
            )

        self.cache.invalidate()
        self.last_booking = appointment
        spoken = (raw_request or "").strip() or speak_slot(
            slot, tz=tz, today=datetime.now(tz).date()
        )
        digits = " ".join(appointment.booking_ref)
        return (
            f"Saved with {target.name}. Now say it back to them out loud using "
            f'THEIR OWN words: "{spoken}". Do not convert it to another timezone '
            f"and do not name a weekday if they said today or tomorrow. Then read "
            f"the booking number once, slowly, as digits: {digits}. Tell them they "
            f"can use that or just this phone number to change it. Then stop and "
            f"wait. Do not end the call in this reply."
        )

    @function_tool
    async def find_my_appointment(self, context: RunContext) -> str:
        """Look up the caller's upcoming appointments from the number they rang on.

        Use this first whenever someone wants to check, move or cancel an
        appointment. Do not ask for their number: this uses it automatically.
        """
        if self.store is None:
            return _no_store()

        _, caller = _call_identity()
        if not caller:
            return (
                "There is no caller number on this call, so nothing can be looked "
                "up. Ask for the four digit booking number instead."
            )

        try:
            found = await self.store.find_by_number(caller)
        except Exception:
            logger.exception("could not look up appointments")
            return "The appointment book did not load. Apologise and offer to have the desk call back."

        if not found:
            return (
                "Nothing is booked on this number. Say so gently and ask for the "
                "four digit booking number, or offer to book them in."
            )

        lines = [self._describe(item) for item in found if item.starts_at()]
        if len(lines) == 1:
            return f"One appointment: {lines[0]}. Say it back and ask if that is the one."
        return (
            f"{len(lines)} appointments: {'; '.join(lines)}. Ask which one they mean "
            "before doing anything."
        )

    def _describe(self, item: Appointment) -> str:
        tz = resolve_timezone(item.caller_timezone) or self.caller_tz
        when = speak_slot(item.starts_at(), tz=tz, today=datetime.now(tz).date())
        return f"{item.booking_ref}: {item.department} {when} for {item.patient_name}"

    @function_tool
    async def cancel_appointment(self, context: RunContext, booking_ref: str = "") -> str:
        """Cancel an appointment.

        Only call this once you have said which appointment you mean and the
        caller has confirmed it. Cancelling the wrong one means someone turns up
        to nothing.

        Args:
            booking_ref: The four digit booking number, if they gave you one.
                Leave empty to use the appointment on the number they called
                from, which only works when there is exactly one.
        """
        if self.store is None:
            return _no_store()

        target = await self._resolve(booking_ref)
        if isinstance(target, str):
            return target

        try:
            await self.store.cancel(target.booking_ref, reason="cancelled by caller")
        except Exception:
            logger.exception("could not cancel %s", target.booking_ref)
            return (
                "The cancellation did NOT save. Do not tell them it is cancelled. "
                "Say the desk will call them back to sort it out."
            )

        if self.cache:
            self.cache.invalidate()
        return (
            f"Cancelled: {self._describe(target)}. Tell them it is cancelled, then "
            "offer once to book another time. Do not end the call in this reply."
        )

    @function_tool
    async def reschedule_appointment(
        self,
        context: RunContext,
        date: str,
        time: str,
        timezone: str = "",
        booking_ref: str = "",
        raw_request: str = "",
    ) -> str:
        """Move an appointment to a new date and time, same department.

        Use this rather than cancelling and booking again: if the new time turns
        out to be gone, the caller keeps the appointment they already had.

        Args:
            date: The new date as YYYY-MM-DD in the caller's own timezone.
            time: The new time as 24-hour HH:MM in the caller's own timezone.
            timezone: The caller's timezone if they stated one. Leave empty to
                use the one already established on this call.
            booking_ref: The four digit booking number, if they gave you one.
                Leave empty to use the appointment on the number they called from.
            raw_request: What they actually said about the new timing.
        """
        if self.store is None or self.cache is None:
            return _no_store()

        target = await self._resolve(booking_ref)
        if isinstance(target, str):
            return target

        department = self.schedule.department(target.department)
        if department is None:
            return (
                f"Booking {target.booking_ref} is in '{target.department}', which is "
                "no longer a department here. Say the desk will call them back."
            )

        tz = self._resolve_timezone(timezone)
        if isinstance(tz, str):
            return tz

        slot = _parse_slot(date, time, tz)
        if slot is None:
            return f"'{date} {time}' is not a date and time. Work them out and call this again."

        now = datetime.now(UTC)
        if slot <= now:
            return "That time has already passed. Offer a later one."
        if not self.schedule.is_valid_slot(department, slot):
            return (
                f"{time} is not one of the appointment times in {department.name}. "
                "Call check_availability and offer a time it gives you."
            )

        # New booking first. If it fails, the original is untouched.
        try:
            await self.store.create(
                patient_name=target.patient_name,
                patient_number=target.patient_number,
                department=department.name,
                slot=slot,
                slot_local=speak_slot(slot, tz=self.schedule.tz),
                caller_timezone=str(tz),
                reason=target.reason,
                raw_request=raw_request or target.raw_request,
                room=target.room,
            )
        except SlotTaken:
            self.cache.invalidate()
            return (
                "That new time was just taken, and their original appointment is "
                "still in place. Say so, then call check_availability and offer "
                "what is free."
            )
        except Exception:
            logger.exception("could not create the replacement appointment")
            return (
                "The move did NOT go through and the original appointment still "
                "stands. Say the desk will call them back."
            )

        try:
            await self.store.cancel(target.booking_ref, reason="moved")
        except Exception:
            # The new one exists, so the patient has a slot. A duplicate is
            # visible in the sheet and a human can clear it; losing the booking
            # entirely would be worse.
            logger.exception("moved but could not cancel %s", target.booking_ref)

        self.cache.invalidate()
        spoken = (raw_request or "").strip() or speak_slot(
            slot, tz=tz, today=datetime.now(tz).date()
        )
        return (
            f'Moved. Tell them it is now "{spoken}", in their own words. Their '
            f"booking number stays the same as far as they are concerned, so do "
            f"not read out a new one. Then stop and wait."
        )

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Hang up. Only after the caller has said they need nothing else.

        Before calling this you MUST have asked whether there is anything else
        you can help with, AND heard them answer. Asking the question and
        calling this in the same reply is wrong: the caller never gets to
        answer.

        Do not call this straight after saving or cancelling an appointment.
        Confirm it out loud first and let them respond.
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

        def _shutdown_session(_: object) -> None:
            context.session.shutdown()

        context.speech_handle.add_done_callback(_shutdown_session)

        def _shutdown_job(event: object) -> None:
            job = get_job_context(required=False)
            if job is not None:
                job.shutdown(
                    reason=getattr(getattr(event, "reason", None), "value", "user_initiated")
                )

        context.session.once("close", _shutdown_job)

        return self._goodbye_instructions

    async def _resolve(self, booking_ref: str) -> Appointment | str:
        """Find the appointment a tool should act on, or say what to ask for."""
        assert self.store is not None

        ref = (booking_ref or "").strip()
        if ref:
            try:
                found = await self.store.find_by_ref(ref)
            except Exception:
                logger.exception("could not look up %s", ref)
                return "The appointment book did not load. Offer to have the desk call back."
            if found is None:
                return (
                    f"No appointment has the number {ref}. Read it back to check you "
                    "heard it right, or look it up from their phone number instead."
                )
            if not found.is_active:
                return f"Booking {ref} is already cancelled. Tell them, and offer to book a new one."
            return found

        _, caller = _call_identity()
        if not caller:
            return (
                "There is no caller number and no booking number, so there is "
                "nothing to act on. Ask for the four digit booking number."
            )

        try:
            found = await self.store.find_by_number(caller)
        except Exception:
            logger.exception("could not look up appointments for the caller")
            return "The appointment book did not load. Offer to have the desk call back."

        if not found:
            return (
                "Nothing is booked on this number. Say so gently and ask for the "
                "four digit booking number."
            )
        if len(found) > 1:
            options = "; ".join(self._describe(item) for item in found if item.starts_at())
            return (
                f"There is more than one: {options}. Ask which one they mean before "
                "changing anything."
            )
        return found[0]


def _no_store() -> str:
    logger.error("appointment store is not configured")
    return (
        "The appointment system is not available. Do not pretend to book or "
        "cancel anything. Apologise and say the desk will call them straight back."
    )


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        return None


def _parse_slot(day: str, at: str, tz) -> datetime | None:
    """Read a wall-clock date and time as being in `tz`, and return it in UTC."""
    try:
        return (
            datetime.strptime(f"{day.strip()} {at.strip()}", "%Y-%m-%d %H:%M")
            .replace(tzinfo=tz)
            .astimezone(UTC)
        )
    except ValueError:
        return None


def _call_identity() -> tuple[str, str]:
    """Best-effort room name and caller number for the current call.

    The SDK exposes no constant for the SIP caller attribute, so rather than
    hardcode a key that may not exist we scan the participant's attributes for
    anything phone-shaped and fall back to its identity. Outbound calls carry
    the number in the job metadata instead, because we chose it.
    """
    ctx = get_job_context(required=False)
    if ctx is None:
        return "", ""

    room = getattr(ctx.room, "name", "") or ""

    metadata = getattr(ctx.job, "metadata", "") or ""
    if isinstance(metadata, str) and metadata:
        try:
            callee = json.loads(metadata).get("callee", "")
            if callee:
                return room, str(callee)
        except (ValueError, AttributeError):
            pass

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
