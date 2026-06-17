"""Calendar availability checking for the follow-up pipeline (read-only).

When the Next Step agent recommends ``schedule_meeting``, the pipeline must
consult the rep's real calendar before the draft engine writes the email, so the
draft proposes concrete free times instead of "let me know when you're free".

This module reads availability and shapes a booking proposal. It never persists,
interrupts, or writes — booking is a deferred write the orchestrator (Step 6)
persists as a ``PendingAction(status='pending')`` and the writer executes only
after the rep accepts.

Timestamps cross the dataclass boundary as ISO-8601 strings, so
``json.dumps(asdict(result))`` succeeds and the result drops straight into
``PendingAction.action_payload`` (jsonb). Slot math uses real datetimes;
conversion happens at the dataclass boundary (the ``_jsonify`` discipline from
``followup/profile/schemas.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from followup.calendar.reader import CalendarReader

# How far out to search for alternatives when every proposed time is busy.
_ALTERNATIVE_SEARCH_DAYS = 5
_MAX_ALTERNATIVES = 3


@dataclass
class TimeSlot:
    start: str  # ISO-8601
    end: str  # ISO-8601
    available: bool


@dataclass
class CalendarResult:
    available_slots: list[TimeSlot]  # the proposed times, each flagged
    all_busy: bool
    suggested_alternatives: list[TimeSlot]  # populated only when all_busy


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (or pass a datetime through) → aware datetime.

    Naive datetimes are assumed UTC; ``Z`` suffixes are normalized. Returns
    ``None`` for anything unparseable.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def check_availability(
    *,
    calendar_reader: "CalendarReader",
    owner_user_id: str,
    workspace_id: str,
    proposed_times: list[str],
    duration_minutes: int = 30,
    find_slots_when_empty: bool = False,
) -> CalendarResult:
    """Check the rep's calendar against proposed times; shape a booking proposal.

    Cases:

    * **Empty/None ``proposed_times``** →
      - with ``find_slots_when_empty`` (the meeting path): proactively search the
        rep's next free business-hour slots so the draft offers concrete, real
        times instead of the model inventing some.
      - otherwise: empty result, no bridge call (a quiet "nothing to check").
    * **Some proposed time free** → ``all_busy=False`` and no alternatives.
    * **All proposed times busy** → ``all_busy=True`` plus up to three
      business-hour alternatives in the next five business days.
    """
    from followup.calendar.reader import _overlaps

    if not proposed_times:
        if not find_slots_when_empty:
            return CalendarResult(available_slots=[], all_busy=False, suggested_alternatives=[])
        slots = await _find_free_slots(calendar_reader, owner_user_id, duration_minutes)
        return CalendarResult(
            available_slots=slots, all_busy=not slots, suggested_alternatives=[]
        )

    duration = timedelta(minutes=duration_minutes)

    # Parse the proposed times once; keep aligned (slot_start, slot_end) ranges.
    ranges: list[tuple[datetime, datetime]] = []
    for raw in proposed_times:
        start = _parse_iso(raw)
        if start is None:
            continue
        ranges.append((start, start + duration))

    if not ranges:
        return CalendarResult(available_slots=[], all_busy=False, suggested_alternatives=[])

    # One windowed fetch over the whole min→max span rather than N bridge calls.
    window_start = min(start for start, _ in ranges)
    window_end = max(end for _, end in ranges)
    events = await calendar_reader.get_events(owner_user_id, window_start, window_end)

    available_slots: list[TimeSlot] = []
    for slot_start, slot_end in ranges:
        available = not any(_overlaps(slot_start, slot_end, event) for event in events)
        available_slots.append(
            TimeSlot(
                start=slot_start.isoformat(),
                end=slot_end.isoformat(),
                available=available,
            )
        )

    if any(slot.available for slot in available_slots):
        return CalendarResult(
            available_slots=available_slots,
            all_busy=False,
            suggested_alternatives=[],
        )

    # Every proposed time is busy — search the next business days for free slots.
    alternatives = await _find_free_slots(
        calendar_reader, owner_user_id, duration_minutes
    )
    return CalendarResult(
        available_slots=available_slots,
        all_busy=True,
        suggested_alternatives=alternatives,
    )


async def _find_free_slots(
    calendar_reader: "CalendarReader", owner_user_id: str, duration_minutes: int
) -> list[TimeSlot]:
    """The rep's next free business-hour slots over the alternative-search window."""
    now = datetime.now(timezone.utc)
    return await calendar_reader.find_free_slots(
        owner_user_id,
        now,
        now + timedelta(days=_ALTERNATIVE_SEARCH_DAYS),
        duration_minutes,
        max_slots=_MAX_ALTERNATIVES,
    )


def build_schedule_payload(
    *,
    slot: TimeSlot,
    participants: list[dict[str, Any]],
    title: str,
    description: str = "",
) -> dict[str, Any]:
    """Shape a ``schedule_meeting`` payload for the orchestrator to persist.

    Pure builder: returns the JSON-safe dict the orchestrator (Step 6) stores as
    ``PendingAction.action_payload`` and the writer later hands to the calendar
    write tool (under the writer's elevated scope, after the rep accepts). This
    module persists and executes **nothing**.

    TODO: confirm the calendar write tool's arg shape (title/startsAt/endsAt/
    isFullDay/participants) against learn_tools before production; this mirrors
    seed_data.add_calendar_event.
    """
    return {
        "title": title,
        "startsAt": slot.start,
        "endsAt": slot.end,
        "isFullDay": False,
        "description": description,
        "participants": participants,
    }


__all__ = [
    "TimeSlot",
    "CalendarResult",
    "check_availability",
    "build_schedule_payload",
]
