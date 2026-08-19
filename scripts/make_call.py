"""Place an outbound call: the agent dials a phone number.

    uv run python scripts/make_call.py +918169796256

Flow:
  1. create an agent dispatch so the agent joins the room
  2. ask LiveKit to dial out through the Plivo trunk
  3. when the person answers, the SIP participant lands in the same room

The agent itself needs no changes beyond knowing it placed the call rather than
received one, so it opens by saying who is calling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv

from livekit import api

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("make_call")


def _require(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"\n{name} is not set in .env\n")
    return value


async def place_call(to_number: str, *, trunk_id: str, wait: bool) -> None:
    caller = _require("PLIVO_CALLER_NUMBER")
    agent_name = os.environ.get("LIVEKIT_AGENT_NAME", "").strip()
    if not agent_name:
        raise SystemExit(
            "\nLIVEKIT_AGENT_NAME is not set. Outbound uses explicit dispatch, so "
            "the agent will never join the room without it.\n"
        )

    room = f"outbound-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    lk = api.LiveKitAPI()

    try:
        # The agent must be in the room before the callee answers, or the first
        # thing they hear is silence.
        logger.info("dispatching agent %r to room %s", agent_name, room)
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room,
                metadata=json.dumps({"direction": "outbound", "callee": to_number}),
            )
        )

        logger.info("calling %s from %s ...", to_number, caller)
        participant = await lk.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=to_number,
                sip_number=caller,
                room_name=room,
                participant_identity=f"sip_{to_number.lstrip('+')}",
                participant_name="Callee",
                wait_until_answered=wait,
            )
        )
        logger.info("connected: %s", participant.participant_identity)
        logger.info("room: %s", room)
    finally:
        await lk.aclose()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Call a phone number with the agent.")
    parser.add_argument("number", help="destination in E.164, e.g. +918169796256")
    parser.add_argument(
        "--trunk-id",
        default=os.environ.get("LIVEKIT_OUTBOUND_TRUNK_ID", ""),
        help="LiveKit outbound trunk id (default: LIVEKIT_OUTBOUND_TRUNK_ID)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="return as soon as the call is placed instead of waiting for an answer",
    )
    args = parser.parse_args()

    if not args.number.startswith("+"):
        raise SystemExit("\nNumber must be E.164 and start with '+', e.g. +918169796256\n")
    if not args.trunk_id:
        raise SystemExit(
            "\nNo trunk id. Set LIVEKIT_OUTBOUND_TRUNK_ID in .env or pass --trunk-id.\n"
        )

    try:
        asyncio.run(place_call(args.number, trunk_id=args.trunk_id, wait=not args.no_wait))
    except api.TwirpError as exc:
        logger.error("LiveKit rejected the call: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
