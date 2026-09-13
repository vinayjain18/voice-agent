"""How a reminder actually reaches the patient.

One protocol, two implementations. `dry_run` is the default because a live
WhatsApp call needs a business number outside the US, Canada, Egypt, Vietnam and
Nigeria: Meta excludes those from business-initiated calling, and the test
number is a US one. Everything up to the dial is exercised either way, so
switching REMINDER_CHANNEL is the only change needed once a number exists.

Permission is not a problem for the normal case. A patient who rang the hospital
to book has, by placing that call, granted temporary call permission for seven
days, so an appointment booked within a week of the call is already covered.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Protocol

from livekit.protocol.agent_dispatch import RoomAgentDispatch

from livekit import api
from voice_agent.config import Settings
from voice_agent.storage import Appointment

logger = logging.getLogger(__name__)

# The key the agent reads at shutdown to hang up the WhatsApp leg. It must match
# voice_agent.whatsapp.disconnect.CALL_ID_ATTRIBUTE, which this module cannot
# import: the Vercel bundle has no whatsapp package. A test pins the two together.
CALL_ID_KEY = "whatsapp_call_id"


class ReminderChannel(Protocol):
    name: str

    async def send(self, appointment: Appointment) -> bool:
        """Deliver the reminder. True if it went out."""
        ...


class DryRunChannel:
    """Logs the call it would have placed, and reports success."""

    name = "dry_run"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, appointment: Appointment) -> bool:
        logger.info(
            "DRY RUN reminder: would call %s about %s for %s at %s UTC (booking %s)",
            appointment.patient_number or "(no number)",
            appointment.patient_name or "(no name)",
            appointment.department or "(no department)",
            appointment.slot_utc,
            appointment.booking_ref,
        )
        return True


class WhatsAppCallChannel:
    """Places a business-initiated WhatsApp call with the agent already in the room."""

    name = "whatsapp_call"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, appointment: Appointment) -> bool:
        whatsapp = self._settings.whatsapp
        reminders = self._settings.reminders

        to = _dial_digits(appointment.patient_number)
        if not to:
            logger.warning("no number on booking %s, cannot call", appointment.booking_ref)
            return False
        if not (whatsapp.phone_number_id and whatsapp.access_token):
            logger.error("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN are required")
            return False

        room = f"reminder-{appointment.booking_ref}-{uuid.uuid4().hex[:6]}"
        metadata = json.dumps(
            {
                "direction": "outbound",
                "purpose": "reminder",
                "booking_ref": appointment.booking_ref,
                "callee": appointment.patient_number,
            }
        )

        lkapi = api.LiveKitAPI()
        try:
            response = await lkapi.connector.dial_whatsapp_call(
                api.DialWhatsAppCallRequest(
                    whatsapp_phone_number_id=whatsapp.phone_number_id,
                    whatsapp_to_phone_number=to,
                    whatsapp_api_key=whatsapp.access_token,
                    whatsapp_cloud_api_version=whatsapp.cloud_api_version,
                    room_name=room,
                    agents=[
                        RoomAgentDispatch(
                            agent_name=whatsapp.agent_name, metadata=metadata
                        )
                    ],
                    participant_identity=f"wa_{appointment.booking_ref}",
                    participant_name=appointment.patient_name or "Patient",
                    participant_metadata=metadata,
                    destination_country=reminders.destination_country or _country_for(to),
                    ringing_timeout=_seconds(reminders.ringing_timeout_seconds),
                )
            )
            call_id = getattr(response, "whatsapp_call_id", "") or ""
            room_name = getattr(response, "room_name", "") or room
            logger.info(
                "reminder call placed for %s: call_id=%s room=%s",
                appointment.booking_ref,
                call_id,
                room_name,
            )
            await store_call_id_on_room(lkapi, room_name, call_id)
            return True
        except Exception:
            logger.exception("could not place a reminder call for %s", appointment.booking_ref)
            return False
        finally:
            await lkapi.aclose()


async def store_call_id_on_room(lkapi: api.LiveKitAPI, room: str, call_id: str) -> None:
    """Let the agent hang up a call it placed.

    The agent hangs up the WhatsApp leg with Meta's call id, read at shutdown. An
    outbound call's id only exists once DialWhatsAppCall returns, after the
    participant was created, so it cannot be a participant attribute the way it
    is for inbound calls. On the 2026-09-13 reminder test the agent ended the
    session and Meta's terminate only arrived 24 seconds later.

    Never raises: the patient's phone is already ringing, and reporting the send
    as failed would make the next pass ring them again.
    """
    if not call_id:
        return
    try:
        await lkapi.room.update_room_metadata(
            api.UpdateRoomMetadataRequest(
                room=room, metadata=json.dumps({CALL_ID_KEY: call_id})
            )
        )
    except Exception:
        logger.warning(
            "could not store call id %s on room %s; the agent will not be able "
            "to hang up this call itself",
            call_id,
            room,
            exc_info=True,
        )


def _dial_digits(number: str) -> str:
    """The number as the dial API wants it.

    LiveKit's connector docs: it "Must include the country code without the
    leading + sign". Stored numbers carry a plus and sometimes spaces.
    """
    return re.sub(r"\D", "", number or "")


# Only the prefixes this clinic actually dials. destination_country is optional
# in LiveKit's API, and routing a call through the wrong country is worse than
# leaving it to LiveKit, so an unknown prefix sends nothing.
_COUNTRY_BY_PREFIX = (("91", 12, "IN"), ("1", 11, "US"))


def _country_for(digits: str) -> str:
    """Where the call terminates, derived from the number being dialled.

    It was hardcoded to US. The only outbound WhatsApp call that has actually
    rung, to an Indian number on 2026-09-13, passed IN.
    """
    for prefix, length, country in _COUNTRY_BY_PREFIX:
        if digits.startswith(prefix) and len(digits) == length:
            return country
    return ""


def _seconds(value: int):
    from google.protobuf.duration_pb2 import Duration

    duration = Duration()
    duration.seconds = max(1, int(value))
    return duration


CHANNELS = {
    DryRunChannel.name: DryRunChannel,
    WhatsAppCallChannel.name: WhatsAppCallChannel,
}


def build_channel(settings: Settings) -> ReminderChannel:
    choice = (settings.reminders.channel or "dry_run").lower()
    if choice not in CHANNELS:
        raise ValueError(
            f"REMINDER_CHANNEL must be one of {', '.join(sorted(CHANNELS))}. Got '{choice}'."
        )
    return CHANNELS[choice](settings)
