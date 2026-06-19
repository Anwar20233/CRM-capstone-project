"""Unit tests for the writer's deterministic note/task → target link guard.

The guard (``agent/workers/auto_link.reconcile_targets``) runs after the writer
finishes a turn and guarantees that a note/task it created links to the entities
the instruction named — even when the LLM forgot the ``create_*_target`` calls.
These tests stub the bridge so nothing hits the network, and assert:

* a created note links to a person the instruction named (the "add note to
  <person>" case that used to float),
* entities NOT named in this instruction (but resolved earlier) are not linked,
* links the writer already made are not duplicated.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.masking import EntityHandleMap
from agent.workers import auto_link


def _resolved_map(*records):
    """A handle map with the given ``(entity_type, id, name)`` records resolved."""
    m = EntityHandleMap()
    for entity_type, record_id, name in records:
        m.register_resolved(entity_type, {"id": record_id, "name": name})
    return m


def _created_note_messages(instruction, note_id="note-1", target_calls=None):
    """A writer transcript: instruction → create_note (ok) [→ optional targets]."""
    messages = [HumanMessage(content=instruction)]
    create = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "execute_tool",
                     "args": {"tool": "create_note", "tool_args": {"title": "n"}}}],
    )
    messages.append(create)
    # Real create envelope shape: data.result.id (record-crud create service).
    messages.append(ToolMessage(
        content='{"ok": true, "data": {"success": true, "result": {"id": "%s"}}}' % note_id,
        tool_call_id="c1"))
    for index, args in enumerate(target_calls or []):
        tc_id = f"t{index}"
        messages.append(AIMessage(content="", tool_calls=[
            {"id": tc_id, "name": "execute_tool",
             "args": {"tool": "create_note_target", "tool_args": args}}]))
        messages.append(ToolMessage(content='{"ok": true, "data": {"id": "row"}}',
                                    tool_call_id=tc_id))
    return messages


def _patch_bridge(monkeypatch):
    """Capture bridge ``execute`` calls; return the list of (tool, args)."""
    calls: list = []

    async def fake_forward(path, payload):
        calls.append((payload.get("tool"), payload.get("args")))
        return {"ok": True, "data": {"id": "row"}}

    monkeypatch.setattr("bridge_client.forward", fake_forward)
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("TWENTY_USER_ID", "user")
    monkeypatch.setenv("TWENTY_WRITER_ROLE_ID", "role")
    return calls


class TestReconcileTargets:
    @pytest.mark.asyncio
    async def test_links_note_to_named_person(self, monkeypatch):
        calls = _patch_bridge(monkeypatch)
        m = _resolved_map(("person", "p-1", "Ibrahim"))
        handle = m.handles[0].name  # e.g. "person001"
        messages = _created_note_messages(f"Create a note for {handle}.")

        linked = await auto_link.reconcile_targets(messages, m)

        assert len(linked) == 1
        tool, args = calls[0]
        assert tool == "create_note_target"
        assert args == {"noteId": "note-1", "targetPersonId": "p-1"}

    @pytest.mark.asyncio
    async def test_ignores_entities_not_named_in_instruction(self, monkeypatch):
        calls = _patch_bridge(monkeypatch)
        # Two resolved entities, but only the person is named in the instruction.
        m = _resolved_map(("person", "p-1", "Ibrahim"), ("company", "co-9", "OldCo"))
        person = m.handle_for_surface("Ibrahim").name
        messages = _created_note_messages(f"Create a note for {person}.")

        await auto_link.reconcile_targets(messages, m)

        linked_ids = {args.get("targetCompanyId") or args.get("targetPersonId") for _, args in calls}
        assert linked_ids == {"p-1"}  # company resolved earlier is NOT linked

    @pytest.mark.asyncio
    async def test_does_not_duplicate_existing_link(self, monkeypatch):
        calls = _patch_bridge(monkeypatch)
        m = _resolved_map(("person", "p-1", "Ibrahim"))
        person = m.handles[0].name
        # The writer already linked the person itself → guard must add nothing.
        messages = _created_note_messages(
            f"Create a note for {person}.",
            target_calls=[{"noteId": "note-1", "targetPersonId": "p-1"}],
        )

        linked = await auto_link.reconcile_targets(messages, m)

        assert linked == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_noop_when_no_resolved_targets_named(self, monkeypatch):
        calls = _patch_bridge(monkeypatch)
        m = _resolved_map(("person", "p-1", "Ibrahim"))
        # Instruction names no handle → nothing to link (follow-up path shape).
        messages = _created_note_messages("Create a generic note.")

        linked = await auto_link.reconcile_targets(messages, m)

        assert linked == []
        assert calls == []
