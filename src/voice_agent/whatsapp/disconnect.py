"""Hang up the WhatsApp leg of a call.

Ending the LiveKit room is not enough: the caller's WhatsApp call stays open
until LiveKit's own 30 second cleanup kicks in, so the person hears silence on a
live call after the agent has said goodbye.

LiveKit's connector docs: "You must call this API for both business-initiated
and user-initiated disconnects... If you don't call DisconnectWhatsAppCall after
a user hangs up, LiveKit automatically cleans up the call after 30 seconds.
During this window, any agents, egress, or other services running in the room
continue to run unnecessarily."
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("voice_agent.whatsapp")

# Attribute name used to carry Meta's call id from the webhook to the agent.
CALL_ID_ATTRIBUTE = "whatsapp_call_id"


def find_whatsapp_call_id(room: Any) -> str | None:
    """Meta's call id for this room, if it is a WhatsApp call.

    Inbound calls carry it as a participant attribute, set by the webhook when it
    accepts the call. An outbound call cannot: its id only exists once
    DialWhatsAppCall returns, after the participant was already created. So the
    dialer writes it into the room's metadata instead, and this falls back to
    that. Without it the agent could not hang up a call it placed, and the
    patient sat in silence until LiveKit's own cleanup.
    """
    try:
        for participant in (room.remote_participants or {}).values():
            attributes = dict(getattr(participant, "attributes", {}) or {})
            call_id = attributes.get(CALL_ID_ATTRIBUTE)
            if call_id:
                return str(call_id)
    except Exception:
        logger.debug("could not read whatsapp call id", exc_info=True)
    return _call_id_from_room_metadata(room)


def _call_id_from_room_metadata(room: Any) -> str | None:
    """The id the dialer stored on the room. Never raises: this runs at shutdown."""
    try:
        parsed = json.loads(getattr(room, "metadata", "") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    call_id = parsed.get(CALL_ID_ATTRIBUTE)
    return call_id if isinstance(call_id, str) and call_id else None


async def disconnect_whatsapp_call(call_id: str, api_key: str | None) -> bool:
    """End the WhatsApp call as a business-initiated disconnect.

    Returns True when the disconnect was accepted. Never raises: this runs during
    shutdown, where an exception helps nobody.

    A BUSINESS_INITIATED disconnect requires the Meta access token; USER_INITIATED
    does not, because no call out to WhatsApp is needed.
    """
    if not api_key:
        logger.warning(
            "cannot hang up WhatsApp call %s: WHATSAPP_ACCESS_TOKEN is not set, "
            "so the caller stays connected until LiveKit's 30s cleanup",
            call_id,
        )
        return False

    from livekit import api

    lkapi = api.LiveKitAPI()
    try:
        await lkapi.connector.disconnect_whatsapp_call(
            api.DisconnectWhatsAppCallRequest(
                whatsapp_call_id=call_id,
                whatsapp_api_key=api_key,
                disconnect_reason=api.DisconnectWhatsAppCallRequest.BUSINESS_INITIATED,
            )
        )
        logger.info("hung up WhatsApp call %s", call_id)
        return True
    except Exception:
        logger.exception("failed to hang up WhatsApp call %s", call_id)
        return False
    finally:
        await lkapi.aclose()
