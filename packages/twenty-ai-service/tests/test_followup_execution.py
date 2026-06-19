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
    """Patch the orchestrator write seam; capture the instruction + handle map.

    Also stubs the bridge link write so the executor's deterministic
    note/task→target linking is captured (under ``calls["links"]``) instead of
    hitting the network.
    """
    calls: dict = {"links": []}

    async def fake_delegate(instruction, *, pii_map=None, session_id=None, model=None, **_kwargs):
        calls["instruction"] = instruction
        calls["pii_map"] = pii_map
        return result

    async def fake_link(tool, args):
        calls["links"].append((tool, args))
        return {"id": "link-row"}

    import agent.orchestrator as orch
    import followup.api.execution as execmod

    monkeypatch.setattr(orch, "delegate_write", fake_delegate)
    monkeypatch.setattr(execmod, "_direct_bridge_write", fake_link)
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

    def test_resolved_targets_covers_every_entity_including_people(self):
        # The executor links the new note/task to every resolved target — one join
        # row per entity (opportunity + company + every contact PERSON). Persons
        # used to be dropped entirely.
        ex = FollowupActionExecutor()
        action = _action("escalate", payload={"company_id": "co-1"})
        action.opportunity_id = "opp-1"
        rows = ex._resolved_targets(action, [
            {"type": "opportunity", "id": "opp-1", "name": "Deal"},
            {"type": "company", "id": "co-1", "name": "Acme"},
            {"type": "person", "id": "p-1", "name": "Ibrahim"},
            {"type": "person", "id": "p-2", "name": "Sara"},
        ])
        assert {(r["field"], r["id"]) for r in rows} == {
            ("targetOpportunityId", "opp-1"),
            ("targetCompanyId", "co-1"),
            ("targetPersonId", "p-1"),
            ("targetPersonId", "p-2"),
        }
        # one join row per target, no duplicates
        assert len(rows) == 4

    def test_write_note_instruction_does_not_ask_writer_to_link(self):
        # Linking is the executor's deterministic job now — the writer is told NOT
        # to link, so it can't loop on create_*_target discovery (which once blew
        # the model's context window).
        from agent.masking import EntityHandleMap

        ex = FollowupActionExecutor()
        action = _action("escalate", payload={"task_results": {"write_note": {"body": "x"}}})
        instruction = ex._build_instruction("write_note", _step("write_note"), action, EntityHandleMap())
        assert "create_note_target" not in instruction
        assert "link" in instruction.lower()  # explicitly told NOT to link

    def test_resolved_targets_dedupes_and_falls_back_without_targets(self):
        # Legacy actions carry no targets list — still link opp + company.
        ex = FollowupActionExecutor()
        action = _action("escalate", payload={"company_id": "co-1"})
        action.opportunity_id = "opp-1"
        rows = ex._resolved_targets(action, None)
        ids = {r["id"] for r in rows}
        assert ids == {"opp-1", "co-1"}


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
        # The executor links the created records to the opportunity deterministically.
        assert any(a.get("targetOpportunityId") == str(action.opportunity_id)
                   for _t, a in calls["links"])

    @pytest.mark.asyncio
    async def test_note_is_linked_deterministically_even_if_llm_skipped_it(self, monkeypatch):
        # The writer created the note but issued NO create_note_target call — the
        # executor must link it to opportunity + company + person on its own.
        calls = _patch_seam(monkeypatch, _ok_writer_result("create_note"))
        action = _plan_action(
            [_step("write_note")],
            company_id="co-1",
            task_results={"write_note": {"body": "x", "targets": [
                {"type": "person", "id": "p-1", "name": "Lisa"},
            ]}},
        )
        action.opportunity_id = "opp-1"
        result = await FollowupActionExecutor().execute(action)
        assert result == {"status": "completed"}
        linked = {(t, tuple(a.items())) for t, a in calls["links"]}
        # the created note id comes from _ok_writer_result → data.result.id == "new-id"
        assert ("create_note_target", (("noteId", "new-id"), ("targetOpportunityId", "opp-1"))) in linked
        assert ("create_note_target", (("noteId", "new-id"), ("targetCompanyId", "co-1"))) in linked
        assert ("create_note_target", (("noteId", "new-id"), ("targetPersonId", "p-1"))) in linked

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
