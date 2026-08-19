"""Voice activity detection.

Silero runs locally - the ONNX weights ship inside the plugin wheel, so this
needs no API key and no download step. VAD stays active even when the STT model
handles end-of-turn, because barge-in detection is a separate job from turn
detection.
"""

from __future__ import annotations

from livekit.agents import vad as vad_base

# Must be module scope - see the note in providers/stt.py.
from livekit.plugins import silero


def build_vad(prewarmed: vad_base.VAD | None = None) -> vad_base.VAD:
    """Return the VAD, reusing a prewarmed instance when one is available.

    `VAD.load()` is blocking and reads the ONNX weights off disk. The worker
    loads it once in `prewarm_fnc`; doing it again here would put that cost back
    on the critical path of every call, which is exactly what prewarming exists
    to avoid. Console mode has no prewarm step, so fall back to loading.
    """
    if prewarmed is not None:
        return prewarmed

    return silero.VAD.load()
