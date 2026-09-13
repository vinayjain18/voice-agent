"""Meta WhatsApp call webhook.

Meta posts a "call connect" event here when someone calls the business number.
We hand the SDP offer to LiveKit, which bridges the call into a room and
dispatches the agent. The agent itself is unchanged: a WhatsApp call is just
another participant in a normal LiveKit room.

Run it:

    uv run python -m voice_agent.whatsapp          # listens on :8000
    ngrok http 8000                                # public https URL for Meta
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

from fastapi import FastAPI, Request, Response
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.rtc import SessionDescription

from livekit import api
from voice_agent.config import Settings
from voice_agent.reminders import run_once
from voice_agent.whatsapp.disconnect import CALL_ID_ATTRIBUTE
from voice_agent.whatsapp.payload import WhatsAppCallEvent, parse_call_events

logger = logging.getLogger("voice_agent.whatsapp")

app = FastAPI(title="WhatsApp call webhook")


def _settings() -> Settings:
    # Loaded per request so a token can be rotated without a restart. Meta's
    # temporary tokens expire after 24 hours, which makes this genuinely useful.
    return Settings.load()


@app.get("/health")
async def health() -> dict[str, object]:
    wa = _settings().whatsapp
    return {"ok": True, "whatsapp_configured": wa.is_configured}


@app.get("/webhook")
async def verify(request: Request) -> Response:
    """Meta's one-time verification handshake.

    Meta sends the verify token you typed into its console. Echo the challenge
    back only if it matches, otherwise anyone could claim this endpoint.
    """
    wa = _settings().whatsapp
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and wa.verify_token and token == wa.verify_token:
        logger.info("webhook verified by Meta")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("webhook verification failed (mode=%r, token matched=%s)", mode, token == wa.verify_token)
    return Response(status_code=403, content="verification failed")


@app.post("/webhook")
async def receive(request: Request) -> Response:
    """Handle call events.

    Always returns 200 once the signature checks out. Meta retries on non-2xx,
    and a retry cannot help with a call that has already failed - it would just
    produce duplicate attempts.
    """
    settings = _settings()
    wa = settings.whatsapp
    raw = await request.body()

    if not _signature_ok(wa.app_secret, raw, request.headers.get("x-hub-signature-256")):
        logger.warning("rejected webhook with a bad signature")
        return Response(status_code=403, content="bad signature")

    try:
        body = json.loads(raw)
    except ValueError:
        logger.warning("webhook body was not JSON: %r", raw[:400])
        return Response(status_code=200, content="ignored")

    events = parse_call_events(body)
    if not events:
        # Not an error: terminate/status events land here too. Log the raw body
        # so an unexpected shape is visible rather than silently dropped.
        logger.info("no actionable call event in payload: %s", json.dumps(body)[:800])
        return Response(status_code=200, content="ignored")

    for event in events:
        if event.is_inbound_connect:
            await _accept(settings, event)
        elif event.is_outbound_connect:
            await _connect(event)
        elif event.event == "terminate":
            # The caller hung up. Telling LiveKit promptly frees the room and
            # stops the agent running for LiveKit's 30 second grace period.
            await _release(settings, event)
        else:
            logger.info(
                "ignoring call event id=%s event=%r sdp_type=%r",
                event.call_id, event.event, event.sdp_type,
            )

    return Response(status_code=200, content="ok")


async def _accept(settings: Settings, event: WhatsAppCallEvent) -> None:
    """Bridge one inbound call into a LiveKit room with the agent in it."""
    wa = settings.whatsapp
    if not wa.is_configured:
        logger.error(
            "cannot accept call %s: set WHATSAPP_PHONE_NUMBER_ID, "
            "WHATSAPP_ACCESS_TOKEN and WHATSAPP_VERIFY_TOKEN",
            event.call_id,
        )
        return

    room = f"{wa.room_prefix}-{uuid.uuid4().hex[:8]}"
    lkapi = api.LiveKitAPI()
    try:
        logger.info(
            "accepting WhatsApp call id=%s from=%s -> room=%s agent=%s",
            event.call_id, event.caller or "unknown", room, wa.agent_name,
        )
        await lkapi.connector.accept_whatsapp_call(
            api.AcceptWhatsAppCallRequest(
                whatsapp_phone_number_id=wa.phone_number_id,
                whatsapp_api_key=wa.access_token,
                whatsapp_cloud_api_version=wa.cloud_api_version,
                whatsapp_call_id=event.call_id,
                # The field is a livekit.SessionDescription message, not a raw
                # string. Meta sends an SDP offer for inbound calls.
                sdp=SessionDescription(type=event.sdp_type or "offer", sdp=event.sdp),
                room_name=room,
                agents=[RoomAgentDispatch(agent_name=wa.agent_name)],
                # The agent reads this on shutdown so it can hang up the
                # WhatsApp leg, not just leave the LiveKit room.
                participant_attributes={CALL_ID_ATTRIBUTE: event.call_id},
                wait_until_answered=wa.wait_until_answered,
            )
        )
        logger.info("call %s connected to room %s", event.call_id, room)
    except Exception:
        # A failure here means the caller hears nothing. Log loudly; never let
        # it bubble up and turn into a retry storm from Meta.
        logger.exception("failed to accept WhatsApp call %s", event.call_id)
    finally:
        await lkapi.aclose()


async def _connect(event: WhatsAppCallEvent) -> None:
    """Finish a call the business placed by handing Meta's SDP answer to LiveKit.

    DialWhatsAppCall only starts the call. When the callee picks up, Meta posts a
    connect event carrying an SDP answer, and LiveKit's docs say
    ConnectWhatsAppCall must be called with it immediately, or the callee hears
    silence and the call drops. This webhook used to log that event as ignored,
    so no outbound call, reminders included, could ever connect.
    """
    lkapi = api.LiveKitAPI()
    try:
        logger.info("connecting outbound WhatsApp call id=%s", event.call_id)
        await lkapi.connector.connect_whatsapp_call(
            api.ConnectWhatsAppCallRequest(
                whatsapp_call_id=event.call_id,
                # A SessionDescription message, not a raw string (constraint 15).
                sdp=SessionDescription(type=event.sdp_type or "answer", sdp=event.sdp),
            )
        )
        logger.info("outbound WhatsApp call %s connected", event.call_id)
    except Exception:
        # Never let this bubble up: Meta would retry the webhook, and the callee
        # is already on the line either way.
        logger.exception("failed to connect outbound WhatsApp call %s", event.call_id)
    finally:
        await lkapi.aclose()


async def _release(settings: Settings, event: WhatsAppCallEvent) -> None:
    """Tell LiveKit the caller hung up, so it tears the room down immediately.

    LiveKit requires the Meta access token for this disconnect too, and rejects
    the request without it: `whatsapp api key is required`.
    """
    token = settings.whatsapp.access_token
    if not token:
        logger.warning(
            "cannot release WhatsApp call %s: WHATSAPP_ACCESS_TOKEN is not set, "
            "so the room stays up until LiveKit's 30 second cleanup",
            event.call_id,
        )
        return

    lkapi = api.LiveKitAPI()
    try:
        await lkapi.connector.disconnect_whatsapp_call(
            api.DisconnectWhatsAppCallRequest(
                whatsapp_call_id=event.call_id,
                whatsapp_api_key=token,
                disconnect_reason=api.DisconnectWhatsAppCallRequest.USER_INITIATED,
            )
        )
        logger.info("released WhatsApp call %s after user hangup", event.call_id)
    except Exception as exc:  # noqa: BLE001
        # Meta also sends terminate when the BUSINESS hangs up, and the agent has
        # already disconnected that call. A second disconnect is an expected
        # error here, not a failure worth a stack trace.
        logger.info("disconnect for %s not needed: %s", event.call_id, exc)
    finally:
        await lkapi.aclose()


def _signature_ok(app_secret: str | None, raw: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256.

    Skipped when WHATSAPP_APP_SECRET is unset, which is fine behind an ngrok URL
    during testing but should be set before this is exposed publicly: without
    it, anyone who finds the URL can trigger calls on your account.
    """
    if not app_secret:
        logger.warning("WHATSAPP_APP_SECRET not set - webhook signature NOT verified")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _reminder_secret_ok(expected: str, request: Request) -> bool:
    """Guard the trigger endpoint. It sits on a public URL."""
    supplied = request.headers.get("x-reminder-secret", "")
    if not supplied:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
    return bool(supplied) and hmac.compare_digest(expected, supplied)


@app.post("/tasks/reminders")
async def reminders(request: Request) -> Response:
    """Run one reminder pass. Called on a timer, see deploy/appsscript/.

    Safe to call more often than needed and safe to call twice at once: a row is
    claimed before it is dialled, so the second pass skips it.
    """
    expected = _settings().reminders.trigger_secret or ""
    if not expected:
        logger.error("REMINDER_TRIGGER_SECRET is not set, refusing to run unguarded")
        return Response(status_code=503, content="reminder trigger is not configured")

    if not _reminder_secret_ok(expected, request):
        logger.warning("rejected a reminder trigger with a bad secret")
        return Response(status_code=403, content="bad secret")

    result = await run_once()
    return Response(
        status_code=200 if not result.error else 500,
        content=json.dumps(result.as_dict()),
        media_type="application/json",
    )
