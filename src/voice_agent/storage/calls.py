"""One row per call, so an admin can see why someone rang.

Separate from the appointment rows on purpose. Most calls do not end in a
booking - people ask about hours, insurance, results, or get put through to the
emergency room - and those are exactly the calls that vanish without a record.

Written after the call is already over, so nothing here can delay a hangup or
keep a caller on a dead line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from voice_agent.storage.sheets import SheetsClient, SheetsError

logger = logging.getLogger(__name__)

COLUMNS = [
    "call_utc",
    "direction",
    "caller_number",
    "duration_seconds",
    "outcome",
    "department",
    "booking_ref",
    "summary",
    "turns",
    "cost_usd",
    "room",
]

# Outcomes are a small fixed set so the column can be filtered and counted.
OUTCOME_BOOKED = "booked"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_RESCHEDULED = "rescheduled"
OUTCOME_ENQUIRY = "enquiry"
OUTCOME_EMERGENCY = "emergency referral"
OUTCOME_NO_ACTION = "no action"

# A summary is read in a spreadsheet cell, not a report.
MAX_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class CallRecord:
    call_utc: str
    direction: str
    caller_number: str
    duration_seconds: int
    outcome: str
    summary: str
    department: str = ""
    booking_ref: str = ""
    turns: int = 0
    cost_usd: float | None = None
    room: str = ""

    def to_row(self) -> list[str]:
        return [
            self.call_utc,
            self.direction,
            self.caller_number,
            str(self.duration_seconds),
            self.outcome,
            self.department,
            self.booking_ref,
            self.summary[:MAX_SUMMARY_CHARS],
            str(self.turns),
            f"{self.cost_usd:.5f}" if self.cost_usd is not None else "",
            self.room,
        ]


class CallLog:
    def __init__(self, client: SheetsClient, tab: str = "calls") -> None:
        self._client = client
        self._tab = tab

    async def append(self, record: CallRecord) -> bool:
        """Add one row. Never raises: this runs after the call has ended."""
        try:
            await self._client.ensure_tab(self._tab)
            rows = await self._client.read_rows(self._tab)
            if not rows:
                await self._client.insert_row(self._tab, COLUMNS, at=0)
                rows = [COLUMNS]
            elif [cell.strip().lower() for cell in rows[0]][: len(COLUMNS)] != COLUMNS:
                raise SheetsError(
                    f"tab '{self._tab}' has an unexpected header. Expected {COLUMNS}. "
                    "Refusing to write so existing rows are not misaligned."
                )

            await self._client.insert_row(self._tab, record.to_row(), at=len(rows))
            logger.info("call logged: %s / %s", record.outcome, record.summary[:80])
            return True
        except Exception:
            logger.exception("could not log the call summary")
            return False


def build_record(
    *,
    direction: str,
    caller_number: str,
    duration_seconds: float,
    summary: str,
    outcome: str,
    department: str = "",
    booking_ref: str = "",
    turns: int = 0,
    cost_usd: float | None = None,
    room: str = "",
) -> CallRecord:
    return CallRecord(
        call_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        direction=direction,
        caller_number=caller_number,
        duration_seconds=round(duration_seconds),
        outcome=outcome,
        summary=" ".join((summary or "").split()),
        department=department,
        booking_ref=booking_ref,
        turns=turns,
        cost_usd=cost_usd,
        room=room,
    )
