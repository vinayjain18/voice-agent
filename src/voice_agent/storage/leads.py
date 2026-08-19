"""Append captured leads to a CSV file.

CSV rather than .xlsx on purpose: it needs no dependency, Excel and Sheets open
it directly, and appending is atomic-ish so a crash mid-call cannot corrupt
earlier rows. An .xlsx would require rewriting the whole workbook on every
write, which loses every previous lead if the process dies at the wrong moment.

Each call runs in its own OS process, so two simultaneous callers really can
write at the same time. `fcntl.flock` serialises them; without it rows
interleave and the file is silently mangled.
"""

from __future__ import annotations

import csv
import fcntl
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEAD_FIELDS = [
    "timestamp_utc",
    "kind",
    "name",
    "contact",
    "reason",
    "preferred_date",
    "preferred_time",
    "raw_request",
    "caller_number",
    "room",
]


def _rotate_if_schema_changed(path: Path) -> None:
    """Move an old CSV aside when the columns have changed.

    Appending new-shaped rows under an old header silently misaligns every
    column, which is worse than losing the file - the data looks fine and is
    wrong. Rotating keeps the old rows readable in their own file.
    """
    if not path.exists() or path.stat().st_size == 0:
        return

    with path.open("r", newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), [])

    if header == LEAD_FIELDS:
        return

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archived = path.with_name(f"{path.stem}.legacy-{stamp}{path.suffix}")
    path.rename(archived)
    logger.warning(
        "lead CSV columns changed; previous rows preserved at %s", archived
    )


def append_lead(path: Path, **fields: Any) -> None:
    """Append one lead row, creating the file with a header if needed.

    Never raises: losing a lead is bad, but crashing a live call because a disk
    is full is worse. Failures are logged loudly instead.
    """
    row = {key: "" for key in LEAD_FIELDS}
    row["timestamp_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    for key, value in fields.items():
        if key in row:
            row[key] = "" if value is None else str(value)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_schema_changed(path)
        is_new = not path.exists() or path.stat().st_size == 0

        with path.open("a", newline="", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                # Re-check under the lock: another process may have created the
                # header between our check above and acquiring the lock.
                handle.seek(0, 2)
                if is_new and handle.tell() == 0:
                    csv.DictWriter(handle, fieldnames=LEAD_FIELDS).writeheader()
                csv.DictWriter(handle, fieldnames=LEAD_FIELDS).writerow(row)
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        logger.info("lead saved to %s: %s", path, row["name"] or "(no name)")
    except Exception:
        logger.exception("FAILED to save lead - details were: %s", row)
