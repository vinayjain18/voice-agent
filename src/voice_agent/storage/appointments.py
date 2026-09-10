"""Appointments, stored in one Google Sheet tab.

One row per appointment, newest first. The sheet is the only copy: there is no
local cache to fall back on and no file to rebuild from, because the agent, a
later call on a different container and the reminder scanner are three separate
processes that must agree on what is booked.

Everything here is written to survive a restart at any point:

  - `booking_ref` is generated before the write and is the idempotency key.
  - Rows are only ever inserted or amended in place. Nothing clears a range, so
    a redeploy cannot erase what is already there.
  - A reminder is claimed (status -> sending) before it is dialled, so a crash
    between claim and call leaves evidence. A claim older than
    `claim_timeout_minutes` is picked up again rather than stranded.
  - If the tab already has a header that does not match, the store refuses to
    write instead of rotating or overwriting it.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from voice_agent.storage.sheets import SheetsClient, SheetsError

logger = logging.getLogger(__name__)

COLUMNS = [
    "booking_ref",
    "status",
    "patient_name",
    "patient_number",
    "slot_date",
    "slot_time",
    "reason",
    "raw_request",
    "reminder_status",
    "reminder_attempts",
    "reminder_last_utc",
    "created_utc",
    "cancelled_utc",
    "room",
]

STATUS_BOOKED = "booked"
STATUS_CANCELLED = "cancelled"
STATUS_COMPLETED = "completed"

REMINDER_PENDING = "pending"
REMINDER_SENDING = "sending"
REMINDER_SENT = "sent"
REMINDER_FAILED = "failed"
REMINDER_SKIPPED = "skipped"

# Digits only. A booking reference gets said out loud and heard back over a
# phone line, where letters are what speech recognition gets wrong.
REF_DIGITS = 4


class AppointmentError(RuntimeError):
    """The appointment could not be stored."""


class SlotTaken(AppointmentError):
    """Someone else holds that slot."""


@dataclass(frozen=True)
class Appointment:
    booking_ref: str
    status: str
    patient_name: str
    patient_number: str
    slot_date: str
    slot_time: str
    reason: str = ""
    raw_request: str = ""
    reminder_status: str = REMINDER_PENDING
    reminder_attempts: int = 0
    reminder_last_utc: str = ""
    created_utc: str = ""
    cancelled_utc: str = ""
    room: str = ""
    # 0-based index in the sheet, header included. Absent for rows not read back.
    row_index: int | None = None

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_BOOKED

    def starts_at(self, tz: ZoneInfo) -> datetime | None:
        try:
            moment = datetime.strptime(
                f"{self.slot_date} {self.slot_time}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=tz)
        except ValueError:
            logger.warning(
                "appointment %s has an unreadable slot: %r %r",
                self.booking_ref,
                self.slot_date,
                self.slot_time,
            )
            return None
        return moment

    def to_row(self) -> list[str]:
        return [
            self.booking_ref,
            self.status,
            self.patient_name,
            self.patient_number,
            self.slot_date,
            self.slot_time,
            self.reason,
            self.raw_request,
            self.reminder_status,
            str(self.reminder_attempts),
            self.reminder_last_utc,
            self.created_utc,
            self.cancelled_utc,
            self.room,
        ]

    @classmethod
    def from_row(cls, row: list[str], row_index: int) -> Appointment | None:
        if not row or not (row[0] or "").strip():
            return None
        padded = list(row) + [""] * (len(COLUMNS) - len(row))
        values = dict(zip(COLUMNS, (str(cell).strip() for cell in padded), strict=False))
        try:
            attempts = int(values["reminder_attempts"] or 0)
        except ValueError:
            attempts = 0
        return cls(
            booking_ref=values["booking_ref"],
            status=(values["status"] or STATUS_BOOKED).lower(),
            patient_name=values["patient_name"],
            patient_number=values["patient_number"],
            slot_date=values["slot_date"],
            slot_time=values["slot_time"],
            reason=values["reason"],
            raw_request=values["raw_request"],
            reminder_status=(values["reminder_status"] or REMINDER_PENDING).lower(),
            reminder_attempts=attempts,
            reminder_last_utc=values["reminder_last_utc"],
            created_utc=values["created_utc"],
            cancelled_utc=values["cancelled_utc"],
            room=values["room"],
            row_index=row_index,
        )


def normalise_number(value: str) -> str:
    """Compare phone numbers by digits, ignoring +, spaces and punctuation."""
    digits = "".join(character for character in (value or "") if character.isdigit())
    # Indian mobiles reach us variously as 9876543210, 919876543210 or
    # +919876543210. The last ten digits are the stable part.
    return digits[-10:] if len(digits) >= 10 else digits


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AppointmentStore:
    def __init__(self, client: SheetsClient, tab: str = "appointments") -> None:
        self._client = client
        self._tab = tab
        self._header_checked = False

    async def ensure_ready(self) -> None:
        """Create the header if the tab is empty; never touch existing rows."""
        if self._header_checked:
            return

        rows = await self._client.read_rows(self._tab)
        if not rows:
            await self._client.insert_row(self._tab, COLUMNS, at=0)
            self._header_checked = True
            return

        header = [cell.strip().lower() for cell in rows[0]]
        if header[: len(COLUMNS)] != COLUMNS:
            raise SheetsError(
                f"tab '{self._tab}' has an unexpected header. Expected {COLUMNS}, "
                f"found {header}. Refusing to write so existing rows are not "
                "misaligned. Fix the header, or point APPOINTMENTS_SHEET_TAB at a "
                "new tab."
            )
        self._header_checked = True

    async def _snapshot(self) -> tuple[list[Appointment], int]:
        """Every appointment, plus the current row count.

        The count is what new rows are appended at. Appending at the end is
        load-bearing: it means an existing row's index never changes, so an
        amendment computed from an earlier read cannot land on somebody else's
        row. Inserting at the top shifted every row below it and would silently
        overwrite a different patient's appointment.
        """
        await self.ensure_ready()
        rows = await self._client.read_rows(self._tab)
        out = []
        for index, row in enumerate(rows[1:], start=1):
            appointment = Appointment.from_row(row, index)
            if appointment is not None:
                out.append(appointment)
        return out, len(rows)

    async def all(self) -> list[Appointment]:
        appointments, _ = await self._snapshot()
        return appointments

    async def active(self) -> list[Appointment]:
        return [item for item in await self.all() if item.is_active]

    async def taken_slots(self, tz: ZoneInfo) -> set[datetime]:
        moments = (item.starts_at(tz) for item in await self.active())
        return {moment for moment in moments if moment is not None}

    async def find_by_number(self, number: str, *, tz: ZoneInfo) -> list[Appointment]:
        """Active future appointments for a caller, soonest first."""
        wanted = normalise_number(number)
        if not wanted:
            return []
        now = datetime.now(tz)
        matches = [
            item
            for item in await self.active()
            if normalise_number(item.patient_number) == wanted
            and (item.starts_at(tz) or now) >= now
        ]
        return sorted(matches, key=lambda item: (item.slot_date, item.slot_time))

    async def find_by_ref(self, booking_ref: str) -> Appointment | None:
        wanted = (booking_ref or "").strip()
        if not wanted:
            return None
        for item in await self.all():
            if item.booking_ref == wanted:
                return item
        return None

    async def create(
        self,
        *,
        patient_name: str,
        patient_number: str,
        slot: datetime,
        reason: str = "",
        raw_request: str = "",
        room: str = "",
    ) -> Appointment:
        """Book a slot, or raise SlotTaken if someone got there first.

        Sheets has no transactions, so the check and the write cannot be one
        operation. The write itself is atomic, and afterwards we re-read: if two
        bookings landed on the same slot, the later `created_utc` stands down.
        """
        existing, row_count = await self._snapshot()
        slot_date = slot.strftime("%Y-%m-%d")
        slot_time = slot.strftime("%H:%M")

        if _slot_holders(existing, slot_date, slot_time):
            raise SlotTaken(f"{slot_date} {slot_time} is already booked.")

        appointment = Appointment(
            booking_ref=_new_ref({item.booking_ref for item in existing if item.is_active}),
            status=STATUS_BOOKED,
            patient_name=patient_name.strip(),
            patient_number=patient_number.strip(),
            slot_date=slot_date,
            slot_time=slot_time,
            reason=reason.strip(),
            raw_request=raw_request.strip(),
            reminder_status=REMINDER_PENDING,
            created_utc=_now_utc(),
            room=room,
        )
        await self._client.insert_row(self._tab, appointment.to_row(), at=row_count)

        holders = _slot_holders(await self.all(), slot_date, slot_time)
        if len(holders) > 1:
            winner = min(holders, key=lambda item: (item.created_utc, item.booking_ref))
            if winner.booking_ref != appointment.booking_ref:
                await self.cancel(appointment.booking_ref, reason="double booking")
                raise SlotTaken(f"{slot_date} {slot_time} was taken during booking.")

        logger.info(
            "booked %s for %s at %s %s",
            appointment.booking_ref,
            appointment.patient_name or "(no name)",
            slot_date,
            slot_time,
        )
        return appointment

    async def cancel(self, booking_ref: str, *, reason: str = "") -> Appointment | None:
        def amend(current: Appointment) -> Appointment:
            return replace(
                current,
                status=STATUS_CANCELLED,
                cancelled_utc=_now_utc(),
                reminder_status=REMINDER_SKIPPED,
                raw_request=(f"{current.raw_request} | cancelled: {reason}".strip(" |") if reason else current.raw_request),
            )

        updated = await self._amend(booking_ref, amend)
        if updated:
            logger.info("cancelled %s", booking_ref)
        return updated

    async def claim_reminder(self, booking_ref: str, *, max_attempts: int) -> Appointment | None:
        """Mark a reminder as being sent. Returns None if it is not ours to send.

        Claiming before dialling is what makes a double trigger safe: the second
        scan sees `sending` and leaves it alone.
        """

        def amend(current: Appointment) -> Appointment:
            if current.reminder_attempts + 1 > max_attempts:
                raise _NotClaimable()
            return replace(
                current,
                reminder_status=REMINDER_SENDING,
                reminder_attempts=current.reminder_attempts + 1,
                reminder_last_utc=_now_utc(),
            )

        try:
            return await self._amend(booking_ref, amend)
        except _NotClaimable:
            return None

    async def finish_reminder(self, booking_ref: str, *, sent: bool) -> Appointment | None:
        def amend(current: Appointment) -> Appointment:
            return replace(
                current,
                reminder_status=REMINDER_SENT if sent else REMINDER_FAILED,
                reminder_last_utc=_now_utc(),
            )

        return await self._amend(booking_ref, amend)

    async def _amend(self, booking_ref: str, change) -> Appointment | None:
        """Read one row, change it, and write it back at the same index.

        New rows are appended at the end, so an existing row's index is stable
        and this cannot land on a different appointment. The write is still
        verified afterwards: a human deleting a row in the sheet while a call is
        in progress would shift indices in a way nothing here can prevent.
        """
        for attempt in range(3):
            matches = [item for item in await self.all() if item.booking_ref == booking_ref]
            if not matches:
                return None
            if len(matches) > 1:
                # Two rows claiming one reference means the sheet was edited by
                # hand. Refuse rather than pick one and hope.
                raise AppointmentError(
                    f"{booking_ref} appears on {len(matches)} rows. Fix the sheet by hand."
                )

            current = matches[0]
            if current.row_index is None:
                return None

            updated = change(current)
            await self._client.write_row(self._tab, current.row_index, updated.to_row())

            written = [item for item in await self.all() if item.booking_ref == booking_ref]
            if len(written) == 1 and written[0].row_index == current.row_index:
                return replace(updated, row_index=current.row_index)

            logger.warning(
                "row for %s did not land where expected (attempt %d), retrying",
                booking_ref,
                attempt + 1,
            )

        raise AppointmentError(f"could not update {booking_ref} after 3 attempts.")


class _NotClaimable(Exception):
    pass


def _slot_holders(appointments: list[Appointment], slot_date: str, slot_time: str) -> list[Appointment]:
    return [
        item
        for item in appointments
        if item.is_active and item.slot_date == slot_date and item.slot_time == slot_time
    ]


def _new_ref(existing: set[str]) -> str:
    upper = 10**REF_DIGITS
    for _ in range(50):
        candidate = f"{secrets.randbelow(upper):0{REF_DIGITS}d}"
        if candidate not in existing:
            return candidate
    raise AppointmentError("could not generate a free booking reference.")


def due_for_reminder(
    appointments: list[Appointment],
    *,
    now: datetime,
    tz: ZoneInfo,
    lead_minutes: int,
    tolerance_minutes: int,
    max_attempts: int,
    claim_timeout_minutes: int,
) -> list[Appointment]:
    """Appointments whose reminder should go out now.

    Pure, so the whole selection rule is testable without a network. A row stuck
    in `sending` past the claim timeout is included again: that is a process
    that died mid-call, and the patient still has not been reminded.
    """
    target = now.astimezone(tz) + timedelta(minutes=lead_minutes)
    window = timedelta(minutes=tolerance_minutes)
    stale_before = now.astimezone(tz) - timedelta(minutes=claim_timeout_minutes)

    due = []
    for item in appointments:
        if not item.is_active or item.reminder_attempts >= max_attempts:
            continue
        starts = item.starts_at(tz)
        if starts is None or not (target - window <= starts <= target + window):
            continue

        if item.reminder_status in {REMINDER_PENDING, REMINDER_FAILED}:
            due.append(item)
        elif item.reminder_status == REMINDER_SENDING:
            claimed_at = _parse_utc(item.reminder_last_utc)
            if claimed_at is None or claimed_at.astimezone(tz) < stale_before:
                due.append(item)

    return sorted(due, key=lambda item: (item.slot_date, item.slot_time))


def _parse_utc(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
