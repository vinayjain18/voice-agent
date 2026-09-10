"""The Vercel bundle is a copy, so the copy has to be provably current.

deploy/webhook/ deploys on its own and cannot import from src/, so the modules
the reminder endpoint needs are copied in by scripts/sync_webhook_bundle.py.
Copies rot. These tests make that a failing suite rather than a bug that only
shows up in production, where the sheet is real.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.sync_webhook_bundle import BUNDLE, MODULES, check

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK = ROOT / "deploy" / "webhook"


def test_the_bundle_matches_the_source():
    drifted = check()
    assert not drifted, (
        "deploy/webhook is out of date with src/. "
        "Run: uv run python scripts/sync_webhook_bundle.py"
    )


@pytest.mark.parametrize("name", MODULES)
def test_every_bundled_module_is_present(name):
    assert (BUNDLE / name).exists(), name


def test_the_bundle_never_imports_livekit_agents():
    """It would pull the plugin stack into a serverless function that cannot use it."""
    for name in MODULES:
        source = (BUNDLE / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                assert not module.startswith("livekit.agents"), f"{name}: {module}"
                assert not module.startswith("voice_agent.agents"), f"{name}: {module}"


def test_the_bundle_requirements_cover_what_it_imports():
    requirements = (WEBHOOK / "requirements.txt").read_text().lower()
    for package in ("fastapi", "google-auth", "aiohttp", "livekit-api", "python-dotenv"):
        assert package in requirements, package


def test_both_webhook_copies_expose_the_reminder_endpoint():
    """Constraint: a change to one copy must be made to the other."""
    for path in (
        WEBHOOK / "main.py",
        ROOT / "src" / "voice_agent" / "whatsapp" / "webhook.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert '@app.post("/tasks/reminders")' in source, path
        assert "run_once" in source, path


def test_both_copies_guard_the_endpoint_with_a_constant_time_comparison():
    """It sits on a public URL, so a plain == would leak the secret by timing."""
    for path in (
        WEBHOOK / "main.py",
        ROOT / "src" / "voice_agent" / "whatsapp" / "webhook.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source, path
        # An unset secret must refuse to run, never run unguarded.
        assert "refusing to run unguarded" in source, path
