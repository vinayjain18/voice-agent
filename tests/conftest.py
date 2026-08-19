"""Test isolation.

Settings.load() reads the real .env so the agent works from a bare `uv run`.
Tests must not inherit whatever happens to be in the developer's .env, or they
pass or fail depending on which keys that file holds.
"""

from __future__ import annotations

import pytest

from voice_agent import config


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """Stop Settings.load() from reading the developer's real .env file."""
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)
