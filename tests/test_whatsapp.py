"""WhatsApp webhook: verification, payload parsing, and signature checks."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from voice_agent.whatsapp.payload import parse_call_events
from voice_agent.whatsapp.webhook import _signature_ok, app

VERIFY_TOKEN = "test-verify-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def _inbound_payload(sdp="v=0\r\n...", sdp_type="offer", event="connect"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "calls",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001111",
                                 "phone_number_id": "123456"},
                    "calls": [{
                        "id": "wacid.TESTCALL",
                        "to": "15550001111",
                        "from": "918169796256",
                        "event": event,
                        "direction": "USER_INITIATED",
                        "session": {"sdp_type": sdp_type, "sdp": sdp},
                    }],
                },
            }],
        }],
    }


# --- verification handshake -------------------------------------------------

def test_verification_echoes_challenge(client):
    r = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "12345",
    })
    assert r.status_code == 200
    assert r.text == "12345"


def test_verification_rejects_wrong_token(client):
    r = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "12345",
    })
    assert r.status_code == 403


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["whatsapp_configured"] is True


# --- payload parsing --------------------------------------------------------

def test_parses_inbound_connect():
    events = parse_call_events(_inbound_payload())
    assert len(events) == 1
    e = events[0]
    assert e.call_id == "wacid.TESTCALL"
    assert e.caller == "918169796256"
    assert e.phone_number_id == "123456"
    assert e.is_inbound_connect


def test_answer_sdp_is_not_an_inbound_connect():
    """An SDP answer belongs to an outbound call, not an inbound accept."""
    events = parse_call_events(_inbound_payload(sdp_type="answer"))
    assert not events[0].is_inbound_connect


def test_events_without_sdp_are_skipped():
    """A connect with no SDP cannot be bridged, so it is dropped."""
    body = _inbound_payload()
    body["entry"][0]["changes"][0]["value"]["calls"][0].pop("session")
    assert parse_call_events(body) == []


def test_non_call_fields_ignored():
    body = _inbound_payload()
    body["entry"][0]["changes"][0]["field"] = "messages"
    assert parse_call_events(body) == []


@pytest.mark.parametrize("body", [
    {}, {"entry": None}, {"entry": [{}]},
    {"entry": [{"changes": "nope"}]},
    {"entry": [{"changes": [{"field": "calls", "value": None}]}]},
])
def test_malformed_payloads_never_raise(body):
    """A bad payload must not 500 - it would trigger Meta retry storms."""
    assert parse_call_events(body) == []


# --- webhook POST -----------------------------------------------------------

def test_post_ignores_non_call_payload_with_200(client):
    body = _inbound_payload()
    body["entry"][0]["changes"][0]["field"] = "messages"
    r = client.post("/webhook", json=body)
    assert r.status_code == 200


def test_post_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    r = client.post("/webhook", json=_inbound_payload(),
                    headers={"x-hub-signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


def test_post_accepts_valid_signature(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    body = _inbound_payload()
    body["entry"][0]["changes"][0]["field"] = "messages"  # no LiveKit call
    raw = json.dumps(body).encode()
    sig = hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    r = client.post("/webhook", content=raw,
                    headers={"content-type": "application/json",
                             "x-hub-signature-256": f"sha256={sig}"})
    assert r.status_code == 200


def test_signature_skipped_when_no_secret():
    assert _signature_ok(None, b"anything", None) is True


def test_signature_requires_header_when_secret_set():
    assert _signature_ok("s", b"body", None) is False


# --- regression: real payloads captured from Meta -----------------------------

REAL_CONNECT = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "2793142004396749",
        "changes": [{
            "field": "calls",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551983082",
                             "phone_number_id": "1208740925666349"},
                "contacts": [{"profile": {"name": "Vinay Jain"},
                              "wa_id": "918169796256"}],
                "calls": [{
                    "id": "wacid.IhggMDAxQ0ZFMTYwQkQ0NzY3RDY5MzdGMzhERjlFRUVBRUU=",
                    "from": "918169796256", "to": "15551983082",
                    "event": "connect", "direction": "USER_INITIATED",
                    "session": {"sdp": "v=0\r\no=- 178 2 IN IP4 127.0.0.1\r\n",
                                "sdp_type": "offer"},
                }],
            },
        }],
    }],
}

REAL_TERMINATE = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "2793142004396749",
        "changes": [{
            "field": "calls",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "1208740925666349"},
                "calls": [{
                    "id": "wacid.IhggMDAxQ0ZFMTYwQkQ0NzY3RDY5MzdGMzhERjlFRUVBRUU=",
                    "from": "918169796256", "to": "15551983082",
                    "event": "terminate", "direction": "USER_INITIATED",
                    "status": "COMPLETED",
                }],
            },
        }],
    }],
}


def test_real_meta_connect_payload_is_accepted():
    """Captured from an actual inbound WhatsApp call."""
    events = parse_call_events(REAL_CONNECT)
    assert len(events) == 1
    e = events[0]
    assert e.caller == "918169796256"
    assert e.sdp_type == "offer"
    assert e.is_inbound_connect


def test_real_meta_terminate_is_parsed_so_the_room_can_be_released():
    """This used to assert the bug.

    The parser dropped every event without an SDP, and a terminate never has
    one, so the webhook's release branch never ran on a real call. On a live
    test on 2026-09-13 Meta's terminate arrived at 14:54:50, was logged as "no
    actionable call event", and the agent kept talking to an empty line until
    LiveKit's own 30 second cleanup closed the room at 14:55:20.
    """
    events = parse_call_events(REAL_TERMINATE)
    assert len(events) == 1
    event = events[0]
    assert event.event == "terminate"
    assert event.call_id.startswith("wacid.")
    assert not event.is_inbound_connect
    assert not event.is_outbound_connect


def test_accept_request_wraps_sdp_in_session_description():
    """The sdp field is a protobuf message, not a string.

    Passing a raw string raised:
      TypeError: Parameter to initialize message field must be dict or
      instance of same class: expected <class 'rtc.SessionDescription'>
    which failed every real call at the accept step.
    """
    import livekit.api as lkapi
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.rtc import SessionDescription

    event = parse_call_events(REAL_CONNECT)[0]
    req = lkapi.AcceptWhatsAppCallRequest(
        whatsapp_phone_number_id="1208740925666349",
        whatsapp_api_key="x",
        whatsapp_cloud_api_version="25.0",
        whatsapp_call_id=event.call_id,
        sdp=SessionDescription(type=event.sdp_type, sdp=event.sdp),
        room_name="whatsapp-test",
        agents=[RoomAgentDispatch(agent_name="voice-agent-demo")],
    )
    assert req.sdp.type == "offer"
    assert req.sdp.sdp.startswith("v=0")


def test_webhook_source_wraps_sdp():
    """Guard the call site itself, not just that the type exists."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "src/voice_agent/whatsapp/webhook.py"
    text = src.read_text()
    assert "SessionDescription(" in text, "sdp must be wrapped in SessionDescription"
    assert "sdp=event.sdp," not in text, "raw string sdp would fail at runtime"


# --- hanging up the WhatsApp leg ---------------------------------------------

def test_call_id_attribute_is_passed_to_livekit():
    """Without this the agent cannot hang up the WhatsApp call on shutdown."""
    from pathlib import Path

    from voice_agent.whatsapp.disconnect import CALL_ID_ATTRIBUTE

    src = Path(__file__).parent.parent / "src/voice_agent/whatsapp/webhook.py"
    text = src.read_text()
    assert "participant_attributes=" in text
    assert CALL_ID_ATTRIBUTE in text


def test_find_call_id_reads_participant_attributes():
    from voice_agent.whatsapp.disconnect import CALL_ID_ATTRIBUTE, find_whatsapp_call_id

    class P:
        attributes: ClassVar[dict] = {CALL_ID_ATTRIBUTE: "wacid.ABC"}

    class Room:
        remote_participants: ClassVar[dict] = {"sip_1": P()}

    assert find_whatsapp_call_id(Room()) == "wacid.ABC"


def test_find_call_id_returns_none_for_non_whatsapp_calls():
    from voice_agent.whatsapp.disconnect import find_whatsapp_call_id

    class P:
        attributes: ClassVar[dict] = {"something": "else"}

    class Room:
        remote_participants: ClassVar[dict] = {"sip_1": P()}

    assert find_whatsapp_call_id(Room()) is None
    assert find_whatsapp_call_id(type("R", (), {"remote_participants": {}})()) is None


def test_find_call_id_never_raises_on_a_broken_room():
    """This runs during shutdown; an exception there helps nobody."""
    from voice_agent.whatsapp.disconnect import find_whatsapp_call_id

    assert find_whatsapp_call_id(object()) is None
    assert find_whatsapp_call_id(type("R", (), {"remote_participants": None})()) is None


def test_find_call_id_falls_back_to_room_metadata():
    """Outbound calls: the id only exists once DialWhatsAppCall returns, after the
    participant was created, so the dialer stores it on the room instead."""
    from types import SimpleNamespace

    from voice_agent.whatsapp.disconnect import find_whatsapp_call_id

    room = SimpleNamespace(remote_participants={}, metadata='{"whatsapp_call_id": "wacid.OUT"}')
    assert find_whatsapp_call_id(room) == "wacid.OUT"


def test_a_participant_attribute_wins_over_room_metadata():
    from types import SimpleNamespace

    from voice_agent.whatsapp.disconnect import CALL_ID_ATTRIBUTE, find_whatsapp_call_id

    participant = SimpleNamespace(attributes={CALL_ID_ATTRIBUTE: "wacid.IN"})
    room = SimpleNamespace(
        remote_participants={"p": participant},
        metadata='{"whatsapp_call_id": "wacid.OUT"}',
    )
    assert find_whatsapp_call_id(room) == "wacid.IN"


def test_unreadable_room_metadata_is_ignored():
    """Shutdown path: anything unexpected means no id, never an exception."""
    from types import SimpleNamespace

    from voice_agent.whatsapp.disconnect import find_whatsapp_call_id

    for metadata in ("", "not json", "[1, 2]", '{"other": "x"}', None, '{"whatsapp_call_id": 7}'):
        room = SimpleNamespace(remote_participants={}, metadata=metadata)
        assert find_whatsapp_call_id(room) is None, metadata


def test_the_dialer_and_the_agent_agree_on_the_key():
    """The dialer runs on Vercel and cannot import the agent's module."""
    from voice_agent.reminders.channels import CALL_ID_KEY
    from voice_agent.whatsapp.disconnect import CALL_ID_ATTRIBUTE

    assert CALL_ID_KEY == CALL_ID_ATTRIBUTE


async def test_disconnect_without_token_is_a_clean_no_op():
    """BUSINESS_INITIATED needs the Meta token; missing it must not raise."""
    from voice_agent.whatsapp.disconnect import disconnect_whatsapp_call

    assert await disconnect_whatsapp_call("wacid.ABC", None) is False


def test_agent_hangs_up_whatsapp_on_shutdown():
    from pathlib import Path

    src = Path(__file__).parent.parent / "src/voice_agent/main.py"
    text = src.read_text()
    assert "find_whatsapp_call_id" in text
    assert "disconnect_whatsapp_call" in text


def test_terminate_events_release_the_room():
    """User hangup must free the room, not wait out LiveKit's 30s cleanup."""
    from pathlib import Path

    for rel in ("src/voice_agent/whatsapp/webhook.py", "deploy/webhook/main.py"):
        text = (Path(__file__).parent.parent / rel).read_text()
        assert '_release(' in text, f"{rel} does not handle terminate"
        assert "USER_INITIATED" in text, f"{rel} missing USER_INITIATED"


def test_hangup_grace_period_is_configurable(monkeypatch):
    """Cutting the WhatsApp leg instantly clips the agent's closing line."""
    from voice_agent.config import Settings

    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("WHATSAPP_HANGUP_GRACE_SECONDS", raising=False)
    assert Settings.load().whatsapp.hangup_grace_seconds == 1.0

    monkeypatch.setenv("WHATSAPP_HANGUP_GRACE_SECONDS", "4.5")
    assert Settings.load().whatsapp.hangup_grace_seconds == 4.5


def test_hangup_runs_after_the_transcript_not_before():
    """Ordering is the only control we have: callbacks run via asyncio.gather."""
    from pathlib import Path

    text = (Path(__file__).parent.parent / "src/voice_agent/main.py").read_text()
    body = text[text.index("async def _on_shutdown"):text.index("ctx.add_shutdown_callback")]
    assert body.index("save_transcript") < body.index("disconnect_whatsapp_call"), (
        "the WhatsApp hangup must come last, after the closing audio has drained"
    )
    assert "asyncio.sleep(grace)" in body


# --- outbound (business-initiated) calls ---------------------------------------
#
# When the business dials out, Meta answers with a `connect` event carrying an
# SDP *answer* and direction BUSINESS_INITIATED. LiveKit's docs: call
# ConnectWhatsAppCall with it immediately, or the callee hears silence and the
# call drops. Both webhook copies used to log "ignoring event" and stop.


def _outbound_connect_payload(sdp="v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"):
    body = _inbound_payload(sdp=sdp, sdp_type="answer")
    call = body["entry"][0]["changes"][0]["value"]["calls"][0]
    call["direction"] = "BUSINESS_INITIATED"
    call["from"], call["to"] = "15551983082", "918169796256"
    return body


def test_parses_outbound_connect():
    event = parse_call_events(_outbound_connect_payload())[0]
    assert event.is_outbound_connect
    assert not event.is_inbound_connect


def test_an_inbound_offer_is_not_an_outbound_connect():
    event = parse_call_events(_inbound_payload())[0]
    assert not event.is_outbound_connect


class _FakeConnector:
    calls: ClassVar[list[tuple[str, object]]] = []

    async def connect_whatsapp_call(self, request):
        _FakeConnector.calls.append(("connect", request))

    async def accept_whatsapp_call(self, request, **_):
        _FakeConnector.calls.append(("accept", request))

    async def disconnect_whatsapp_call(self, request):
        _FakeConnector.calls.append(("disconnect", request))


class _FakeLiveKitAPI:
    def __init__(self, *_, **__):
        self.connector = _FakeConnector()

    async def aclose(self):
        pass


@pytest.fixture
def fake_livekit(monkeypatch):
    from voice_agent.whatsapp import webhook

    _FakeConnector.calls = []
    monkeypatch.setattr(webhook.api, "LiveKitAPI", _FakeLiveKitAPI)
    return _FakeConnector.calls


def test_outbound_connect_hands_the_answer_to_livekit(client, fake_livekit):
    body = _outbound_connect_payload(sdp="v=0\r\nanswer-sdp\r\n")

    r = client.post("/webhook", json=body)

    assert r.status_code == 200
    assert [kind for kind, _ in fake_livekit] == ["connect"]
    request = fake_livekit[0][1]
    assert request.whatsapp_call_id == "wacid.TESTCALL"
    assert request.sdp.type == "answer"
    assert request.sdp.sdp == "v=0\r\nanswer-sdp\r\n"


def test_inbound_connect_still_goes_to_accept(client, fake_livekit):
    r = client.post("/webhook", json=_inbound_payload())

    assert r.status_code == 200
    assert [kind for kind, _ in fake_livekit] == ["accept"]


def test_both_webhook_copies_connect_outbound_calls():
    """Constraint 16: the Vercel copy is a second copy and must match."""
    from pathlib import Path

    for rel in ("src/voice_agent/whatsapp/webhook.py", "deploy/webhook/main.py"):
        text = (Path(__file__).parent.parent / rel).read_text()
        assert "connect_whatsapp_call" in text, f"{rel} never connects outbound calls"
        assert "is_outbound_connect" in text, f"{rel} does not route outbound connects"


# --- terminate events must reach the release handler --------------------------

# The call object of Meta's terminate for the outbound test call on 2026-09-13,
# as logged. The log line was cut at 600 characters, so only the fields that
# were visible are reproduced here.
REAL_OUTBOUND_TERMINATE = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "2793142004396749",
        "changes": [{
            "field": "calls",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551983082",
                             "phone_number_id": "1208740925666349"},
                "calls": [{
                    "id": "wacid.IRggRTY3QTg3NTI0ODdBQzAzMkYxOUIwOUZCNjI3NDY5OUMcGAsxNTU1MTk4MzA4MhUCABUeAA==",
                    "from": "15551983082", "to": "918169796256",
                    "event": "terminate", "timestamp": "1789291490",
                    "direction": "BUSINESS_INITIATED", "start_time": "1789291460",
                }],
            },
        }],
    }],
}

# Meta's RINGING update for the same call. Status updates arrive under
# `statuses`, not `calls`, and must never be treated as a call event.
REAL_RINGING_STATUS = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "2793142004396749",
        "changes": [{
            "field": "calls",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551983082",
                             "phone_number_id": "1208740925666349"},
                "statuses": [{
                    "id": "wacid.IRggRTY3QTg3NTI0ODdBQzAzMkYxOUIwOUZCNjI3NDY5OUMcGAsxNTU1MTk4MzA4MhUCABUeAA==",
                    "status": "RINGING", "timestamp": "1789291449",
                    "recipient_id": "918169796256", "type": "call",
                }],
            },
        }],
    }],
}


def test_real_outbound_terminate_is_parsed():
    events = parse_call_events(REAL_OUTBOUND_TERMINATE)
    assert [event.event for event in events] == ["terminate"]
    assert events[0].direction == "BUSINESS_INITIATED"


def test_status_updates_are_still_ignored():
    assert parse_call_events(REAL_RINGING_STATUS) == []


def test_terminate_post_releases_the_call(client, fake_livekit):
    """The whole point: a hangup reaches DisconnectWhatsAppCall."""
    import livekit.api as lkapi

    r = client.post("/webhook", json=REAL_TERMINATE)

    assert r.status_code == 200
    assert [kind for kind, _ in fake_livekit] == ["disconnect"]
    request = fake_livekit[0][1]
    assert request.whatsapp_call_id == REAL_TERMINATE["entry"][0]["changes"][0]["value"]["calls"][0]["id"]
    assert request.disconnect_reason == lkapi.DisconnectWhatsAppCallRequest.USER_INITIATED


def test_releasing_a_call_sends_the_whatsapp_api_key(client, fake_livekit):
    """LiveKit rejects a USER_INITIATED disconnect without it:
    `whatsapp api key is required` (invalid_argument, 400)."""
    client.post("/webhook", json=REAL_TERMINATE)

    request = fake_livekit[0][1]
    assert request.whatsapp_api_key == "token"


def test_releasing_without_a_token_warns_instead_of_calling(client, fake_livekit, monkeypatch, caplog):
    """A disconnect LiveKit will reject is not worth sending. Say why instead."""
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    with caplog.at_level("WARNING"):
        client.post("/webhook", json=REAL_TERMINATE)

    assert fake_livekit == []
    assert "WHATSAPP_ACCESS_TOKEN" in caplog.text


def test_both_webhook_copies_send_the_key_on_release():
    """Constraint 16: the Vercel copy must match."""
    import re
    from pathlib import Path

    for rel in ("src/voice_agent/whatsapp/webhook.py", "deploy/webhook/main.py"):
        text = (Path(__file__).parent.parent / rel).read_text()
        release = re.search(r"async def _release\(.*?(?=\n(?:async )?def )", text, re.DOTALL)
        assert release, f"{rel} has no _release"
        assert "whatsapp_api_key=" in release.group(0), f"{rel} releases without the key"


def test_both_payload_parsers_are_identical():
    """Constraint 16: the Vercel copy of the parser must never drift."""
    from pathlib import Path

    root = Path(__file__).parent.parent
    source = (root / "src/voice_agent/whatsapp/payload.py").read_text()
    bundled = (root / "deploy/webhook/wa/payload.py").read_text()
    assert source == bundled
