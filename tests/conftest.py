"""Test isolation.

Settings.load() reads the real .env so the agent works from a bare `uv run`.
Tests must not inherit whatever happens to be in the developer's .env, or they
pass or fail depending on which keys that file holds.
"""

from __future__ import annotations

import base64
import functools
import json

import pytest

from voice_agent import config


@functools.cache
def _throwaway_key() -> str:
    """A real RSA key, generated once.

    SheetsClient parses the key when it is constructed, which is what we want in
    production: a malformed credential should not wait for a live call to
    surface. So the tests need a genuine one, not a placeholder string.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def fake_service_account() -> str:
    """Credentials that parse but could never authenticate."""
    return base64.b64encode(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "test@example.iam.gserviceaccount.com",
                "private_key": _throwaway_key(),
                "private_key_id": "test",
            }
        ).encode()
    ).decode()


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """Stop Settings.load() from reading the developer's real .env file."""
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def _appointment_store_configured(monkeypatch):
    """Preflight refuses to start without a sheet, so give every test one.

    Tests that care about the unconfigured case clear these themselves.
    """
    monkeypatch.setenv("APPOINTMENTS_SHEET_ID", "test-sheet-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", fake_service_account())
