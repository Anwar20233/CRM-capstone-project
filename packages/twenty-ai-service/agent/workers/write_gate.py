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

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from agent.stubs.safety_tools import _ACTION_TIER_MAP, _as_number, _check_conflicts
from agent.tool_scope import is_write_tool

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Conflict-check node — deterministic data guardrail, no LLM, no interrupt.
# ---------------------------------------------------------------------------
# Runs before tools for ANY write call. Catches bad data the LLM would
# otherwise push straight to the bridge: a close date resolved to a past year,
# OR an order-of-magnitude value change / stage regression on an UPDATE. For
# updates it reads the record's current state (reader-scoped) so it can compare
# proposed-vs-current — the writer itself has no read access. A conflict is a
# hard block: we inject a rejection ToolMessage naming the problem and route
# back to the LLM. This does NOT rely on the writer's system prompt or the model
# behaving — it is structural enforcement inside the graph.

# update_<object> / advance_deal_stage → the object to read current state from.
_ACTION_OBJECT: dict[str, str] = {
    "update_person": "person",
    "update_company": "company",
    "update_opportunity": "opportunity",
    "update_task": "task",
    "update_note": "note",
    "advance_deal_stage": "opportunity",
}


def _scalarize(value: Any) -> Any:
    """Reduce a Twenty composite to a comparable scalar.

    Currency/number fields cross the bridge as ``{amountMicros, currencyCode}``;
    the magnitude rule needs a number. Pull ``amountMicros`` (or ``amount``) out
    of a dict so current and proposed compare in the same unit; pass scalars
    through unchanged.
    """
    if isinstance(value, dict):
        for key in ("amountMicros", "amount", "value"):
            if key in value:
                return value[key]
    return value


class _ReadFailed(Exception):
    """Current-state read did not return the record (bridge/role/permission)."""


async def _fetch_current_record(action: str, record_id: str) -> dict[str, Any]:
    """Read the record being updated so conflicts compare against real state.

    Uses the WRITER's identity, not the reader role: the writer is about to
    update this record, so its role can certainly read it — this avoids silently
    losing value-change protection when the separate reader role is unconfigured
    or unpermitted in a deployment.

    Raises ``_ReadFailed`` when the record cannot be read, so the caller decides
    how to react (it must NOT silently allow an unverifiable value change).
    Returns ``{}`` only when the action has no readable object mapping.
    """
    object_name = _ACTION_OBJECT.get(action)
    if not object_name:
        return {}

    from bridge_client import forward
    from agent.crm_tools import _identity
    from agent.tool_scope import WRITER_SCOPE

    try:
        ident = _identity(WRITER_SCOPE)
        result = await forward(
            "execute",
            {
                "tool": f"find_one_{object_name}",
                "args": {"id": record_id},
                "workspaceId": ident["workspace_id"],
                "roleId": ident["role_id"],
                "userId": ident["user_id"],
            },
        )
    except Exception as error:  # noqa: BLE001
        raise _ReadFailed(str(error)) from error

    if not result.get("ok"):
        message = (result.get("error") or {}).get("message", "read returned not-ok")
        raise _ReadFailed(message)

    data = result.get("data")
    # find_one returns either the record directly or wrapped under its name.
    if isinstance(data, dict):
        inner = data.get(object_name)
        return inner if isinstance(inner, dict) else data
    raise _ReadFailed("read returned no record data")


async def _pipeline_stage_values() -> list[str] | None:
    """Ordered stage values for opportunity stage-regression checks (or None)."""
    try:
        from agent.tools.composite_reads import _get_pipeline_stages
        from agent.tool_scope import READER_SCOPE

        _get_pipeline_stages._scope = READER_SCOPE  # type: ignore[attr-defined]
        result = await _get_pipeline_stages()
    except Exception as error:  # noqa: BLE001
        logger.warning("conflict guard: pipeline-stage read failed: %s", error)
        return None
    stages = (result.get("data") or {}).get("stages") or []
    ordered = sorted(stages, key=lambda s: s.get("position", 0))
    return [s.get("value") for s in ordered if s.get("value")] or None


def _is_numeric(value: Any) -> bool:
    return _as_number(_scalarize(value)) is not None


async def _conflicts_for_tc(tc: dict) -> list[dict[str, Any]]:
    """Run the deterministic conflict rules over one execute_tool write call.

    For updates, reads current state so magnitude/stage-regression rules have a
    baseline; creates have no baseline (only value-free rules apply).

    If the current-state read FAILS on an update that changes a numeric or stage
    field, the change cannot be verified safe — we block it loudly (an
    ``unverified_change`` conflict) rather than letting it through silently. A
    read failure on a non-value-sensitive edit (text/name) is allowed; only
    value-free rules (past-date) run on it.
    """
    action = _crm_action_from_tc(tc)
    if not action or not is_write_tool(action):
        return []
    tool_args = tc.get("args", {}).get("tool_args") or {}
    fields = {key: value for key, value in tool_args.items() if key != "id"}
    if not fields:
        return []

    record_id = tool_args.get("id")
    read_failed = False
    current: dict[str, Any] = {}
    if record_id:
        try:
            current = await _fetch_current_record(action, record_id)
        except _ReadFailed as error:
            read_failed = True
            logger.warning(
                "conflict guard: could not read current state for %s (%s) — "
                "value changes will be blocked unverified",
                action,
                error,
            )

    value_sensitive_fields = ("stage", "deal_stage", "pipelineStage")
    if read_failed:
        # Block any numeric / stage change we could not verify; let safe edits and
        # value-free rules proceed.
        unverified = [
            {
                "field": key,
                "type": "unverified_change",
                "detail": (
                    f"could not read the current value of '{key}' to verify this "
                    "change is safe"
                ),
            }
            for key, value in fields.items()
            if _is_numeric(value) or key in value_sensitive_fields
        ]
        past_date = await _check_conflicts(
            [{"field": k, "current_value": None, "proposed_value": v} for k, v in fields.items()]
        )
        return unverified + ((past_date.get("data") or {}).get("conflicts") or [])

    proposed_writes = [
        {
            "field": key,
            "current_value": _scalarize(current.get(key)),
            "proposed_value": _scalarize(value),
        }
        for key, value in fields.items()
    ]

    # Only pay for the stage-metadata read when a stage field is in play.
    pipeline_stages = None
    if any(write["field"] in value_sensitive_fields for write in proposed_writes):
        pipeline_stages = await _pipeline_stage_values()

    result = await _check_conflicts(proposed_writes, pipeline_stages)
    return (result.get("data") or {}).get("conflicts") or []


async def conflict_check_node(state: dict) -> dict:
    """Block writes that fail the deterministic conflict rules.

    Returns ``{}`` when every write is clean (graph proceeds to write_gate /
    tools). When a conflict is found, returns rejection ToolMessages for every
    pending tool_call so the LLM sees a ``WRITE_BLOCKED`` error and must fix the
    value — the bad write never reaches the bridge.
    """
    messages = state.get("messages", [])
    if not messages:
        return {}
    last_msg = messages[-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {}

    blocked: dict[str, list[dict[str, Any]]] = {}
    for tc in last_msg.tool_calls:
        conflicts = await _conflicts_for_tc(tc)
        if conflicts:
            blocked[tc["id"]] = conflicts

    if not blocked:
        return {}  # All clean — proceed to the tier-3 gate / tools.

    rejections: list[ToolMessage] = []
    for tc in last_msg.tool_calls:
        conflicts = blocked.get(tc["id"])
        if conflicts:
            detail = "; ".join(c.get("detail", c.get("type", "")) for c in conflicts)
            content = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "WRITE_BLOCKED",
                        "message": (
                            f"Write blocked — {detail}. This looks like bad data; "
                            "correct the value (e.g. use the intended future date) "
                            "and retry."
                        ),
                        "conflicts": conflicts,
                    },
                }
            )
        else:
            content = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CANCELLED",
                        "message": "Cancelled because a related write was blocked.",
                    },
                }
            )
        rejections.append(ToolMessage(content=content, tool_call_id=tc["id"]))

    return {"messages": rejections}


def has_blocking_conflict(state: dict) -> bool:
    """True if the conflict_check node injected a WRITE_BLOCKED rejection.

    Used by the graph router to send the turn back to the LLM (to correct the
    value) instead of forward to tools.
    """
    messages = state.get("messages", [])
    if not messages:
        return False
    last_msg = messages[-1]
    return isinstance(last_msg, ToolMessage) and "WRITE_BLOCKED" in (last_msg.content or "")
