"""Ask a caller who has gone quiet whether they are there, then let the line go.

`AgentSession` provides the first wait: once neither side has spoken for
`user_away_timeout` it sets the user state to "away" and emits
`user_state_changed` (`agent_session.py:1992`).

The wait after the question is a timer this module owns, because
`_update_user_state` returns early when the state has not changed
(`agent_session.py:1968`): "away" is emitted once per silence episode, and the
state is still "away" while the question goes unanswered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from livekit.agents import get_job_context

from voice_agent.config import Settings

logger = logging.getLogger("voice_agent.silence")

# Spoken content lives in business/profile.json; these cover a missing key,
# because the failure mode would be silence on a call about silence.
DEFAULT_CHECK_LINE = "Sorry, are you still there?"
DEFAULT_GOODBYE_LINE = "I can't hear you, so I'll let you go. Call us back any time."


def away_timeout(settings: Settings) -> float | None:
    """What to give `AgentSession(user_away_timeout=...)`.

    None disables silence handling: with no "away" event there is nothing to
    act on.
    """
    seconds = settings.pipeline.silence_prompt_seconds
    return seconds if seconds > 0 else None


class SilenceWatch:
    """Asks once whether the caller is there, then ends the call.

    Holds no LiveKit types: anything that can `say`, `shutdown` and `once` will
    do, so it is testable without a room.
    """

    def __init__(
        self,
        session: Any,
        *,
        check_line: str,
        goodbye_line: str,
        hangup_seconds: float,
        max_prompts: int,
    ) -> None:
        self._session = session
        self._check_line = check_line
        self._goodbye_line = goodbye_line
        self._hangup_seconds = hangup_seconds
        self._max_prompts = max_prompts

        self._prompts = 0
        self._pending: asyncio.Task | None = None
        self._asking = False
        self._closing = False

    @property
    def closing(self) -> bool:
        """True once this watch has decided to end the call."""
        return self._closing

    def on_user_state(self, state: str) -> None:
        """Feed the session's user state in. Never raises: it is an event handler."""
        try:
            if state == "away":
                self._on_away()
            else:
                # They are back, so the countdown is off.
                self._cancel_pending()
                self._asking = False
        except Exception:
            logger.exception("silence watch failed on state %r", state)

    def cancel(self) -> None:
        """Stop watching, for a call that is ending some other way."""
        self._cancel_pending()

    # --- internals ------------------------------------------------------------

    def _on_away(self) -> None:
        if self._closing or self._asking:
            return
        if self._prompts >= self._max_prompts:
            # Constraint 10: repeating a question into silence is worse than
            # ending the call.
            logger.info("caller still silent, already asked %d times", self._prompts)
            return

        self._asking = True
        self._prompts += 1
        logger.info(
            "caller has gone quiet, asking if they are there (%d of %d)",
            self._prompts,
            self._max_prompts,
        )
        self._pending = asyncio.create_task(self._ask_then_wait())

    async def _ask_then_wait(self) -> None:
        try:
            await self._session.say(self._check_line)
            await asyncio.sleep(self._hangup_seconds)
            await self._hang_up()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not handle a silent caller")

    async def _hang_up(self) -> None:
        if self._closing:
            return
        self._closing = True
        logger.info("no answer after %.0fs, ending the call", self._hangup_seconds)

        handle = await self._session.say(self._goodbye_line)

        # Close the session only once the goodbye has played, then leave the
        # ordering to main.py's shutdown callback (constraint 13). Deleting the
        # room here would strand the WhatsApp leg, which must be hung up first.
        def _shutdown_session(_: object) -> None:
            self._session.shutdown()

        handle.add_done_callback(_shutdown_session)

        def _shutdown_job(_: object) -> None:
            job = get_job_context(required=False)
            if job is not None:
                job.shutdown(reason="caller_silent")

        self._session.once("close", _shutdown_job)

    def _cancel_pending(self) -> None:
        task, self._pending = self._pending, None
        if task is not None and not task.done():
            task.cancel()


def build_silence_watch(settings: Settings, session: Any, profile: Any) -> SilenceWatch | None:
    """A watch for this call, or None when silence handling is off."""
    if away_timeout(settings) is None:
        logger.info("silence handling is off (SILENCE_PROMPT_SECONDS=0)")
        return None

    return SilenceWatch(
        session,
        check_line=_line(profile, "silence_check", DEFAULT_CHECK_LINE),
        goodbye_line=_line(profile, "silence_goodbye", DEFAULT_GOODBYE_LINE),
        hangup_seconds=settings.pipeline.silence_hangup_seconds,
        max_prompts=settings.pipeline.silence_max_prompts,
    )


def _line(profile: Any, key: str, fallback: str) -> str:
    try:
        spoken = str(profile[key]).strip()
    except (KeyError, TypeError):
        spoken = ""
    return spoken or fallback


def install(watch: SilenceWatch | None, session: Any) -> None:
    """Subscribe the watch to the session's user state changes."""
    if watch is None:
        return

    def _on_change(event: Any) -> None:
        watch.on_user_state(str(getattr(event, "new_state", "")))

    session.on("user_state_changed", _on_change)
