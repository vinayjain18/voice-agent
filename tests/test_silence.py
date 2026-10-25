"""A caller who goes quiet is asked once, then let go.

The session marks the user "away" after a period where neither side speaks.
That event arrives once per silence episode, so the wait after the question is
the watch's own timer.
"""

from __future__ import annotations

import asyncio

from voice_agent.silence import SilenceWatch

# Short enough to keep the suite fast, long enough not to be flaky.
PROMPT = 0.01
HANGUP = 0.05


class FakeSpeechHandle:
    def __init__(self) -> None:
        self.callbacks: list = []

    def add_done_callback(self, fnc) -> None:
        self.callbacks.append(fnc)

    def finish(self) -> None:
        for fnc in list(self.callbacks):
            fnc(self)


class FakeSession:
    """Only the surface SilenceWatch is allowed to touch."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.handles: list[FakeSpeechHandle] = []
        self.shutdown_calls = 0
        self.once_events: list[str] = []
        self.deleted_room = False

    async def say(self, text: str, **_):
        self.said.append(text)
        handle = FakeSpeechHandle()
        self.handles.append(handle)
        return handle

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def once(self, event: str, _fnc) -> None:
        self.once_events.append(event)


def make_watch(session: FakeSession, **kwargs) -> SilenceWatch:
    options = {
        "check_line": "Are you still there?",
        "goodbye_line": "I'll let you go for now.",
        "hangup_seconds": HANGUP,
        "max_prompts": 2,
    }
    options.update(kwargs)
    return SilenceWatch(session, **options)


async def settle(seconds: float = HANGUP * 3) -> None:
    await asyncio.sleep(seconds)


# --- the prompt ---------------------------------------------------------------

async def test_going_away_asks_once_whether_they_are_there():
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle(HANGUP / 2)

    assert session.said == ["Are you still there?"]
    assert session.shutdown_calls == 0


async def test_a_repeated_away_event_does_not_ask_twice():
    """One silence episode asks one question."""
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    watch.on_user_state("away")
    await settle(HANGUP / 2)

    assert session.said == ["Are you still there?"]


# --- the caller coming back ---------------------------------------------------

async def test_speaking_again_cancels_the_hangup():
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle(HANGUP / 2)
    watch.on_user_state("speaking")
    await settle()

    assert session.said == ["Are you still there?"]
    assert session.shutdown_calls == 0, "the caller answered, do not hang up"


async def test_a_second_silence_is_prompted_again():
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle(HANGUP / 2)
    watch.on_user_state("listening")
    watch.on_user_state("away")
    await settle(HANGUP / 2)

    assert session.said == ["Are you still there?", "Are you still there?"]


async def test_the_prompt_cap_stops_the_nagging():
    """Constraint 10: the agent must not repeat a question into silence."""
    session = FakeSession()
    watch = make_watch(session, max_prompts=2)

    for _ in range(3):
        watch.on_user_state("away")
        await settle(HANGUP / 2)
        watch.on_user_state("listening")

    assert len(session.said) == 2


# --- the hangup ---------------------------------------------------------------

async def test_still_silent_after_the_second_wait_ends_the_call():
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle()

    assert session.said == ["Are you still there?", "I'll let you go for now."]
    assert session.shutdown_calls == 0, "shutdown waits for the goodbye to finish"

    session.handles[-1].finish()
    assert session.shutdown_calls == 1
    assert "close" in session.once_events, "the job must be shut down too"


async def test_the_watch_never_deletes_the_room():
    """Constraint 13: teardown order belongs to main.py."""
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle()

    assert not session.deleted_room
    assert not hasattr(watch, "delete_room")


async def test_a_call_already_ending_is_not_ended_twice():
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle()
    said_once = list(session.said)

    watch.on_user_state("away")
    await settle()

    assert session.said == said_once, "no second goodbye after the call is closing"


async def test_cancel_stops_a_pending_hangup():
    """The call ending some other way must not leave a timer running."""
    session = FakeSession()
    watch = make_watch(session)

    watch.on_user_state("away")
    await settle(HANGUP / 2)
    watch.cancel()
    await settle()

    assert session.said == ["Are you still there?"]
    assert session.shutdown_calls == 0


# --- configuration ------------------------------------------------------------

def test_the_session_is_given_our_silence_timeout(monkeypatch):
    """The first wait is the SDK's away timer, so it must be configured."""
    import inspect

    from voice_agent import session as session_module

    source = inspect.getsource(session_module.build_session)
    assert "user_away_timeout" in source, "the SDK default would apply instead"


def test_silence_settings_have_the_agreed_defaults(monkeypatch):
    from voice_agent.config import Settings

    for key in ("SILENCE_PROMPT_SECONDS", "SILENCE_HANGUP_SECONDS", "SILENCE_MAX_PROMPTS"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.load()

    assert settings.pipeline.silence_prompt_seconds == 10.0
    assert settings.pipeline.silence_hangup_seconds == 10.0
    assert settings.pipeline.silence_max_prompts == 2


def test_silence_handling_can_be_turned_off(monkeypatch):
    """Zero disables it: the session gets no away timer at all."""
    from voice_agent.config import Settings
    from voice_agent.silence import away_timeout

    monkeypatch.setenv("SILENCE_PROMPT_SECONDS", "0")
    settings = Settings.load()

    assert settings.pipeline.silence_prompt_seconds == 0.0
    assert away_timeout(settings) is None
