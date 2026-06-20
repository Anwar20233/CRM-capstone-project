"""Tests for in-place step revision (Strategy A) — ``revise_step_in_place``.

All LLM / drafting / calendar / DB seams are faked, so these run offline. The
focus is the revise contract: a targeted edit preserves the rest of the workflow,
a meeting-date change cascades to dependent artifacts, and a move onto a booked
slot is refused rather than applied.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from followup.calendar.reader import CalendarEvent
from followup.contracts.drafting import DraftResult
from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.revise import revise_step_in_place
from followup.profile.schemas import ContactSummary, DealContext
from followup.store.repositories import PendingAction

WORKSPACE_ID = uuid.uuid4()
OPPORTUNITY_ID = uuid.uuid4()
USER_ID = str(uuid.uuid4())
REQUESTED_TIME = "2026-06-23T14:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _deal_context() -> DealContext:
    return DealContext(
        opportunity_id=str(OPPORTUNITY_ID),
        opportunity_name="Acme Expansion",
        deal_stage="PROPOSAL",
        deal_value=50000.0,
        company_name="Acme",
        profile_narrative="A warm deal with an open pricing concern.",
        contacts=[
            ContactSummary(
                crm_id=str(uuid.uuid4()),
                name="Jane Buyer",
                role="VP",
                email="jane@acme.test",
                facts=[],
            )
        ],
        recent_activities=[],
        key_relationships=[],
        open_concerns=[{"content": "Worried about the Q3 timeline."}],
        risk_score=0.4,
        company_id=str(uuid.uuid4()),
    )


class _FakeChatLLM:
    """Stands in for the content-author LLM: echoes the meeting time if present."""

    async def ainvoke(self, messages: Any) -> SimpleNamespace:
        text = messages[0].content
        marker = "Scheduled meeting time:"
        if marker in text:
            when = text.split(marker, 1)[1].strip().splitlines()[0]
            return SimpleNamespace(content=f"Note referencing meeting at {when}")
        return SimpleNamespace(content="A freshly authored internal note.")


class _FakeCalendarReader:
    """Busy when ``busy`` is set; otherwise every requested slot is free."""

    def __init__(self, busy: bool) -> None:
        self._busy = busy

    async def get_events(self, owner: str, start: datetime, end: datetime) -> list[CalendarEvent]:
        if not self._busy:
            return []
        return [CalendarEvent(event_id="e1", title="Booked", start=start, end=end)]

    async def find_free_slots(self, owner, start, end, duration_minutes, max_slots):
        from followup.calendar.reader import TimeSlot

        return [
            TimeSlot(
                start="2026-06-24T15:00:00+00:00",
                end="2026-06-24T15:30:00+00:00",
                available=True,
            )
        ]


class _FakePendingRepo:
    def __init__(self) -> None:
        self.saved: list[PendingAction] = []

    async def save(self, action: PendingAction) -> PendingAction:
        self.saved.append(action)
        return action


class _FakeDrafting:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def run(self, request: Any) -> DraftResult:
        if self._fail:
            raise RuntimeError("drafting agent boom")
        # Echo the directive so we can assert the rep's instruction reached it.
        return DraftResult(
            opportunity_id=str(OPPORTUNITY_ID),
            subject="Revised follow-up",
            body=f"REVISED BODY :: {request.intent}",
            recipient_email=request.recipient_email,
            tone="professional",
            drafted_at=datetime.now(timezone.utc).isoformat(),
            metadata={"source": "fake"},
        )


def _make_deps(
    *, busy: bool = False, fail_drafting: bool = False
) -> tuple[OrchestratorDeps, _FakePendingRepo]:
    pending_repo = _FakePendingRepo()
    pipeline = SimpleNamespace(
        calendar_reader=_FakeCalendarReader(busy=busy),
        pending_actions=pending_repo,
        get_chat_llm=lambda: _FakeChatLLM(),
    )
    profile_service = SimpleNamespace(
        build_deal_context=lambda opp_id, include_shadows=False: _async(_deal_context())
    )
    agents = SimpleNamespace(drafting=_FakeDrafting(fail=fail_drafting), risk=None, next_step=None)
    deps = OrchestratorDeps(
        pipeline=pipeline,  # type: ignore[arg-type]
        agents=agents,  # type: ignore[arg-type]
        profile_service=profile_service,  # type: ignore[arg-type]
    )
    return deps, pending_repo


async def _async(value: Any) -> Any:
    return value


def _multi_step_action() -> PendingAction:
    steps = [
        {"kind": "draft_email", "intent": "Reassure on the timeline.", "priority": "high"},
        {"kind": "write_note", "intent": "Log the escalation.", "priority": "high"},
        {"kind": "create_task", "intent": "Chase the outstanding items.", "priority": "high"},
    ]
    return PendingAction(
        id=uuid.uuid4(),
        opportunity_id=OPPORTUNITY_ID,
        workspace_id=WORKSPACE_ID,
        trigger_type="email_signal",
        action_type="escalate",
        action_payload={
            "steps": steps,
            "draft": {"subject": "Old", "body": "old body", "recipient_email": "jane@acme.test"},
            "task_results": {
                "write_note": {"body": "old note"},
                "create_task": {"title": "old task"},
            },
        },
        next_step_result={"steps": steps, "headline_action": "escalate", "summary": "3-step plan"},
        draft_result={"subject": "Old", "body": "old body", "recipient_email": "jane@acme.test"},
        status="pending",
    )


def _meeting_action(slot: dict | None = None) -> PendingAction:
    steps = [
        {"kind": "book_meeting", "intent": "Meet about Acme.", "priority": "medium"},
        {"kind": "draft_email", "intent": "Offer the meeting times.", "priority": "medium"},
        {"kind": "write_note", "intent": "Log the booking.", "priority": "low"},
    ]
    available_slots = [slot] if slot else []
    return PendingAction(
        id=uuid.uuid4(),
        opportunity_id=OPPORTUNITY_ID,
        workspace_id=WORKSPACE_ID,
        trigger_type="direct_request",
        action_type="schedule_meeting",
        action_payload={
            "steps": steps,
            "draft": {"subject": "Meet?", "body": "old", "recipient_email": "jane@acme.test"},
            "calendar": {
                "available_slots": available_slots,
                "all_busy": False,
                "suggested_alternatives": [],
            },
            "task_results": {"write_note": {"body": "old note"}},
        },
        next_step_result={"steps": steps, "headline_action": "schedule_meeting", "summary": "meeting"},
        draft_result={"subject": "Meet?", "body": "old", "recipient_email": "jane@acme.test"},
        status="pending",
    )


# A confirmed 30-minute slot already on the action (June 23 09:00–09:30 ET).
_EXISTING_SLOT = {
    "start": "2026-06-23T09:00:00+00:00",
    "end": "2026-06-23T09:30:00+00:00",
    "available": True,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_revise_draft_email_preserves_other_steps() -> None:
    deps, _ = _make_deps()
    action = _multi_step_action()

    result = await revise_step_in_place(
        deps,
        action=action,
        target="draft_email",
        instructions="make it friendlier",
        user_id=USER_ID,
    )

    assert result["status"] == "updated"
    updated = result["action"]
    payload = updated.action_payload
    # The whole workflow is intact.
    assert len(payload["steps"]) == 3
    # Only the email changed; note + task artifacts are untouched.
    assert payload["draft"]["body"].startswith("REVISED BODY")
    assert "make it friendlier" in payload["draft"]["body"]
    assert payload["task_results"]["write_note"] == {"body": "old note"}
    assert payload["task_results"]["create_task"] == {"title": "old task"}
    assert updated.status == "pending"


async def test_revise_meeting_cascades_to_dependents() -> None:
    deps, _ = _make_deps(busy=False)
    action = _meeting_action()

    result = await revise_step_in_place(
        deps,
        action=action,
        target="book_meeting",
        instructions="move it to next Tuesday at 2pm",
        requested_time=REQUESTED_TIME,
        user_id=USER_ID,
    )

    assert result["status"] == "updated"
    payload = result["action"].action_payload
    # The calendar slot was re-derived to the requested (free) time.
    slots = payload["calendar"]["available_slots"]
    assert any(s["start"] == REQUESTED_TIME and s["available"] for s in slots)
    # The dependent draft was regenerated, and the note now references the new date.
    assert payload["draft"]["body"].startswith("REVISED BODY")
    assert REQUESTED_TIME in payload["task_results"]["write_note"]["body"]
    assert len(payload["steps"]) == 3


async def test_revise_meeting_unavailable_refuses() -> None:
    deps, pending_repo = _make_deps(busy=True)
    action = _meeting_action()

    result = await revise_step_in_place(
        deps,
        action=action,
        target="book_meeting",
        instructions="move it to next Tuesday at 2pm",
        requested_time=REQUESTED_TIME,
        user_id=USER_ID,
    )

    assert result["status"] == "unavailable"
    assert result["requested_time"] == REQUESTED_TIME
    assert result["alternatives"]  # free alternatives were offered
    # Nothing was persisted — the action is left unchanged.
    assert pending_repo.saved == []


async def test_expand_duration_keeps_start_and_cascades() -> None:
    # The reported bug: asking to expand 30min -> 1h must KEEP June 23 09:00 and
    # only move the end to 10:00 — never relocate the meeting to a new day.
    deps, _ = _make_deps(busy=False)
    action = _meeting_action(slot=dict(_EXISTING_SLOT))

    result = await revise_step_in_place(
        deps,
        action=action,
        target="book_meeting",
        instructions="30 minutes is not enough, expand it to 1 hour",
        requested_duration_minutes=60,
        user_id=USER_ID,
    )

    assert result["status"] == "updated"
    payload = result["action"].action_payload
    chosen = next(s for s in payload["calendar"]["available_slots"] if s["available"])
    # Start preserved, end extended to a full hour.
    assert chosen["start"] == "2026-06-23T09:00:00+00:00"
    assert chosen["end"] == "2026-06-23T10:00:00+00:00"
    # Cascade: the email and the note were regenerated against the new time.
    assert payload["draft"]["body"].startswith("REVISED BODY")
    assert "2026-06-23T09:00:00+00:00" in payload["task_results"]["write_note"]["body"]


async def test_cascade_gate_aborts_atomically_when_dependent_fails() -> None:
    # If a dependent artifact (the email) cannot be regenerated, NOTHING is
    # persisted — the meeting must never move while the email stays stale.
    deps, pending_repo = _make_deps(busy=False, fail_drafting=True)
    action = _meeting_action(slot=dict(_EXISTING_SLOT))

    result = await revise_step_in_place(
        deps,
        action=action,
        target="book_meeting",
        instructions="expand it to 1 hour",
        requested_duration_minutes=60,
        user_id=USER_ID,
    )

    assert result["status"] == "error"
    assert "draft_email" in result["error"]
    assert pending_repo.saved == []  # all-or-nothing: no partial update


async def test_create_threads_requested_time_as_proposed_times() -> None:
    # The reported bug: a requested meeting date ("June 25") was dropped, so the
    # calendar auto-picked a different day and the email disagreed. A requested
    # time must flow into proposed_times so the slot + email reflect the request.
    from followup.chat.agent import _ChatTools

    captured: dict[str, Any] = {}

    async def _fake_run_pipeline(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(status="completed", pending_action_id="pa-1", error=None)

    tools = _ChatTools(
        deps=SimpleNamespace(),
        accept_graph=None,
        followup_graph=None,
        run_pipeline=_fake_run_pipeline,
        opportunity_id=str(OPPORTUNITY_ID),
        workspace_id=str(WORKSPACE_ID),
        user_id=USER_ID,
    )

    await tools._create(
        "Schedule a 15-minute check-in for June 25",
        "medium",
        False,
        "2026-06-25T13:00:00+00:00",
        15,
    )

    trigger = captured["trigger"]
    assert trigger["proposed_times"] == ["2026-06-25T13:00:00+00:00"]
    assert trigger["duration_minutes"] == 15  # honour the requested length
    assert trigger["wants_meeting"] is True  # a requested time implies a meeting


def test_to_utc_iso_treats_naive_time_as_rep_local() -> None:
    # "1pm" typed by a UTC-5 rep means 1pm local → 18:00 UTC, NOT 1pm UTC.
    from followup.chat.agent import _to_utc_iso

    converted = _to_utc_iso("2026-06-23T13:00:00", "America/New_York")  # EDT = UTC-4
    assert converted == "2026-06-23T17:00:00+00:00"

    # An explicit offset is just normalized to UTC.
    assert _to_utc_iso("2026-06-23T13:00:00-04:00", "America/New_York") == (
        "2026-06-23T17:00:00+00:00"
    )

    # No timezone given → assume UTC (no shift, no regression).
    assert _to_utc_iso("2026-06-23T13:00:00", None) == "2026-06-23T13:00:00+00:00"

    # Unknown zone falls back to UTC rather than raising.
    assert _to_utc_iso("2026-06-23T13:00:00", "Not/AZone") == "2026-06-23T13:00:00+00:00"


@pytest.mark.asyncio
async def test_opportunity_update_writes_grounded_change_directly() -> None:
    # A stage change validated at plan time is written DIRECTLY (no writer LLM):
    # the exact field+value reaches the bridge as an update_opportunity call.
    from followup.api.execution import FollowupActionExecutor

    executor = FollowupActionExecutor()
    writes: list[tuple[str, dict]] = []

    async def _fake_direct_write(tool: str, args: dict) -> dict:
        writes.append((tool, args))
        return {"id": str(OPPORTUNITY_ID)}

    executor._direct_write = _fake_direct_write  # type: ignore[assignment]
    action = SimpleNamespace(
        opportunity_id=OPPORTUNITY_ID,
        action_payload={
            "task_results": {
                "update_stage": {"field": "stage", "value": "PROPOSAL", "valid": True}
            }
        },
        reasoning="",
    )

    status, error = await executor._execute_opportunity_update(
        "update_stage", {"kind": "update_stage", "intent": "Advance to Proposal"}, action
    )
    assert status == "completed"
    assert error is None
    assert writes == [("update_opportunity", {"id": str(OPPORTUNITY_ID), "stage": "PROPOSAL"})]


@pytest.mark.asyncio
async def test_opportunity_update_invalid_change_fails_with_reason_not_silently() -> None:
    # An ungrounded stage (one the pipeline does not have) must FAIL with a reason
    # and write nothing — never the old silent no-op.
    from followup.api.execution import FollowupActionExecutor

    executor = FollowupActionExecutor()
    called: list[str] = []

    async def _fake_direct_write(tool: str, args: dict) -> dict:
        called.append(tool)
        return {}

    executor._direct_write = _fake_direct_write  # type: ignore[assignment]
    action = SimpleNamespace(
        opportunity_id=OPPORTUNITY_ID,
        action_payload={
            "task_results": {
                "update_stage": {
                    "field": "stage",
                    "value": "Negotiation",
                    "valid": False,
                    "reason": "'Negotiation' is not a valid stage. Valid stages: New (NEW)",
                }
            }
        },
        reasoning="",
    )

    status, error = await executor._execute_opportunity_update(
        "update_stage", {"kind": "update_stage", "intent": "move to Negotiation"}, action
    )
    assert status == "failed"
    assert "Negotiation" in error
    assert called == []  # nothing was written


def test_meeting_brief_states_slot_duration() -> None:
    # The email's stated length is derived from the chosen slot, so a 15-minute
    # slot can never be described as a 30-minute meeting (and vice versa).
    from followup.calendar.availability import CalendarResult, TimeSlot
    from followup.orchestrator.authoring import ContentAuthor

    author = ContentAuthor(SimpleNamespace())  # meeting_context never calls the LLM
    calendar = CalendarResult(
        available_slots=[
            TimeSlot(
                start="2026-06-25T09:00:00+00:00",
                end="2026-06-25T09:15:00+00:00",
                available=True,
            )
        ],
        all_busy=False,
        suggested_alternatives=[],
    )
    ctx = SimpleNamespace(deal_context=_deal_context(), calendar=calendar)

    brief = author.meeting_context(ctx)  # type: ignore[arg-type]
    assert "15 minutes" in brief


def test_format_slot_time_is_weekday_correct() -> None:
    # June 25, 2026 is a Thursday — the formatter must say so (the LLM previously
    # hallucinated "Monday June 23"). We hand it the correct string to copy.
    from followup.agents.drafting_adapter import _fmt_slot_time

    assert _fmt_slot_time("2026-06-25T09:00:00+00:00").startswith("Thursday, June 25, 2026")
    assert _fmt_slot_time("2026-06-23T09:00:00+00:00").startswith("Tuesday, June 23, 2026")


def test_fmt_slot_time_renders_in_rep_timezone_not_utc() -> None:
    # A UTC-stored slot must be shown to the recipient in the rep's local time,
    # never as raw "07:00 UTC". 07:00 UTC == 10:00 AM in Asia/Riyadh (UTC+3).
    from followup.agents.drafting_adapter import _fmt_slot_time

    rendered = _fmt_slot_time("2026-06-23T07:00:00+00:00", "Asia/Riyadh")
    assert "10:00 AM" in rendered
    assert "Asia/Riyadh" in rendered
    assert "UTC" not in rendered


async def test_revise_unknown_target_errors() -> None:
    deps, _ = _make_deps()
    action = _multi_step_action()

    result = await revise_step_in_place(
        deps,
        action=action,
        target="book_meeting",  # not present in this 3-step action
        instructions="whatever",
        user_id=USER_ID,
    )
    assert result["status"] == "error"
