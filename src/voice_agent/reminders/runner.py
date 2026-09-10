"""One pass of the reminder scanner.

Triggered on a timer from outside (see deploy/appsscript/). Everything that
decides *what* to send lives in `storage.appointments.due_for_reminder`, which
is pure and tested; this only sequences claim, send and record.

Ordering is what makes a double trigger safe. The row is claimed first, so a
second overlapping pass sees `sending` and skips it. If this process dies
between the claim and the call, the claim goes stale after
REMINDER_CLAIM_TIMEOUT_MINUTES and the next pass picks it up again, which is
what makes a restart resume rather than lose the reminder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from voice_agent.config import Settings
from voice_agent.reminders.channels import ReminderChannel, build_channel
from voice_agent.storage import build_store, due_for_reminder

logger = logging.getLogger(__name__)


@dataclass
class ReminderRun:
    scanned: int = 0
    due: int = 0
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    channel: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "due": self.due,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "channel": self.channel,
            "error": self.error,
        }


async def run_once(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    channel: ReminderChannel | None = None,
) -> ReminderRun:
    settings = settings or Settings.load()
    result = ReminderRun(channel=(channel.name if channel else settings.reminders.channel))

    store = build_store(settings)
    if store is None:
        result.error = "appointment store is not configured"
        logger.error(result.error)
        return result

    try:
        appointments = await store.all()
    except Exception as exc:
        result.error = f"could not read the appointment sheet: {exc}"
        logger.exception("reminder scan could not read the sheet")
        return result

    result.scanned = len(appointments)
    moment = now or datetime.now(UTC)
    tz = ZoneInfo(settings.reminders.timezone)

    due = due_for_reminder(
        appointments,
        now=moment,
        tz=tz,
        lead_minutes=settings.reminders.lead_minutes,
        tolerance_minutes=settings.reminders.tolerance_minutes,
        max_attempts=settings.reminders.max_attempts,
        claim_timeout_minutes=settings.reminders.claim_timeout_minutes,
    )
    result.due = len(due)
    if not due:
        return result

    sender = channel or build_channel(settings)
    result.channel = sender.name

    for appointment in due:
        claimed = await store.claim_reminder(
            appointment.booking_ref, max_attempts=settings.reminders.max_attempts
        )
        if claimed is None:
            result.skipped.append(appointment.booking_ref)
            continue

        try:
            delivered = await sender.send(claimed)
        except Exception:
            logger.exception("reminder channel raised for %s", claimed.booking_ref)
            delivered = False

        await store.finish_reminder(claimed.booking_ref, sent=delivered)
        (result.sent if delivered else result.failed).append(claimed.booking_ref)

    logger.info(
        "reminder pass: %d scanned, %d due, %d sent, %d failed, %d skipped",
        result.scanned,
        result.due,
        len(result.sent),
        len(result.failed),
        len(result.skipped),
    )
    return result
