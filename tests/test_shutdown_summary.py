"""The summary must actually fire when a session ends.

Cannot be checked against a live call here, so this drives the real entrypoint
with the network pieces mocked and asserts the shutdown callback is registered
and does its job.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit.agents.metrics.usage import (
    AgentSessionUsage,
    LLMModelUsage,
    STTModelUsage,
    TTSModelUsage,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "RUMIK_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _fake_session():
    session = MagicMock()
    session.start = AsyncMock()
    session.generate_reply = AsyncMock()
    session.say = AsyncMock()
    session.history.to_dict.return_value = {"items": []}
    session.usage = AgentSessionUsage(model_usage=[
        STTModelUsage(provider="deepgram", model="flux-general-multi", audio_duration=120.0),
        LLMModelUsage(provider="groq", model="openai/gpt-oss-120b",
                      input_tokens=10_000, input_cached_tokens=6_000, output_tokens=500),
        TTSModelUsage(provider="rumik", model="mulberry",
                      characters_count=1500, audio_duration=60.0),
    ])
    return session


async def _run_entrypoint():
    """Run the real entrypoint and return the registered shutdown callback."""
    from voice_agent import main

    ctx = MagicMock()
    ctx.room.name = "call-test"
    ctx.job.metadata = ""  # inbound
    ctx.connect = AsyncMock()
    ctx.proc.userdata = {}
    registered = []
    ctx.add_shutdown_callback = registered.append

    session = _fake_session()
    with patch.object(main, "build_session", return_value=session):
        await main.entrypoint(ctx)

    assert registered, "no shutdown callback was registered"
    return registered[0], session


async def test_shutdown_callback_is_registered():
    callback, _ = await _run_entrypoint()
    assert callable(callback)


async def test_shutdown_logs_the_session_summary(caplog):
    callback, _ = await _run_entrypoint()
    with caplog.at_level("INFO", logger="voice_agent.usage"):
        await callback()

    output = caplog.text
    assert "SESSION SUMMARY" in output
    assert "Duration" in output
    assert "TOTAL" in output
    assert "flux-general-multi" in output
    assert "openai/gpt-oss-120b" in output
    assert "mulberry" in output


async def test_shutdown_writes_usage_into_the_transcript(tmp_path):
    callback, _ = await _run_entrypoint()
    await callback()

    files = list((tmp_path / "transcripts").glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["estimated_cost_usd"] > 0
    assert saved["duration_seconds"] >= 0
    assert len(saved["usage"]) == 3


async def test_summary_still_logged_if_transcripts_are_off(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("SAVE_TRANSCRIPTS", "false")
    callback, _ = await _run_entrypoint()
    with caplog.at_level("INFO", logger="voice_agent.usage"):
        await callback()
    assert "SESSION SUMMARY" in caplog.text
    assert not (tmp_path / "transcripts").exists()
