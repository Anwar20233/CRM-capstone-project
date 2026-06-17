"""Unit tests for the Step 7 execution layer (followup/api/execution.py).

Record writes (note/task/stage) go through the orchestrator seam
(``orchestrator.delegate_write``); email/calendar go direct. These tests stub the
seam + the direct path so nothing hits an LLM or the bridge, and assert:

* email/calendar build direct send args; record writes hide their content behind
  a handle (the writer LLM never sees the real content),
* execution status is read back from the writer's executed ``execute_tool`` calls,
* a completed plan is logged as a CHECK-constraint-valid commitment fact.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from followup.api.execution import FollowupActionExecutor, _action_to_fact


def _action(action_type, *, payload=None, draft=None, reasoning=None):
    return SimpleNamespace(
        action_type=action_type,
        opportunity_id=uuid.uuid4(),
        action_payload=payload or {},
        draft_result=draft or {},
        reasoning=reasoning,
    )


def _patch_seam(monkeypatch, result):
    """Patch the orchestrator write seam; capture the instruction + handle map."""
    calls: dict = {}

    async def fake_delegate(instruction, *, pii_map=None, session_id=None, model=None):
        calls["instruction"] = instruction
        calls["pii_map"] = pii_map
        return result

    import agent.orchestrator as orch

    monkeypatch.setattr(orch, "delegate_write", fake_delegate)
    return calls


def _ok_writer_result(tool):
    return {
        "type": "response",
        "response": "Done.",
        "tool_calls": [
            {"name": "get_tool_catalog", "args": {}, "result": {"ok": True}},
            {
                "name": "execute_tool",
                "args": {"tool": tool, "tool_args": {}},
                "result": {"ok": True, "data": {"result": {"id": "new-id"}}},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Instruction building
# ---------------------------------------------------------------------------


def _step(kind, intent="do the thing"):
    return {"kind": kind, "intent": intent, "priority": "medium"}


class TestDirectSenders:
    """Email + calendar carry finished content → direct tool call, no LLM."""

    def test_draft_email_builds_send_email_args(self):
        ex = FollowupActionExecutor()
        action = _action(
            "follow_up_call",
            payload={"draft": {"recipient_email": "jane@acme.com", "subject": "Next steps", "body": "Hi\nJane"}},
        )
        tool, args = ex._direct_draft_email(_step("draft_email"), action)
        assert tool == "send_email"
        assert args["recipients"] == {"to": "jane@acme.com"}
        assert args["subject"] == "Next steps"
        assert args["body"] == "Hi<br>Jane"  # newlines → HTML

    def test_draft_email_without_recipient_returns_none(self):
        ex = FollowupActionExecutor()
        action = _action("follow_up_call", payload={"draft": {"body": "hi"}})
        assert ex._direct_draft_email(_step("draft_email"), action) is None

    def test_book_meeting_builds_calendar_event_args(self):
        ex = FollowupActionExecutor()
        action = _action(
            "schedule_meeting",
            payload={"calendar": {"available_slots": [
                {"start": "s1", "end": "e1", "available": False},
                {"start": "s2", "end": "e2", "available": True},
            ]}},
        )
        tool, args = ex._direct_book_meeting(_step("book_meeting"), action)
        assert tool == "create_calendar_event"
        assert args["startsAt"] == "s2" and args["endsAt"] == "e2"

    def test_book_meeting_without_slot_returns_none(self):
        ex = FollowupActionExecutor()
        action = _action("schedule_meeting", payload={"calendar": {"available_slots": []}})
        assert ex._direct_book_meeting(_step("book_meeting"), action) is None


class TestWriterInstructionBuilding:
    """Record writes hide their content behind a handle; the real id stays visible."""

    def test_write_note_hides_body_keeps_real_id(self):
        from agent.masking import EntityHandleMap

        ex = FollowupActionExecutor()
        m = EntityHandleMap()
        action = _action("escalate", payload={"task_results": {"write_note": {"body": "Wants pricing for Acme"}}})
        instruction = ex._build_instruction("write_note", _step("write_note"), action, m)
        assert str(action.opportunity_id) in instruction       # real id, visible
        assert "Wants pricing for Acme" not in instruction      # content hidden
        # the handle in the instruction unmasks back to the real body at execute
        assert "Wants pricing for Acme" in m.unmask_text(instruction)

    def test_create_task_hides_title(self):
        from agent.masking import EntityHandleMap

        ex = FollowupActionExecutor()
        m = EntityHandleMap()
        action = _action("escalate")
        instruction = ex._build_instruction("create_task", _step("create_task", "Chase the SOW"), action, m)
        assert "Chase the SOW" not in instruction
        assert "Chase the SOW" in m.unmask_text(instruction)

    def test_email_and_calendar_are_not_writer_instructions(self):
        from agent.masking import EntityHandleMap

        ex = FollowupActionExecutor()
        m = EntityHandleMap()
        assert ex._build_instruction("draft_email", _step("draft_email"), _action("x"), m) is None
        assert ex._build_instruction("book_meeting", _step("book_meeting"), _action("x"), m) is None


# ---------------------------------------------------------------------------
# Execution + status detection
# ---------------------------------------------------------------------------


def _plan_action(steps, **payload):
    payload["steps"] = steps
    return _action("escalate", payload=payload)


def _patch_direct(monkeypatch, *, ok=True):
    """Patch the direct-write path (send_email / create_calendar_event)."""
    calls: list = []

    async def fake_direct(self, tool, args):
        calls.append((tool, args))
        if not ok:
            raise RuntimeError("bridge send failed")
        return {"result": {"id": "x"}}

    monkeypatch.setattr(FollowupActionExecutor, "_direct_write", fake_direct)
    return calls


class TestExecute:
    @pytest.mark.asyncio
    async def test_completed_when_all_record_writes_ok(self, monkeypatch):
        calls = _patch_seam(monkeypatch, _ok_writer_result("create_note"))
        action = _plan_action(
            [_step("write_note"), _step("create_task")],
            task_results={"write_note": {"body": "x"}},
        )
        result = await FollowupActionExecutor().execute(action)
        assert result == {"status": "completed"}
        assert str(action.opportunity_id) in calls["instruction"]

    @pytest.mark.asyncio
    async def test_email_step_sends_directly_not_via_writer(self, monkeypatch):
        calls = _patch_direct(monkeypatch, ok=True)
        # The seam would raise if called — proving the email path skips the writer.
        async def _boom(*a, **k):
            raise AssertionError("writer seam must not be used for email")
        import agent.orchestrator as orch
        monkeypatch.setattr(orch, "delegate_write", _boom)

        action = _plan_action(
            [_step("draft_email")],
            draft={"recipient_email": "jane@acme.com", "subject": "S", "body": "B"},
        )
        result = await FollowupActionExecutor().execute(action)
        assert result == {"status": "completed"}
        assert calls and calls[0][0] == "send_email"

    @pytest.mark.asyncio
    async def test_book_meeting_sends_directly(self, monkeypatch):
        calls = _patch_direct(monkeypatch, ok=True)
        action = _plan_action(
            [_step("book_meeting")],
            calendar={"available_slots": [{"start": "s", "end": "e", "available": True}]},
        )
        result = await FollowupActionExecutor().execute(action)
        assert result == {"status": "completed"}
        assert calls[0][0] == "create_calendar_event"

    @pytest.mark.asyncio
    async def test_direct_send_failure_is_reported(self, monkeypatch):
        _patch_direct(monkeypatch, ok=False)
        action = _plan_action(
            [_step("draft_email")],
            draft={"recipient_email": "jane@acme.com", "subject": "S", "body": "B"},
        )
        result = await FollowupActionExecutor().execute(action)
        assert result["status"] == "failed"
        assert "bridge send failed" in result["error"]

    @pytest.mark.asyncio
    async def test_failed_when_a_step_write_errors(self, monkeypatch):
        bad = {
            "type": "response",
            "tool_calls": [{
                "name": "execute_tool",
                "args": {"tool": "create_note", "tool_args": {}},
                "result": {"ok": False, "error": {"message": "bad field"}},
            }],
        }
        _patch_seam(monkeypatch, bad)
        action = _plan_action([_step("write_note")], task_results={"write_note": {"body": "x"}})
        result = await FollowupActionExecutor().execute(action)
        assert result["status"] == "failed"
        assert "bad field" in result["error"]

    @pytest.mark.asyncio
    async def test_failed_when_no_write_executed(self, monkeypatch):
        _patch_seam(monkeypatch, {"type": "response", "tool_calls": []})
        action = _plan_action([_step("write_note")], task_results={"write_note": {"body": "x"}})
        result = await FollowupActionExecutor().execute(action)
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_partial_completion_is_failed_with_detail(self, monkeypatch):
        # First step (write_note) writes ok; second (draft_email) has no draft → skipped.
        _patch_seam(monkeypatch, _ok_writer_result("create_note"))
        action = _plan_action(
            [_step("write_note"), _step("draft_email")],
            task_results={"write_note": {"body": "x"}},  # no "draft" → draft_email yields no instruction
        )
        result = await FollowupActionExecutor().execute(action)
        assert result["status"] == "failed"
        assert "1/2 steps completed" in result["error"]

    @pytest.mark.asyncio
    async def test_interrupt_is_surfaced_as_failure(self, monkeypatch):
        _patch_seam(monkeypatch, {"type": "interrupt", "interrupt": {}, "thread_id": "t"})
        action = _plan_action([_step("create_task")])
        result = await FollowupActionExecutor().execute(action)
        assert result["status"] == "failed"
        assert "approval" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_legacy_action_without_steps_falls_back(self, monkeypatch):
        # A pending action persisted before the plan model: action_type only.
        _patch_seam(monkeypatch, _ok_writer_result("create_note"))
        action = _action("add_note", payload={"task_results": {"write_note": {"body": "x"}}})
        result = await FollowupActionExecutor().execute(action)
        assert result == {"status": "completed"}

    @pytest.mark.asyncio
    async def test_no_steps_and_unmapped_action_fails(self, monkeypatch):
        _patch_seam(monkeypatch, _ok_writer_result("create_note"))
        result = await FollowupActionExecutor().execute(_action("no_action"))
        assert result["status"] == "failed"
        assert "No executable steps" in result["error"]


# ---------------------------------------------------------------------------
# Commitment fact
# ---------------------------------------------------------------------------


class TestActionToFact:
    def test_source_type_is_check_constraint_valid(self):
        # profile_facts CHECK allows email|note|crm_record|meeting|risk_score —
        # 'agent_action' would violate it and silently drop the fact.
        action = _plan_action([_step("draft_email")])
        fact = _action_to_fact(action)
        assert fact["source_type"] == "crm_record"
        assert fact["fact_type"] == "commitment"
        assert fact["entity_type"] == "opportunity"
        # profile_facts_entity_ref CHECK: entity_crm_id OR shadow_entity_id must
        # be set, else the insert is rejected and the fact silently dropped.
        assert fact["entity_crm_id"] == action.opportunity_id

    def test_fact_value_summarizes_the_plan(self):
        fact = _action_to_fact(_plan_action([_step("draft_email"), _step("write_note")]))
        value = fact["fact_value"].lower()
        assert "email" in value and "note" in value
