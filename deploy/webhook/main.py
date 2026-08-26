"""Vercel entrypoint for the WhatsApp call webhook.

Vercel's Python runtime looks for an ASGI app named `app` in main.py, so the
FastAPI instance is exported from here directly.

Environment variables required (set in the Vercel project settings):
    WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_VERIFY_TOKEN,
    WHATSAPP_APP_SECRET, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
    LIVEKIT_AGENT_NAME
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid

from fastapi import FastAPI, Request, Response
from livekit.protocol.agent_dispatch import RoomAgentDispatch
from livekit.protocol.rtc import SessionDescription
from wa.payload import WhatsAppCallEvent, parse_call_events

from livekit import api

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wa.webhook")

app = FastAPI(title="WhatsApp call webhook")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "whatsapp_configured": bool(
            _env("WHATSAPP_PHONE_NUMBER_ID")
            and _env("WHATSAPP_ACCESS_TOKEN")
            and _env("WHATSAPP_VERIFY_TOKEN")
        ),
        "signature_verification": bool(_env("WHATSAPP_APP_SECRET")),
    }


@app.get("/webhook")
async def verify(request: Request) -> Response:
    """Meta's one-time verification handshake."""
    params = request.query_params
    expected = _env("WHATSAPP_VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and expected and params.get("hub.verify_token") == expected:
        logger.info("webhook verified by Meta")
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    logger.warning("webhook verification failed")
    return Response(status_code=403, content="verification failed")


@app.post("/webhook")
async def receive(request: Request) -> Response:
    raw = await request.body()

    if not _signature_ok(_env("WHATSAPP_APP_SECRET"), raw, request.headers.get("x-hub-signature-256")):
        logger.warning("rejected webhook with a bad signature")
        return Response(status_code=403, content="bad signature")

    try:
        body = json.loads(raw)
    except ValueError:
        logger.warning("webhook body was not JSON")
        return Response(status_code=200, content="ignored")

    events = parse_call_events(body)
    if not events:
        logger.info("no actionable call event: %s", json.dumps(body)[:600])
        return Response(status_code=200, content="ignored")

    for event in events:
        if event.is_inbound_connect:
            await _accept(event)
        elif event.event == "terminate":
            await _release(event)
        else:
            logger.info("ignoring event=%r sdp_type=%r", event.event, event.sdp_type)

    return Response(status_code=200, content="ok")


async def _accept(event: WhatsAppCallEvent) -> None:
    room = f"{_env('WHATSAPP_ROOM_PREFIX', 'whatsapp')}-{uuid.uuid4().hex[:8]}"
    agent_name = _env("LIVEKIT_AGENT_NAME", "voice-agent-demo")
    # Serverless functions have an execution limit, and blocking until the agent
    # joins can exceed it on a cold start. Default to not waiting here.
    wait = _env("WHATSAPP_WAIT_UNTIL_ANSWERED", "false").lower() in {"1", "true", "yes"}

    lkapi = api.LiveKitAPI()
    try:
        logger.info("accepting call id=%s from=%s -> room=%s", event.call_id, event.caller, room)
        await lkapi.connector.accept_whatsapp_call(
            api.AcceptWhatsAppCallRequest(
                whatsapp_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
                whatsapp_api_key=_env("WHATSAPP_ACCESS_TOKEN"),
                whatsapp_cloud_api_version=_env("WHATSAPP_CLOUD_API_VERSION", "25.0"),
                whatsapp_call_id=event.call_id,
                # The field is a livekit.SessionDescription message, not a string.
                sdp=SessionDescription(type=event.sdp_type or "offer", sdp=event.sdp),
                room_name=room,
                agents=[RoomAgentDispatch(agent_name=agent_name)],
                # The agent reads this on shutdown to hang up the WhatsApp leg,
                # not just leave the LiveKit room.
                participant_attributes={"whatsapp_call_id": event.call_id},
                wait_until_answered=wait,
            )
        )
        logger.info("call %s connected to room %s", event.call_id, room)
    except Exception:
        logger.exception("failed to accept WhatsApp call %s", event.call_id)
    finally:
        await lkapi.aclose()


async def _release(event: WhatsAppCallEvent) -> None:
    """User hung up: free the LiveKit room instead of waiting out the 30s cleanup."""
    lkapi = api.LiveKitAPI()
    try:
        await lkapi.connector.disconnect_whatsapp_call(
            api.DisconnectWhatsAppCallRequest(
                whatsapp_call_id=event.call_id,
                disconnect_reason=api.DisconnectWhatsAppCallRequest.USER_INITIATED,
            )
        )
        logger.info("released call %s after user hangup", event.call_id)
    except Exception as exc:  # noqa: BLE001 - see comment below
        # Meta also sends terminate when the business hangs up, which the agent
        # has already disconnected. A second disconnect errors; that is expected.
        logger.info("disconnect for %s not needed: %s", event.call_id, exc)
    finally:
        await lkapi.aclose()


def _signature_ok(app_secret: str, raw: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256. Required on a public URL."""
    if not app_secret:
        logger.warning("WHATSAPP_APP_SECRET not set - signature NOT verified")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))
