"""Notice when LiveKit silently downgrades interruption detection.

Adaptive interruption runs a ~100ms inference against LiveKit Cloud while the
agent is speaking, on a 700ms budget. When one of those times out,
`AgentActivity._fallback_to_vad_interruption` (`agent_activity.py:4490`) sets
`_interruption_detection_enabled = False` and the rest of the call runs on raw
VAD, where any 0.4s of audio cuts the agent off.

Three things make that worth reporting:

* It is permanent. That flag is set True once at construction and never again,
  so one transient timeout degrades every remaining turn of the call.
* It is silent. `_on_error` emits a session "error" event for LLM, STT, TTS and
  realtime failures, but `InterruptionDetectionError` returns before that line
  (`agent_activity.py:1899`), so there is no event to subscribe to.
* On a real call it showed up as the agent interrupting itself, which looks
  like a bug in our code and is not.

The detector is built with no arguments (`agent_activity.py:4613`) and the
session only accepts the strings "adaptive" or "vad", so the timeout cannot be
raised from here, and the flag cannot be set back without reaching into the
library's private state on a live call. Reporting it is what is actually
available, and it is worth having: a degraded call is otherwise
indistinguishable from a good one in the logs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("voice_agent")

# Narrow on purpose, like the filter in main.py: if the SDK rewords this, the
# watch stops matching rather than reporting something that did not happen.
FALLBACK_MESSAGE = "adaptive interruption disabled due to unrecoverable error"


class InterruptionFallbackWatch(logging.Filter):
    """Report the downgrade. Never suppresses anything."""

    def __init__(self) -> None:
        super().__init__()
        self.degraded = False

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.degraded and FALLBACK_MESSAGE in record.getMessage():
            self.degraded = True
            logger.warning(
                "adaptive interruption is off for the rest of this call: a "
                "LiveKit inference timed out and the SDK does not turn it back "
                "on. Interruptions now come from VAD alone, so background "
                "noise or audio echo can cut the agent off mid-sentence."
            )
        return True


def watch_interruption_fallback() -> InterruptionFallbackWatch:
    """Install a watch for this call on the SDK logger and hand it back.

    Any earlier watch is taken off first. The logger is process wide and a
    worker handles many calls, so a call whose shutdown never ran would
    otherwise leave its watch attached for the life of the worker.
    """
    sdk = logging.getLogger("livekit.agents")
    for stale in [f for f in sdk.filters if isinstance(f, InterruptionFallbackWatch)]:
        sdk.removeFilter(stale)
    watch = InterruptionFallbackWatch()
    sdk.addFilter(watch)
    return watch
