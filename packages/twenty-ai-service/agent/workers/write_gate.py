"""Write-gate node — intercepts tier-3 write tool calls for human approval.

Inserted between the LLM node and ToolNode in the writer's StateGraph. Flow:

1. Inspects the last AIMessage for tier-3 write calls issued via ``execute_tool``.
2. If found, calls ``interrupt()`` — LangGraph truly pauses execution,
   checkpoints state, and returns control to the API layer.
3. Resume with ``approved=True``  → returns {} so ToolNode runs normally.
4. Resume with ``approved=False`` → injects ToolMessages rejecting every
   pending call so the message list stays valid, then routes back to the LLM.

The LLM never manages the confirmation loop. It either sees a normal tool
result (approved) or a rejection envelope (denied) — the gate is invisible.

Important: the writer uses the meta-tool pattern, so the LLM always calls
``execute_tool(tool="delete_company", tool_args={...})`` — the tool_call name
is always ``"execute_tool"``, never the CRM action directly. The gate must look
inside the args to find the real action.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from agent.stubs.safety_tools import _ACTION_TIER_MAP


def _crm_action_from_tc(tc: dict) -> str | None:
    """Return the real CRM action from an execute_tool call, or None.

    The writer always calls execute_tool(tool="<action>", tool_args={...}).
    Other meta-tool calls (get_tool_catalog, learn_tools) are not writes.
    """
    if tc.get("name") == "execute_tool":
        return tc.get("args", {}).get("tool")
    return None


def _tier_for(action: str) -> int:
    """Synchronous tier lookup — reads the static map, no I/O needed."""
    entry = _ACTION_TIER_MAP.get(action)
    return entry["tier"] if entry else 3  # unknown actions default to tier 3


def _action_summary(action: str, args: dict[str, Any]) -> str:
    """Short human-readable label shown in the approval UI."""
    tool_args = args.get("tool_args") or {}
    id_val = (
        tool_args.get("id")
        or tool_args.get("dealId")
        or tool_args.get("entityId")
        or ""
    )
    id_str = f" (id: {id_val})" if id_val else ""
    return f"{action}{id_str}"


def build_write_gate_node(auto_approve: bool = False):
    """Return a write-gate node.

    When ``auto_approve`` is set, the caller has already obtained the user's
    approval (e.g. a follow-up action the rep explicitly accepted), so the gate
    never interrupts — it always passes through. This is required when the writer
    runs nested inside another graph (the follow-up accept pipeline), where a
    LangGraph ``interrupt()`` would propagate up as an exception instead of a
    resumable pause.
    """

    def write_gate_node(state: dict) -> dict:
        if auto_approve:
            return {}  # pre-approved — let ToolNode execute every call.
        return _gate(state)

    return write_gate_node


def write_gate_node(state: dict) -> dict:
    """LangGraph node: gate tier-3 write calls behind a human interrupt.

    Returns an empty dict (no state change) to let ToolNode execute, OR
    injects rejection ToolMessages and returns them so the loop stays valid.
    """
    return _gate(state)


def _gate(state: dict) -> dict:
    messages = state.get("messages", [])
    if not messages:
        return {}

    last_msg = messages[-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {}

    # Find the first tier-3 CRM action — look inside execute_tool args.
    gated: dict | None = None
    gated_action: str = ""
    for tc in last_msg.tool_calls:
        action = _crm_action_from_tc(tc)
        if action and _tier_for(action) >= 3:
            gated = tc
            gated_action = action
            break

    if gated is None:
        return {}  # Nothing to gate — ToolNode runs all calls immediately.

    # TRUE pause: LangGraph checkpoints here and returns control to the caller.
    # On Command(resume=True/False) the node re-runs and interrupt() returns
    # the resume value instead of raising.
    approved: bool = interrupt(
        {
            "type": "write_confirmation",
            "action": gated_action,
            "args": gated.get("args", {}).get("tool_args", {}),
            "summary": _action_summary(gated_action, gated.get("args", {})),
        }
    )

    if approved:
        return {}  # Gate passed — ToolNode will execute all tool calls.

    # Rejected — inject a ToolMessage for EVERY pending tool_call_id so the
    # conversation stays structurally valid (LangGraph requires a response for
    # each tool_call_id that appears in an AIMessage).
    rejections: list[ToolMessage] = []
    for tc in last_msg.tool_calls:
        if tc["id"] == gated["id"]:
            content = (
                '{"ok": false, "error": {'
                '"code": "USER_REJECTED", '
                '"message": "Action rejected by user."}}'
            )
        else:
            content = (
                '{"ok": false, "error": {'
                '"code": "CANCELLED", '
                '"message": "Cancelled because a related action was rejected."}}'
            )
        rejections.append(ToolMessage(content=content, tool_call_id=tc["id"]))

    return {"messages": rejections}
