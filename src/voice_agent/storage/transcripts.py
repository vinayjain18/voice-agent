"""Write the full conversation to disk when a call ends.

Worth having for a demo: you can show what was actually said, and it is the
raw material for improving the prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def save_transcript(
    directory: Path, room: str, history: Any, *, extra: dict[str, Any] | None = None
) -> Path | None:
    """Write one transcript JSON file. Returns the path, or None on failure."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_room = "".join(c if c.isalnum() or c in "-_" else "_" for c in room)
        path = directory / f"{stamp}_{safe_room}.json"

        payload: dict[str, Any] = {
            "room": room,
            "ended_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            **(extra or {}),
            # exclude_audio/image keep the file readable; metrics are useful for
            # spotting which turns were slow.
            "history": history.to_dict(exclude_audio=True, exclude_image=True),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("transcript saved to %s", path)
        return path
    except Exception:
        logger.exception("failed to save transcript for room %s", room)
        return None
