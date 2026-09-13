"""Parse Meta's `calls` webhook payload.

Meta wraps everything in an entry/changes envelope. The exact shape is not
fully documented publicly, so this parser is deliberately tolerant: it walks the
structure with .get(), returns None rather than raising, and the caller logs the
raw body when nothing is found. That way an unexpected shape shows up as a log
line to fix rather than a 500 that drops the caller.

Observed shape (matches LiveKit's examples, which read `call.id` and
`call.session.sdp`):

    {"object": "whatsapp_business_account",
     "entry": [{"changes": [{"field": "calls",
        "value": {"metadata": {"phone_number_id": "..."},
                  "calls": [{"id": "wacid...", "event": "connect",
                             "direction": "USER_INITIATED",
                             "from": "9198...",
                             "session": {"sdp_type": "offer", "sdp": "v=0..."}}]}}]}]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WhatsAppCallEvent:
    call_id: str
    sdp: str
    sdp_type: str
    event: str
    direction: str
    caller: str
    phone_number_id: str

    @property
    def is_inbound_connect(self) -> bool:
        """A user-initiated call that is ready to be bridged."""
        return self.event == "connect" and self.sdp_type == "offer"

    @property
    def is_outbound_connect(self) -> bool:
        """A call the business placed, picked up: Meta sends the SDP answer."""
        return self.event == "connect" and self.sdp_type == "answer"


def parse_call_events(body: dict[str, Any]) -> list[WhatsAppCallEvent]:
    """Extract every call event in the payload. Never raises."""
    events: list[WhatsAppCallEvent] = []

    for entry in _as_list(body.get("entry")):
        for change in _as_list(entry.get("changes")):
            if change.get("field") != "calls":
                continue
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")

            for call in _as_list(value.get("calls")):
                session = call.get("session") or {}
                call_id = call.get("id") or ""
                sdp = session.get("sdp") or ""
                event = call.get("event") or ""
                if not call_id:
                    continue
                # A connect with no SDP cannot be bridged, so it is dropped. A
                # terminate never carries one, and dropping it too meant a hangup
                # never reached the release handler: the agent kept talking to an
                # empty line until LiveKit's own 30 second cleanup. RINGING and
                # ACCEPTED arrive under `statuses`, not `calls`, so never get here.
                if not sdp and event != "terminate":
                    continue
                events.append(
                    WhatsAppCallEvent(
                        call_id=call_id,
                        sdp=sdp,
                        sdp_type=session.get("sdp_type") or "",
                        event=event,
                        direction=call.get("direction") or "",
                        caller=call.get("from") or "",
                        phone_number_id=phone_number_id,
                    )
                )
    return events


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []
