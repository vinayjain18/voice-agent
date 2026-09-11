"""The silent downgrade from adaptive interruption to plain VAD.

One 700ms inference timeout turns adaptive interruption off for the rest of the
call, and nothing above the SDK's own INFO line ever says so.
"""

from __future__ import annotations

import logging

import pytest

from voice_agent.observability.interruption import (
    FALLBACK_MESSAGE,
    watch_interruption_fallback,
)


@pytest.fixture
def sdk_logger():
    logger = logging.getLogger("livekit.agents")
    before = list(logger.filters)
    yield logger
    logger.filters = before


def _sdk_says(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="livekit.agents",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_the_fallback_is_reported_as_a_warning(sdk_logger, caplog):
    watch = watch_interruption_fallback()

    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        kept = watch.filter(_sdk_says(FALLBACK_MESSAGE + ", falling back"))

    assert kept is True, "observing, not suppressing"
    assert watch.degraded is True
    assert "interruption" in caplog.text
    assert "rest of this call" in caplog.text


def test_an_unrelated_sdk_line_is_left_alone(sdk_logger, caplog):
    watch = watch_interruption_fallback()

    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        kept = watch.filter(_sdk_says("using preemptive generation"))

    assert kept is True
    assert watch.degraded is False
    assert caplog.text == ""


def test_the_warning_is_not_repeated(sdk_logger, caplog):
    """The SDK disables the detector once. A second line would be noise."""
    watch = watch_interruption_fallback()

    with caplog.at_level(logging.WARNING, logger="voice_agent"):
        watch.filter(_sdk_says(FALLBACK_MESSAGE))
        watch.filter(_sdk_says(FALLBACK_MESSAGE))

    assert caplog.text.count("rest of this call") == 1


def test_watching_installs_itself_on_the_sdk_logger(sdk_logger):
    watch = watch_interruption_fallback()

    assert watch in sdk_logger.filters


def test_watching_twice_leaves_a_single_watch(sdk_logger):
    """A worker handles many calls. A watch per call that never came off would
    grow the filter list on a process-wide logger for the life of the worker."""
    from voice_agent.observability.interruption import InterruptionFallbackWatch

    first = watch_interruption_fallback()
    second = watch_interruption_fallback()

    installed = [f for f in sdk_logger.filters if isinstance(f, InterruptionFallbackWatch)]
    assert installed == [second]
    assert first not in sdk_logger.filters
