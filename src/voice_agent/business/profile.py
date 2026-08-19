"""Business facts, kept as data rather than baked into the prompt text.

The prompt template in prompts/ refers to these by name. Editing the JSON
changes what the agent knows without touching any Python.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / "profile.json"
DEFAULT_FAQ_PATH = Path(__file__).resolve().parent / "faq.json"


@dataclass(frozen=True)
class BusinessProfile:
    data: dict[str, Any]
    faqs: list[dict[str, str]] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def as_prompt_vars(self) -> dict[str, str]:
        """Flatten to strings so the prompt template can interpolate them."""
        out: dict[str, str] = {}
        for key, value in self.data.items():
            if key.startswith("_"):
                continue  # editorial comments, not agent knowledge
            if isinstance(value, list):
                out[key] = "\n".join(f"- {item}" for item in value)
            else:
                out[key] = str(value)
        out["faqs"] = self.faq_block()
        return out

    def faq_block(self) -> str:
        """Render the FAQ as Q/A pairs for the prompt."""
        return "\n\n".join(
            f"Q: {item['q']}\nA: {item['a']}" for item in self.faqs
        )


def load_profile(
    path: Path | None = None, faq_path: Path | None = None
) -> BusinessProfile:
    target = path or DEFAULT_PROFILE_PATH
    if not target.exists():
        raise FileNotFoundError(f"Business profile not found at {target}")

    faq_target = faq_path or DEFAULT_FAQ_PATH
    faqs: list[dict[str, str]] = []
    if faq_target.exists():
        faqs = json.loads(faq_target.read_text(encoding="utf-8")).get("faqs", [])

    return BusinessProfile(json.loads(target.read_text(encoding="utf-8")), faqs)
