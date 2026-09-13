"""Place ONE outbound WhatsApp call with the agent in the room.

A manual end-to-end check of business-initiated WhatsApp calling, which is the
path a reminder call takes. It dry-runs by default and only dials with --yes.

    uv run python scripts/dial_whatsapp_test.py 918169796256 --country IN
    uv run python scripts/dial_whatsapp_test.py 918169796256 --country IN --yes

Reminder mode dials through the real reminder channel, so the agent is told it
is a reminder and which booking it is about:

    uv run python scripts/dial_whatsapp_test.py 918169796256 --reminder 1234
    uv run python scripts/dial_whatsapp_test.py 918169796256 --reminder 1234 --yes

For the callee to hear the agent, all three must hold:
  1. they have granted this business call permission (checked here first)
  2. the webhook Meta posts to handles the outbound `connect` event, which is
     what calls ConnectWhatsAppCall with Meta's SDP answer
  3. an agent registered as LIVEKIT_AGENT_NAME is running, hosted or local

Meta counts every dial against the number's daily call limit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from dotenv import load_dotenv
from livekit.protocol.agent_dispatch import RoomAgentDispatch

from livekit import api


def _require(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"\n{name} is not set in .env\n")
    return value


def _digits(number: str) -> str:
    # LiveKit's connector docs: "Must include the country code without the
    # leading + sign."
    return re.sub(r"\D", "", number)


def check_permission(phone_number_id: str, token: str, user: str, version: str) -> bool:
    """Meta's own answer to whether this business may call this user now."""
    url = f"https://graph.facebook.com/v{version}/{phone_number_id}/call_permissions?" + (
        urllib.parse.urlencode({"user_wa_id": user})
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        body = json.load(urllib.request.urlopen(request, timeout=20))
    except urllib.error.HTTPError as exc:
        print(f"permission check failed: HTTP {exc.code} {exc.read()[:300]!r}")
        return False

    status = (body.get("permission") or {}).get("status")
    start = next((a for a in body.get("actions", []) if a.get("action_name") == "start_call"), {})
    print(f"call permission: {status}, start_call allowed: {start.get('can_perform_action')}")
    return status in {"temporary", "permanent"} and bool(start.get("can_perform_action"))


async def remind(args: argparse.Namespace) -> int:
    """Send one reminder through WhatsAppCallChannel, exactly as a pass would."""
    from voice_agent.config import Settings
    from voice_agent.reminders.channels import WhatsAppCallChannel
    from voice_agent.storage import build_store

    settings = Settings.load()
    store = build_store(settings)
    if store is None:
        print("not dialing: the appointment store is not configured")
        return 1

    appointment = await store.find_by_ref(args.reminder)
    if appointment is None or not appointment.is_active:
        print(f"not dialing: booking {args.reminder} is missing or not active")
        return 1

    to = _digits(args.number)
    if _digits(appointment.patient_number) != to:
        # The agent will read this booking out. Never read one person's
        # appointment to a different number.
        print(f"not dialing: booking {args.reminder} is not for {to}")
        return 1

    print(
        f"reminder for booking {appointment.booking_ref}: {appointment.department} "
        f"at {appointment.slot_utc} for {appointment.patient_name}"
    )
    version = os.environ.get("WHATSAPP_CLOUD_API_VERSION", "25.0")
    if not check_permission(_require("WHATSAPP_PHONE_NUMBER_ID"), _require("WHATSAPP_ACCESS_TOKEN"), to, version):
        print("not dialing: Meta reports no usable call permission for this user")
        return 1

    if not args.yes:
        print("dry run: pass --yes to place the reminder call")
        return 0

    sent = await WhatsAppCallChannel(settings).send(appointment)
    print("reminder call placed" if sent else "the reminder channel reported failure, see the log above")
    return 0 if sent else 1


async def dial(args: argparse.Namespace) -> int:
    phone_number_id = _require("WHATSAPP_PHONE_NUMBER_ID")
    token = _require("WHATSAPP_ACCESS_TOKEN")
    agent_name = _require("LIVEKIT_AGENT_NAME")
    version = os.environ.get("WHATSAPP_CLOUD_API_VERSION", "25.0")
    to = _digits(args.number)
    room = f"whatsapp-test-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    metadata = json.dumps({"direction": "outbound", "purpose": "test", "callee": to})

    print(f"to={to} country={args.country} room={room} agent={agent_name} api=v{version}")

    if not check_permission(phone_number_id, token, to, version):
        print("not dialing: Meta reports no usable call permission for this user")
        return 1

    if not args.yes:
        print("dry run: pass --yes to place the call")
        return 0

    ringing = api.DialWhatsAppCallRequest().ringing_timeout.__class__(seconds=args.ring)
    lkapi = api.LiveKitAPI()
    try:
        response = await lkapi.connector.dial_whatsapp_call(
            api.DialWhatsAppCallRequest(
                whatsapp_phone_number_id=phone_number_id,
                whatsapp_to_phone_number=to,
                whatsapp_api_key=token,
                whatsapp_cloud_api_version=version,
                room_name=room,
                agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)],
                participant_identity=f"wa_{to}",
                participant_name="Test callee",
                participant_metadata=metadata,
                destination_country=args.country,
                ringing_timeout=ringing,
            )
        )
        # So the agent can hang up this call, exactly as a reminder would.
        from voice_agent.reminders.channels import store_call_id_on_room

        await store_call_id_on_room(lkapi, response.room_name, response.whatsapp_call_id)
    except Exception as exc:  # noqa: BLE001 - a manual test reports, it does not hide
        print(f"LiveKit/Meta rejected the dial: {exc}")
        return 1
    finally:
        await lkapi.aclose()

    print(f"dialled: whatsapp_call_id={response.whatsapp_call_id} room={response.room_name}")
    print("the phone should ring now; if it is answered the webhook must connect it")
    return 0


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Place one outbound WhatsApp test call.")
    parser.add_argument("number", help="callee with country code, e.g. 918169796256")
    parser.add_argument("--country", help="two letter destination country, e.g. IN")
    parser.add_argument("--reminder", metavar="REF", help="send a reminder for this booking instead")
    parser.add_argument("--ring", type=int, default=30, help="ringing timeout in seconds")
    parser.add_argument("--yes", action="store_true", help="actually place the call")
    args = parser.parse_args()
    if args.reminder:
        sys.exit(asyncio.run(remind(args)))
    if not args.country:
        parser.error("--country is required unless --reminder is given")
    sys.exit(asyncio.run(dial(args)))


if __name__ == "__main__":
    main()
