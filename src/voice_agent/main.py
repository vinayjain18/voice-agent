"""Agent server definition.

Run it with the LiveKit CLI:

    lk agent console            # laptop mic + speakers, no LiveKit account needed
    lk agent dev                # register a worker, talk from the browser
    lk agent dev --dev          # ... against a local `livekit-server --dev`

`lk` imports this module and looks for a module-level `AgentServer` named
`app`, `server` or `agent`. That is why `server` below is defined at module
scope and not inside a function.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from livekit.agents import AgentServer, JobContext, JobProcess

# Importing the provider package here pulls in every livekit.plugins package at
# module scope, on the main thread. Plugin registration raises if it happens on
# any other thread, and job runners execute in worker threads - so this import
# must stay at module level. See providers/stt.py.
from voice_agent.agents import ReceptionistAgent
from voice_agent.agents.receptionist import opening_line
from voice_agent.config import Settings
from voice_agent.observability import (
    attach_conversation_logging,
    attach_metrics_logging,
    log_session_summary,
)
from voice_agent.observability.summary import infer_outcome, summarise_call
from voice_agent.providers import build_vad
from voice_agent.session import build_session
from voice_agent.storage import build_store, save_transcript
from voice_agent.storage.calls import CallLog, build_record
from voice_agent.whatsapp.disconnect import (
    disconnect_whatsapp_call,
    find_whatsapp_call_id,
)

logger = logging.getLogger("voice_agent")

_settings = Settings.load()


class _SuppressUnusedTurnDetectorWarning(logging.Filter):
    """Silence a warning about a turn detector we never actually use.

    livekit-agents 1.6.10 does:

        turn_handling.get("turn_detection", inference.TurnDetector())

    Python evaluates that default eagerly, so a LiveKit *cloud* turn detector is
    constructed on every session even when we pass an explicit value that
    immediately discards it. Without LiveKit credentials that logs a warning
    with no bearing on this agent.

    Only installed when LiveKit is unconfigured - once credentials exist the
    warning would be meaningful, so we let it through. Narrow on purpose: if the
    message changes, the filter stops matching rather than hiding it forever.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "LIVEKIT_INFERENCE_URL is set but" not in record.getMessage()


if not _settings.livekit.is_configured:
    logging.getLogger("livekit.agents").addFilter(_SuppressUnusedTurnDetectorWarning())


# `agent_name` is deliberately not set: an unnamed worker uses *automatic*
# dispatch and joins every room in the project, which is what the browser Agent
# Console needs. Setting LIVEKIT_AGENT_NAME switches to explicit dispatch, which
# is what SIP telephony will need later - the library reads that env var itself.
server = AgentServer(
    ws_url=_settings.livekit.url,
    api_key=_settings.livekit.api_key,
    api_secret=_settings.livekit.api_secret,
)


def setup(proc: JobProcess) -> None:
    """Per-process warmup, before any call is handled.

    Two jobs: fail loudly on missing credentials while nobody is waiting on the
    line, and load the Silero VAD weights once rather than per call.
    """
    _settings.preflight()
    proc.userdata["vad"] = build_vad()


server.setup_fnc = setup


def _job_metadata(ctx: JobContext) -> dict:
    """Dispatch metadata, or {} if there is none.

    Never raises: a malformed metadata string must not stop a call, it just
    means we greet as if the caller dialled in.
    """
    raw = getattr(ctx.job, "metadata", "") or ""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("could not parse job metadata: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    settings = Settings.load()
    metadata = _job_metadata(ctx)
    outbound = metadata.get("direction") == "outbound"

    # Reuse the VAD loaded in setup() so the model load stays off the critical
    # path of this call.
    session = build_session(settings, vad=ctx.proc.userdata.get("vad"))
    if settings.log_metrics:
        attach_metrics_logging(session)
    if settings.log_transcript:
        attach_conversation_logging(
            session, include_interim=settings.log_interim_transcript
        )

    started_at = time.monotonic()

    store = build_store(settings)
    reminder_for = None
    if metadata.get("purpose") == "reminder" and store is not None:
        reminder_for = await _reminder_appointment(store, metadata.get("booking_ref", ""))

    agent = ReceptionistAgent(
        settings=settings, outbound=outbound, store=store, reminder_for=reminder_for
    )

    # Warm the diary while the caller is still hearing the greeting, so the
    # first check_availability is a lookup rather than a network round trip.
    # Cancelled in _on_shutdown rather than by its own shutdown callback:
    # callbacks run concurrently under gather, and teardown order is the one
    # thing we control (see the note in _on_shutdown).
    prefetch = (
        asyncio.create_task(_warm(agent.cache)) if agent.cache is not None else None
    )

    await session.start(agent, room=ctx.room)
    await ctx.connect()

    async def _on_shutdown() -> None:
        """Log usage, save the transcript, then tear the call down.

        Runs when the caller hangs up, when the agent ends the call itself, when
        the LiveKit console ends the session, and on Ctrl+C in terminal mode.
        """
        if prefetch is not None and not prefetch.done():
            prefetch.cancel()

        # Deleting the LiveKit room does not end the WhatsApp call: without an
        # explicit disconnect the caller hears silence until LiveKit's 30 second
        # cleanup. Read the id while the participant is still there; the actual
        # hangup happens at the end of this callback.
        call_id = find_whatsapp_call_id(ctx.room)

        duration = time.monotonic() - started_at
        cost = log_session_summary(session, duration)

        if settings.storage.save_transcripts:
            save_transcript(
                settings.storage.transcripts_dir,
                room=ctx.room.name,
                history=session.history,
                extra={
                    "duration_seconds": round(duration, 1),
                    "estimated_cost_usd": round(cost.total_usd, 6) if cost else None,
                    "usage": [
                        {
                            "label": item.label,
                            "detail": item.detail,
                            "cost_usd": item.cost_usd,
                        }
                        for item in (cost.items if cost else [])
                    ],
                },
            )

        # Tear the call down only after the bookkeeping above, plus a short
        # grace period. Audio handed to the transport is still in flight to the
        # caller's phone when the session closes, so dropping the leg
        # immediately clips the agent's closing line.
        #
        # EndCallTool used to delete the room itself, from a second shutdown
        # callback. Callbacks all run together under asyncio.gather, so that
        # one raced this: the room went away while the goodbye was still
        # playing, and the WhatsApp disconnect below then had no participant
        # left to act on (404 participant does not exist). It is now built with
        # delete_room=False and the whole teardown happens here, in order.
        grace = max(0.0, settings.whatsapp.hangup_grace_seconds)
        if grace:
            await asyncio.sleep(grace)

        # WhatsApp first: it needs the participant to still exist.
        if call_id:
            await disconnect_whatsapp_call(call_id, settings.whatsapp.access_token)

        # Then the room, which is what disconnects a SIP caller. Harmless for
        # console and browser sessions, where the room is already going away.
        try:
            await ctx.delete_room()
        except Exception:
            logger.debug("could not delete room %s", ctx.room.name, exc_info=True)

        # Last, with the caller already gone: write one line saying why they
        # rang. This makes an LLM call, so it must stay after the teardown
        # rather than in front of it.
        await _log_call(
            settings,
            session=session,
            agent=agent,
            store=store,
            outbound=outbound,
            duration=duration,
            cost=cost,
            room=ctx.room.name,
        )

    ctx.add_shutdown_callback(_on_shutdown)

    # Speak the opening verbatim instead of asking the model to compose one.
    # A generated greeting varied call to call and put an LLM round trip plus
    # TTS in front of the caller's very first second, which is exactly where
    # dead air is least forgivable. The text lives in business/profile.json, so
    # this is still a content change, not a code change.
    #
    # add_to_chat_ctx defaults to True, so the model sees the greeting as its
    # own first turn and does not repeat it.
    logger.info(
        "call direction: %s",
        "reminder" if reminder_for else ("outbound" if outbound else "inbound"),
    )
    await session.say(
        opening_line(
            agent.profile, outbound=outbound, reminder=reminder_for is not None
        )
    )


async def _reminder_appointment(store, booking_ref: str):
    """The appointment this reminder call is about, or None if it has gone.

    A patient who cancelled between the scan and the call being answered should
    not be reminded about an appointment that no longer exists.
    """
    if not booking_ref:
        return None
    try:
        found = await store.find_by_ref(booking_ref)
    except Exception:
        logger.exception("could not load appointment %s for a reminder call", booking_ref)
        return None
    if found is None or not found.is_active:
        logger.info("reminder call for %s but it is no longer booked", booking_ref)
        return None
    return found


async def _warm(cache) -> None:
    try:
        await cache.items(force=True)
    except Exception:
        # A cold cache is not fatal: the tool re-reads and reports its own error.
        logger.warning("could not prefetch the appointment diary", exc_info=True)


async def _log_call(
    settings,
    *,
    session,
    agent,
    store,
    outbound: bool,
    duration: float,
    cost,
    room: str,
) -> None:
    """Record one line saying why this person rang.

    Runs after the call has been torn down, so the LLM round trip it makes
    cannot delay a hangup. Never raises: a missing log entry is a nuisance, an
    exception here would mark the whole job as failed.
    """
    if store is None:
        return
    try:
        summary = await summarise_call(session, session.llm)
        booking = agent.last_booking
        record = build_record(
            direction="outbound" if outbound else "inbound",
            caller_number=agent.caller_number,
            duration_seconds=duration,
            summary=summary,
            outcome=infer_outcome(session.history),
            department=booking.department if booking else "",
            booking_ref=booking.booking_ref if booking else "",
            turns=len(getattr(session.history, "items", []) or []),
            cost_usd=cost.total_usd if cost else None,
            room=room,
        )
        await CallLog(store.client, tab=settings.sheets.calls_tab).append(record)
    except Exception:
        logger.warning("could not log the call summary", exc_info=True)
