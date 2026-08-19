"""Prompt templates, stored as files so they can be edited without touching code."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def render_prompt(name: str, variables: dict[str, str]) -> str:
    """Load prompts/<name>.md and interpolate {placeholders}.

    Raises a clear error if the template references a variable the business
    profile does not define, rather than emitting a prompt with a literal
    '{missing_key}' in it that the model would then read aloud.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    template = path.read_text(encoding="utf-8")
    try:
        return template.format(**variables)
    except KeyError as exc:
        raise KeyError(
            f"Prompt '{name}' references {exc} but the business profile does not define it."
        ) from exc
