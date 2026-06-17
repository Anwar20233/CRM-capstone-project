"""Calendar availability subdomain for the Follow-Up pipeline (Step 5).

Read-only by contract: checks the rep's real calendar so a ``schedule_meeting``
recommendation proposes concrete free times. Never persists, interrupts, or
writes — booking is a deferred write owned by the orchestrator + the writer.

Public surface:

* ``TimeSlot`` / ``CalendarResult`` / ``check_availability`` /
  ``build_schedule_payload`` — availability checking + the orchestrator's
  payload builder (availability.py).
* ``CalendarEvent`` / ``CalendarReader`` / ``BridgeCalendarReader`` /
  ``FakeCalendarReader`` — the reader Protocol, its bridge adapter, and the
  in-memory test double (reader.py).
"""

from __future__ import annotations

from followup.calendar.availability import (
    CalendarResult,
    TimeSlot,
    build_schedule_payload,
    check_availability,
)
from followup.calendar.reader import (
    BridgeCalendarReader,
    CalendarEvent,
    CalendarReader,
    FakeCalendarReader,
    free_slots_from_events,
)

__all__ = [
    # availability
    "TimeSlot",
    "CalendarResult",
    "check_availability",
    "build_schedule_payload",
    # reader
    "CalendarEvent",
    "CalendarReader",
    "BridgeCalendarReader",
    "FakeCalendarReader",
    "free_slots_from_events",
]
