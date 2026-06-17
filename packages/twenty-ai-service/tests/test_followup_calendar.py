"""Unit tests for followup/calendar/ — Step 5 availability service.

Fakes only: no Postgres, no bridge. Covers:
* check_availability across all three cases (empty short-circuit; some free →
  not all_busy, no alternatives; all busy → all_busy + ≤3 business-hour
  alternatives).
* Weekend / business-hour skipping and all-day-event blocking in free-slot math.
* JSON-safety (json.dumps(asdict(result)) succeeds) and Protocol conformance
  (isinstance(FakeCalendarReader()/BridgeCalendarReader(), CalendarReader)).
* No write-tool name / WRITER_SCOPE anywhere in the module.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from followup.calendar import (
    BridgeCalendarReader,
    CalendarEvent,
    CalendarReader,
    CalendarResult,
    FakeCalendarReader,
    TimeSlot,
    build_schedule_payload,
    check_availability,
)
from followup.calendar.reader import free_slots_from_events


def _dt(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# A fixed Wednesday so weekday math is deterministic across runs.
WED = _dt(2026, 6, 17, 0, 0)  # 2026-06-17 is a Wednesday


# ===========================================================================
# Protocol conformance + JSON-safety
# ===========================================================================


def test_readers_conform_to_protocol():
    assert isinstance(FakeCalendarReader(), CalendarReader)
    assert isinstance(BridgeCalendarReader(), CalendarReader)


async def test_result_is_json_safe():
    reader = FakeCalendarReader()
    result = await check_availability(
        calendar_reader=reader,
        owner_user_id="rep-1",
        workspace_id="ws-1",
        proposed_times=[WED.replace(hour=10).isoformat()],
    )
    # Must round-trip cleanly into PendingAction.action_payload (jsonb).
    encoded = json.dumps(asdict(result))
    assert isinstance(json.loads(encoded), dict)


# ===========================================================================
# check_availability — the three cases
# ===========================================================================


async def test_empty_proposed_times_short_circuits():
    # Quiet "nothing to check": empty result, no error, and no bridge/reader call.
    class ExplodingReader:
        async def get_events(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("get_events should not be called")

        async def find_free_slots(self, *a, **k):  # pragma: no cover
            raise AssertionError("find_free_slots should not be called")

    for proposed in ([], None):
        result = await check_availability(
            calendar_reader=ExplodingReader(),
            owner_user_id="rep-1",
            workspace_id="ws-1",
            proposed_times=proposed,  # type: ignore[arg-type]
        )
        assert result == CalendarResult([], all_busy=False, suggested_alternatives=[])


async def test_empty_proposed_times_finds_free_slots_when_opted_in():
    # The meeting path opts in: no proposed times → offer the rep's free slots
    # so the draft proposes real, calendar-verified times.
    reader = FakeCalendarReader()
    result = await check_availability(
        calendar_reader=reader,
        owner_user_id="rep-1",
        workspace_id="ws-1",
        proposed_times=[],
        find_slots_when_empty=True,
    )
    assert result.all_busy is False
    assert result.suggested_alternatives == []
    assert len(result.available_slots) > 0
    for slot in result.available_slots:
        start = datetime.fromisoformat(slot.start)
        assert 9 <= start.hour < 17 and start.weekday() < 5
        assert slot.available is True


async def test_some_time_free_yields_no_alternatives():
    # 10:00 is busy, 14:00 is free → not all_busy, no alternatives offered.
    reader = FakeCalendarReader(
        busy=[(WED.replace(hour=10), WED.replace(hour=10, minute=30))]
    )
    result = await check_availability(
        calendar_reader=reader,
        owner_user_id="rep-1",
        workspace_id="ws-1",
        proposed_times=[
            WED.replace(hour=10).isoformat(),
            WED.replace(hour=14).isoformat(),
        ],
    )
    assert result.all_busy is False
    assert result.suggested_alternatives == []
    flags = {slot.start: slot.available for slot in result.available_slots}
    assert flags[WED.replace(hour=10).isoformat()] is False
    assert flags[WED.replace(hour=14).isoformat()] is True


async def test_all_busy_yields_business_hour_alternatives():
    # Both proposed times overlap a busy block → all_busy + ≤3 alternatives.
    reader = FakeCalendarReader(
        busy=[
            (WED.replace(hour=10), WED.replace(hour=11)),
            (WED.replace(hour=14), WED.replace(hour=15)),
        ]
    )
    result = await check_availability(
        calendar_reader=reader,
        owner_user_id="rep-1",
        workspace_id="ws-1",
        proposed_times=[
            WED.replace(hour=10).isoformat(),
            WED.replace(hour=14).isoformat(),
        ],
    )
    assert result.all_busy is True
    assert all(slot.available is False for slot in result.available_slots)
    assert 0 < len(result.suggested_alternatives) <= 3
    # Alternatives fall inside the 09:00–17:00 business window.
    for slot in result.suggested_alternatives:
        start = datetime.fromisoformat(slot.start)
        assert 9 <= start.hour < 17
        assert start.weekday() < 5  # Mon–Fri


async def test_proposed_time_uses_duration_window():
    # A 30-min busy block at 10:00 collides with a 60-min meeting starting 09:30.
    reader = FakeCalendarReader(
        busy=[(WED.replace(hour=10), WED.replace(hour=10, minute=30))]
    )
    result = await check_availability(
        calendar_reader=reader,
        owner_user_id="rep-1",
        workspace_id="ws-1",
        proposed_times=[WED.replace(hour=9, minute=30).isoformat()],
        duration_minutes=60,
    )
    assert result.available_slots[0].available is False
    assert result.all_busy is True


# ===========================================================================
# Free-slot math — weekends, business hours, all-day blocking
# ===========================================================================


def test_free_slots_skip_weekends():
    # Search spanning Sat+Sun only → no slots (weekends are not business days).
    saturday = _dt(2026, 6, 20)  # Saturday
    sunday = _dt(2026, 6, 21, 23)  # Sunday
    slots = free_slots_from_events([], saturday, sunday, 30, max_slots=3)
    assert slots == []


def test_free_slots_stay_in_business_hours():
    start = WED.replace(hour=0)
    end = WED.replace(hour=23, minute=59)
    slots = free_slots_from_events([], start, end, 30, max_slots=5)
    assert slots, "expected free slots on an empty business day"
    for slot in slots:
        begin = datetime.fromisoformat(slot.start)
        finish = datetime.fromisoformat(slot.end)
        assert begin.hour >= 9
        assert finish.hour <= 17


def test_all_day_event_blocks_whole_day():
    # An all-day event (even with a narrow stored start/end) blocks the full day.
    all_day = CalendarEvent(
        event_id="e1",
        title="Company offsite",
        start=WED.replace(hour=10),
        end=WED.replace(hour=10, minute=30),
        is_all_day=True,
    )
    same_day = free_slots_from_events([all_day], WED, WED.replace(hour=23), 30, 3)
    assert same_day == []

    # The next business day is untouched.
    thursday = WED + timedelta(days=1)
    next_day = free_slots_from_events(
        [all_day], thursday, thursday.replace(hour=23), 30, 3
    )
    assert next_day


async def test_fake_reader_get_events_filters_by_window():
    inside = CalendarEvent("a", "in", WED.replace(hour=10), WED.replace(hour=11))
    outside = CalendarEvent("b", "out", WED.replace(hour=20), WED.replace(hour=21))
    reader = FakeCalendarReader(events=[inside, outside])
    events = await reader.get_events(
        "rep-1", WED.replace(hour=9), WED.replace(hour=12)
    )
    assert [e.event_id for e in events] == ["a"]


# ===========================================================================
# build_schedule_payload — pure, JSON-safe, no write
# ===========================================================================


def test_build_schedule_payload_is_json_safe():
    slot = TimeSlot(
        start=WED.replace(hour=10).isoformat(),
        end=WED.replace(hour=10, minute=30).isoformat(),
        available=True,
    )
    payload = build_schedule_payload(
        slot=slot,
        participants=[{"email": "buyer@acme.com"}],
        title="Follow-up call",
    )
    assert payload["startsAt"] == slot.start
    assert payload["isFullDay"] is False
    json.dumps(payload)  # must not raise


# ===========================================================================
# Read-only guarantee — grep the module source
# ===========================================================================


def test_module_never_writes():
    calendar_dir = Path(__file__).resolve().parents[1] / "followup" / "calendar"
    for source in calendar_dir.glob("*.py"):
        text = source.read_text()
        assert "WRITER_SCOPE" not in text, source
        assert "create_calendar_event" not in text, source
        assert "update_calendar_event" not in text, source
