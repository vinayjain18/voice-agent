"""LiveKit cloud/local mode resolution and turn-taking selection."""

from __future__ import annotations

import pytest

from voice_agent.config import (
    LOCAL_LIVEKIT_API_KEY,
    LOCAL_LIVEKIT_URL,
    ConfigError,
    Settings,
)
from voice_agent.session import _resolve_interruption_mode, _resolve_turn_detection


def _settings(monkeypatch, **env) -> Settings:
    for key in (
        "LIVEKIT_MODE",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "TURN_DETECTION",
        "INTERRUPTION_MODE",
        "STT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.load()


def test_cloud_mode_reads_standard_livekit_vars(monkeypatch):
    s = _settings(
        monkeypatch,
        LIVEKIT_URL="wss://x.livekit.cloud",
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s",
    )
    assert s.livekit.mode == "cloud"
    assert s.livekit.url == "wss://x.livekit.cloud"
    assert s.livekit.is_configured
    assert not s.livekit.is_local


def test_local_mode_uses_dev_server_defaults(monkeypatch):
    s = _settings(monkeypatch, LIVEKIT_MODE="local")
    assert s.livekit.is_local
    assert s.livekit.url == LOCAL_LIVEKIT_URL
    assert s.livekit.api_key == LOCAL_LIVEKIT_API_KEY
    assert s.livekit.is_configured


def test_local_mode_overrides_are_honoured(monkeypatch):
    monkeypatch.setenv("LIVEKIT_LOCAL_URL", "ws://192.168.1.5:7880")
    s = _settings(monkeypatch, LIVEKIT_MODE="local")
    assert s.livekit.url == "ws://192.168.1.5:7880"


def test_cloud_mode_without_credentials_is_unconfigured(monkeypatch):
    s = _settings(monkeypatch)
    assert not s.livekit.is_configured


def test_invalid_mode_is_rejected(monkeypatch):
    with pytest.raises(ConfigError, match="LIVEKIT_MODE"):
        _settings(monkeypatch, LIVEKIT_MODE="hybrid")


def test_auto_turn_detection_prefers_flux(monkeypatch):
    s = _settings(monkeypatch)
    assert _resolve_turn_detection(s) == "stt"


def test_auto_turn_detection_falls_back_without_flux(monkeypatch):
    s = _settings(monkeypatch, STT_MODEL="nova-3")
    assert _resolve_turn_detection(s) == "vad"


def test_stt_turn_detection_rejected_for_non_flux_model(monkeypatch):
    s = _settings(monkeypatch, TURN_DETECTION="stt", STT_MODEL="nova-3")
    with pytest.raises(ConfigError, match="Deepgram Flux"):
        _resolve_turn_detection(s)


def test_livekit_turn_detection_requires_credentials(monkeypatch):
    s = _settings(monkeypatch, TURN_DETECTION="livekit")
    with pytest.raises(ConfigError, match="LIVEKIT_URL"):
        _resolve_turn_detection(s)


def test_livekit_turn_detection_allowed_when_configured(monkeypatch):
    s = _settings(
        monkeypatch,
        TURN_DETECTION="livekit",
        LIVEKIT_URL="wss://x.livekit.cloud",
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s",
    )
    assert _resolve_turn_detection(s) == "livekit"


def test_auto_interruption_is_vad_without_livekit(monkeypatch):
    s = _settings(monkeypatch)
    assert _resolve_interruption_mode(s) == "vad"


def test_auto_interruption_is_adaptive_with_livekit(monkeypatch):
    s = _settings(
        monkeypatch,
        LIVEKIT_URL="wss://x.livekit.cloud",
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s",
    )
    assert _resolve_interruption_mode(s) == "adaptive"


def test_adaptive_interruption_requires_livekit(monkeypatch):
    s = _settings(monkeypatch, INTERRUPTION_MODE="adaptive")
    with pytest.raises(ConfigError, match="Cloud"):
        _resolve_interruption_mode(s)


def test_local_mode_has_credentials_but_no_cloud_inference(monkeypatch):
    """A self-hosted server is media only - no turn detector, no adaptive."""
    s = _settings(monkeypatch, LIVEKIT_MODE="local")
    assert s.livekit.is_configured
    assert not s.livekit.has_cloud_inference


def test_auto_interruption_stays_vad_in_local_mode(monkeypatch):
    s = _settings(monkeypatch, LIVEKIT_MODE="local")
    assert _resolve_interruption_mode(s) == "vad"


def test_adaptive_rejected_in_local_mode(monkeypatch):
    s = _settings(monkeypatch, LIVEKIT_MODE="local", INTERRUPTION_MODE="adaptive")
    with pytest.raises(ConfigError, match="Cloud"):
        _resolve_interruption_mode(s)


def test_livekit_turn_detection_rejected_in_local_mode(monkeypatch):
    s = _settings(monkeypatch, LIVEKIT_MODE="local", TURN_DETECTION="livekit")
    with pytest.raises(ConfigError, match="inference gateway"):
        _resolve_turn_detection(s)
