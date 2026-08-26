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
from voice_agent.config import Settings
from voice_agent.observability import attach_metrics_logging, log_session_summary
from voice_agent.providers import build_vad
from voice_agent.session import build_session
from voice_agent.storage import save_transcript
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


def _is_outbound(ctx: JobContext) -> bool:
    """Outbound calls carry direction in the dispatch metadata (see make_call.py).

    Never raises: a malformed metadata string must not stop a call, it just
    means we greet as if the caller dialled in.
    """
    raw = getattr(ctx.job, "metadata", "") or ""
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return json.loads(raw).get("direction") == "outbound"
    except (ValueError, AttributeError):
        logger.warning("could not parse job metadata: %r", raw)
        return False


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    settings = Settings.load()
    outbound = _is_outbound(ctx)

    # Reuse the VAD loaded in setup() so the model load stays off the critical
    # path of this call.
    session = build_session(settings, vad=ctx.proc.userdata.get("vad"))
    if settings.log_metrics:
        attach_metrics_logging(session)

    started_at = time.monotonic()

    await session.start(
        ReceptionistAgent(settings=settings, outbound=outbound), room=ctx.room
    )
    await ctx.connect()

    async def _on_shutdown() -> None:
        """Hang up the WhatsApp leg, then log usage and save the transcript.

        Runs when the caller hangs up, when the agent ends the call itself, when
        the LiveKit console ends the session, and on Ctrl+C in terminal mode.
        """
        # Deleting the LiveKit room does not end the WhatsApp call: without an
        # explicit disconnect the caller hears silence until LiveKit's 30 second
        # cleanup. Capture the id now, but hang up LAST - see below.
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

        # Hang up only after the bookkeeping above, plus a short grace period.
        # Audio handed to the transport is still in flight to the caller's phone
        # when the session closes; disconnecting immediately clips the agent's
        # closing line. Shutdown callbacks run concurrently (asyncio.gather), so
        # this ordering is the only control we have over when the leg drops.
        if call_id:
            grace = max(0.0, settings.whatsapp.hangup_grace_seconds)
            if grace:
                await asyncio.sleep(grace)
            await disconnect_whatsapp_call(call_id, settings.whatsapp.access_token)

    ctx.add_shutdown_callback(_on_shutdown)

    # Let the model produce the greeting from its own instructions rather than
    # hardcoding a line here, so the opening stays in the prompt where it can be
    # edited without a code change.
    logger.info("call direction: %s", "outbound" if outbound else "inbound")
    await session.generate_reply(
        instructions="Open the call according to your opening instructions."
    )
