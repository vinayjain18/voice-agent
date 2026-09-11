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
import uuid
from typing import Protocol

from livekit.protocol.agent_dispatch import RoomAgentDispatch

from livekit import api
from voice_agent.config import Settings
from voice_agent.storage import Appointment

logger = logging.getLogger(__name__)


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

        if not appointment.patient_number:
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
                    whatsapp_to_phone_number=appointment.patient_number,
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
                    destination_country=reminders.destination_country,
                    ringing_timeout=_seconds(reminders.ringing_timeout_seconds),
                )
            )
            logger.info(
                "reminder call placed for %s: call_id=%s room=%s",
                appointment.booking_ref,
                getattr(response, "whatsapp_call_id", ""),
                getattr(response, "room_name", room),
            )
            return True
        except Exception:
            logger.exception("could not place a reminder call for %s", appointment.booking_ref)
            return False
        finally:
            await lkapi.aclose()


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
