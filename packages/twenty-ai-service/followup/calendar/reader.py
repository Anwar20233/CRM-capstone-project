"""Calendar read access for the follow-up pipeline.

Calendar is a distinct subdomain from CRM records, so it gets its own reader
``Protocol`` rather than being bolted onto ``CRMReader``. There is exactly one
owner of bridge calls (``BridgeCalendarReader``) and one in-memory double
(``FakeCalendarReader``), injected the same way ``crm_reader`` is on
``PipelineDeps``.

This module is **read-only by contract**. Every bridge call runs under
``READER_SCOPE``; no calendar write tool and no elevated write scope appear
anywhere here. Booking a meeting is a deferred write owned by the orchestrator
(Step 6) plus the writer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol, runtime_checkable

from followup.calendar.availability import TimeSlot, _parse_iso

logger = logging.getLogger(__name__)

# Business-hours window for free-slot search: Mon–Fri, 09:00–17:00.
# TODO: these bounds are evaluated in UTC. Convert to the rep's real timezone
# (a workspace-member preference) before production so 09:00 means 09:00 local.
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 17
BUSINESS_DAYS = frozenset({0, 1, 2, 3, 4})  # Mon=0 .. Fri=4


@dataclass
class CalendarEvent:
    """A calendar event as the pipeline cares about it.

    ``start``/``end`` are timezone-aware datetimes. An all-day event blocks its
    whole day regardless of the stored start/end (see ``_event_busy_range``).
    """

    event_id: Optional[str]
    title: Optional[str]
    start: datetime
    end: datetime
    is_all_day: bool = False


@runtime_checkable
class CalendarReader(Protocol):
    """Reads a sales rep's calendar. The single owner of calendar bridge calls."""

    async def get_events(
        self, owner_user_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        """Return the rep's events overlapping ``[start, end]`` (timezone-aware)."""
        ...

    async def find_free_slots(
        self,
        owner_user_id: str,
        start: datetime,
        end: datetime,
        duration_minutes: int,
        max_slots: int = 3,
    ) -> list[TimeSlot]:
        """Return up to ``max_slots`` free ``duration_minutes`` slots in business hours."""
        ...


# ===========================================================================
# Shared slot math (used by every reader so business-hours/overlap rules
# live in one place — fakes and the bridge adapter agree by construction).
# ===========================================================================


def _event_busy_range(event: CalendarEvent) -> tuple[datetime, datetime]:
    """The interval an event actually blocks (all-day → its whole calendar day)."""
    if event.is_all_day:
        day_start = event.start.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start, day_start + timedelta(days=1)
    return event.start, event.end


def _overlaps(start: datetime, end: datetime, event: CalendarEvent) -> bool:
    """Half-open overlap: ``[start, end)`` intersects the event's busy range."""
    busy_start, busy_end = _event_busy_range(event)
    return start < busy_end and busy_start < end


def free_slots_from_events(
    events: list[CalendarEvent],
    start: datetime,
    end: datetime,
    duration_minutes: int,
    max_slots: int,
) -> list[TimeSlot]:
    """Derive free business-hour slots from a known event list, locally.

    Walks each business day in ``[start, end]`` in ``duration_minutes`` steps
    across the 09:00–17:00 window, emitting slots that overlap no event. Weekends
    and non-business hours are skipped. Pure function: no bridge, no clock.
    """
    duration = timedelta(minutes=duration_minutes)
    slots: list[TimeSlot] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last_day and len(slots) < max_slots:
        if day.weekday() in BUSINESS_DAYS:
            window_start = day.replace(hour=BUSINESS_START_HOUR)
            window_end = day.replace(hour=BUSINESS_END_HOUR)
            candidate = max(window_start, start)
            while candidate + duration <= window_end and len(slots) < max_slots:
                if candidate >= start and not any(
                    _overlaps(candidate, candidate + duration, event)
                    for event in events
                ):
                    slots.append(
                        TimeSlot(
                            start=candidate.isoformat(),
                            end=(candidate + duration).isoformat(),
                            available=True,
                        )
                    )
                candidate += duration
        day += timedelta(days=1)
    return slots


# ===========================================================================
# Default adapter — bridge-backed, READER_SCOPE only
# ===========================================================================


class BridgeCalendarReader:
    """Default ``CalendarReader``: id-keyed reads go straight through the bridge.

    Mirrors ``ReaderAgentCRMReader._bridge_find`` exactly — we always hold
    ``owner_user_id``/``workspace_id``, so there is no ReaderWorker round-trip
    (it only adds latency and this model's find_one flakiness). ``find_free_slots``
    derives gaps from ``get_events`` locally; we do not assume the bridge exposes
    a free/busy endpoint.
    """

    async def _bridge_find(
        self, tool: str, args: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Identical pattern to dependencies.ReaderAgentCRMReader._bridge_find.
        from agent.tool_scope import READER_SCOPE
        from agent.tools.composite_reads import _exec, _identity
        from bridge_client import forward
        from followup.profile.dependencies import _records_from_bridge_data

        result = await forward("execute", _exec(tool, args, _identity(READER_SCOPE)))
        if not result.get("ok"):
            logger.warning("bridge %s failed: %s", tool, result.get("error"))
            return []
        return _records_from_bridge_data(result.get("data"))

    async def get_events(
        self, owner_user_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        # Overlap filter: an event intersects [start, end] iff it starts before
        # end and ends after start.
        # TODO: confirm against learn_tools("find_calendar_events") before
        # production — the time fields (startsAt/endsAt) and the all-day flag
        # (isFullDay) follow Twenty's standard calendarEvent schema (see
        # seed_data.add_calendar_event) but have not been verified against the
        # live reader. The fake covers tests.
        #
        # Restrict to the rep's events via the participant join. Events can't be
        # filtered by their `calendarEventParticipants` relation (the backend
        # only allows filtering many-to-one relations), so we resolve the rep's
        # event ids through the junction object first. owner_user_id is the rep's
        # workspace-member id.
        participants = await self._bridge_find(
            "find_calendar_event_participants",
            {"limit": 200, "workspaceMemberId": {"eq": owner_user_id}},
        )
        event_ids: list[str] = []
        for participant in participants:
            event_id = participant.get("calendarEventId")
            if event_id and event_id not in event_ids:
                event_ids.append(event_id)
        if not event_ids:
            return []
        # orderBy must be an array of single-key objects — the backend does
        # `(orderBy ?? []).filter(...)`, so a bare object value throws.
        args: dict[str, Any] = {
            "limit": 100,
            "id": {"in": event_ids},
            "startsAt": {"lt": end.isoformat()},
            "endsAt": {"gt": start.isoformat()},
            "orderBy": [{"startsAt": "AscNullsLast"}],
        }
        records = await self._bridge_find("find_calendar_events", args)
        events: list[CalendarEvent] = []
        for record in records:
            event = _event_from_record(record)
            if event is not None:
                events.append(event)
        return events

    async def find_free_slots(
        self,
        owner_user_id: str,
        start: datetime,
        end: datetime,
        duration_minutes: int,
        max_slots: int = 3,
    ) -> list[TimeSlot]:
        events = await self.get_events(owner_user_id, start, end)
        return free_slots_from_events(events, start, end, duration_minutes, max_slots)


def _event_from_record(record: dict[str, Any]) -> Optional[CalendarEvent]:
    """A ``find_calendar_events`` record → ``CalendarEvent`` (None if unparseable)."""
    # Canceled events do not block time.
    if record.get("isCanceled"):
        return None
    start = _parse_iso(record.get("startsAt"))
    end = _parse_iso(record.get("endsAt"))
    if start is None or end is None:
        return None
    return CalendarEvent(
        event_id=record.get("id"),
        title=record.get("title"),
        start=start,
        end=end,
        is_all_day=bool(record.get("isFullDay")),
    )


# ===========================================================================
# Test double — deterministic, no bridge
# ===========================================================================


class FakeCalendarReader:
    """In-memory ``CalendarReader`` for unit tests (mirrors FakeCRMReader style).

    Configure busy time with ``events`` (full ``CalendarEvent`` objects, incl.
    all-day) and/or ``busy`` (``(start, end)`` datetime tuples). ``find_free_slots``
    returns deterministic business-hour slots (tomorrow 10:00/14:00/16:00, rolled
    to the next business day) minus anything that overlaps a configured event.
    """

    _PREDICTABLE_HOURS = (10, 14, 16)

    def __init__(
        self,
        *,
        events: Optional[list[CalendarEvent]] = None,
        busy: Optional[list[tuple[datetime, datetime]]] = None,
    ) -> None:
        self._events: list[CalendarEvent] = list(events or [])
        for busy_start, busy_end in busy or []:
            self._events.append(
                CalendarEvent(
                    event_id=None,
                    title="busy",
                    start=busy_start,
                    end=busy_end,
                )
            )

    async def get_events(
        self, owner_user_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return [
            event for event in self._events if _overlaps(start, end, event)
        ]

    async def find_free_slots(
        self,
        owner_user_id: str,
        start: datetime,
        end: datetime,
        duration_minutes: int,
        max_slots: int = 3,
    ) -> list[TimeSlot]:
        duration = timedelta(minutes=duration_minutes)
        # Anchor on the first business day strictly after ``start``.
        day = (start + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        while day.weekday() not in BUSINESS_DAYS:
            day += timedelta(days=1)

        slots: list[TimeSlot] = []
        for hour in self._PREDICTABLE_HOURS:
            if len(slots) >= max_slots:
                break
            candidate = day.replace(hour=hour, tzinfo=day.tzinfo)
            if any(
                _overlaps(candidate, candidate + duration, event)
                for event in self._events
            ):
                continue
            slots.append(
                TimeSlot(
                    start=candidate.isoformat(),
                    end=(candidate + duration).isoformat(),
                    available=True,
                )
            )
        return slots


__all__ = [
    "CalendarEvent",
    "CalendarReader",
    "BridgeCalendarReader",
    "FakeCalendarReader",
    "free_slots_from_events",
]
