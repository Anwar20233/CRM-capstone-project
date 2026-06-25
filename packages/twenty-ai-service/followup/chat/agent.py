"""Tool-using conversational agent for the Follow-Up Intelligence panel.

The agent reasons over the rep's message + recent history and may call tools
that wrap the existing follow-up operations (list / accept / reject / revise
pending actions, read the opportunity health profile, create a new follow-up).
Tool execution reuses the exact same code paths as the structured REST
endpoints so chat-driven actions behave identically to button-driven ones.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

from agent.llm_client import get_chat_model
from followup.api.models import FollowUpRunResult, PendingActionResponse

logger = logging.getLogger(__name__)

# Bound on tool-calling rounds so a confused model can't loop forever.
_MAX_TOOL_ROUNDS = 6


def _safe_zone(tz_name: Optional[str]) -> Any:
    """The rep's IANA timezone, falling back to UTC for missing/unknown names."""
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def _to_utc_iso(value: Optional[str], tz_name: Optional[str]) -> Optional[str]:
    """Convert a chat-supplied time to a UTC ISO-8601 string before it runs.

    The rep means a wall-clock time in THEIR timezone ("1pm" = 1pm local). The LLM
    emits that naive wall-clock; we attach the rep's zone and convert to UTC so the
    calendar (which assumes UTC) books the time the rep actually meant — instead of
    treating 1pm as UTC and shifting it by the offset. A value that already carries
    an explicit offset is just normalized to UTC.
    """
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_safe_zone(tz_name))
    return parsed.astimezone(timezone.utc).isoformat()

# Type of the pipeline runner injected by the route (avoids importing routes.py
# here, which would create a circular import).
RunPipeline = Callable[..., Awaitable[FollowUpRunResult]]

_SYSTEM_PROMPT = """You are the Follow-Up Intelligence assistant for a sales rep, embedded on a CRM opportunity record.
You help the rep manage AI-suggested follow-up actions and answer questions about the deal.

A pending follow-up action is a WORKFLOW: an ordered list of steps (an email draft,
a note, a task, a meeting, a stage change). Each step has an index and a kind
(draft_email | write_note | create_task | book_meeting | update_stage).

You can:
- List this opportunity's pending follow-up actions and their steps.
- Refine ONE step of a workflow in place (revise_step) — e.g. reword an email,
  tighten a note, move a meeting time. The other steps are preserved.
- Re-plan a whole workflow from scratch (replan_followup) when the rep's request
  changes WHICH steps should exist.
- Accept or reject a pending action when the rep asks.
- Read the opportunity health profile to answer questions about the deal.
- Create a new follow-up (e.g. draft an email) when the rep asks for one.

Choosing between revise_step and replan_followup:
- Use **revise_step** for edits to existing content: tone/wording of an email or
  note, a task title, or moving a meeting's time. Always call list_pending_actions
  first to read the workflow's steps, then target the step by its kind (or index).
  This keeps every other step intact.
- Use **replan_followup** ONLY when the request changes the set of steps (e.g.
  "also add a note and schedule a meeting", or "scrap this and just send an email").
- Change ONLY what the rep asked for; never alter a field they did not mention.
  For a meeting: put a new START in `requested_time` (ISO-8601, resolving phrasing
  like "next Tuesday 2pm" against today's date) ONLY if they asked to move it; put
  a new length in `requested_duration_minutes` ONLY if they asked to change the
  duration (e.g. "make it an hour" → 60). To change duration while keeping the
  same day/time, leave `requested_time` empty. The system keeps the existing
  date/time and propagates the change to the email and any note/task automatically.
- If revise_step reports the slot is unavailable, tell the rep it's booked and
  offer the returned alternatives — never claim it was moved.

Guidelines:
- Be concise and concrete. Refer to actions/steps by what they do, not by raw ids.
- Only act when the rep clearly asks; then confirm exactly what you changed.
- When the rep asks to "draft", "write", or "send" something new, use create_followup
  to generate a draft for their review — drafts are never auto-sent.
- If a request is ambiguous, ask one short clarifying question instead of guessing.
"""


# ---------------------------------------------------------------------------
# Tool schemas (passed to bind_tools; dispatched by class name)
# ---------------------------------------------------------------------------


class ListPendingActions(BaseModel):
    """List the current pending follow-up actions for this opportunity."""


class GetOpportunityHealth(BaseModel):
    """Read the opportunity health profile: narrative, key facts, risk score."""


class AcceptAction(BaseModel):
    """Accept a pending action and execute it (e.g. queue/send a drafted email)."""

    action_id: str = Field(description="Id of the pending action to accept.")


class RejectAction(BaseModel):
    """Reject / dismiss a pending action without taking any side effects."""

    action_id: str = Field(description="Id of the pending action to reject.")
    reason: str = Field(default="", description="Optional short reason.")


class ReviseStep(BaseModel):
    """Refine ONE step of a workflow in place, preserving the other steps.

    Use for tone/wording edits or moving a meeting time. Call list_pending_actions
    first to read the workflow's steps.
    """

    action_id: str = Field(description="Id of the pending action (workflow) to edit.")
    target: str = Field(
        description=(
            "Which step to edit: its kind (draft_email | write_note | create_task | "
            "book_meeting | update_stage) or its numeric index in the workflow."
        )
    )
    instructions: str = Field(
        description="What the rep wants changed for this step, in natural language."
    )
    requested_time: str = Field(
        default="",
        description=(
            "For a book_meeting START change only: the new start as an ISO-8601 "
            "timestamp (e.g. 2026-06-23T14:00:00Z). Leave empty to keep the current "
            "date/time (e.g. when only changing the duration)."
        ),
    )
    requested_duration_minutes: int = Field(
        default=0,
        description=(
            "For a book_meeting DURATION change only: the new length in minutes "
            "(e.g. 60 for 'make it an hour'). Leave 0 to keep the current duration."
        ),
    )


class ReplanFollowup(BaseModel):
    """Discard a workflow and re-plan it from scratch.

    Use ONLY when the rep's request changes which steps should exist.
    """

    action_id: str = Field(description="Id of the pending action to replace.")
    instructions: str = Field(
        description="What the new workflow should do, in natural language."
    )


class CreateFollowup(BaseModel):
    """Create a new follow-up draft/action for this opportunity from instructions."""

    instructions: str = Field(
        description="What follow-up to create, in natural language."
    )
    urgency: str = Field(default="medium", description="low | medium | high")
    wants_meeting: bool = Field(
        default=False, description="True if a meeting / calendar slot is wanted."
    )
    requested_time: str = Field(
        default="",
        description=(
            "If the rep named a specific meeting date/time, the requested START as "
            "an ISO-8601 timestamp (resolve relative phrasing against today's date). "
            "This becomes the proposed time the calendar checks — leave empty to let "
            "the agent find a free slot."
        ),
    )
    duration_minutes: int = Field(
        default=0,
        description=(
            "Meeting length in minutes if the rep specified one (e.g. 15 for a "
            "'15-min check-in', 60 for an hour). Leave 0 to use the 30-minute "
            "default. Must match the duration mentioned in the request."
        ),
    )


_TOOLS: list[type[BaseModel]] = [
    ListPendingActions,
    GetOpportunityHealth,
    AcceptAction,
    RejectAction,
    ReviseStep,
    ReplanFollowup,
    CreateFollowup,
]


class FollowupChatResult(BaseModel):
    """Assistant reply plus the refreshed pending-action list for the UI."""

    reply: str
    actions: list[PendingActionResponse]


# ---------------------------------------------------------------------------
# Tool dispatch — each tool reuses an existing operation
# ---------------------------------------------------------------------------


class _ChatTools:
    def __init__(
        self,
        *,
        deps: Any,
        accept_graph: Any,
        followup_graph: Any,
        run_pipeline: RunPipeline,
        opportunity_id: str,
        workspace_id: Optional[str],
        user_id: str,
        tz_name: Optional[str] = None,
    ) -> None:
        self._deps = deps
        self._accept_graph = accept_graph
        self._followup_graph = followup_graph
        self._run_pipeline = run_pipeline
        self._opportunity_id = opportunity_id
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._tz_name = tz_name

    async def list_actions(self) -> list[PendingActionResponse]:
        actions = await self._deps.pipeline.pending_actions.list_pending(
            uuid.UUID(self._opportunity_id), status="pending"
        )
        return [PendingActionResponse.from_db(action) for action in actions]

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "ListPendingActions":
            return [action.model_dump() for action in await self.list_actions()]
        if name == "GetOpportunityHealth":
            return await self._health()
        if name == "AcceptAction":
            return await self._accept(args["action_id"])
        if name == "RejectAction":
            return await self._reject(args["action_id"], args.get("reason", ""))
        if name == "ReviseStep":
            return await self._revise_step(
                args["action_id"],
                args["target"],
                args["instructions"],
                _to_utc_iso(args.get("requested_time") or None, self._tz_name),
                args.get("requested_duration_minutes") or None,
            )
        if name == "ReplanFollowup":
            return await self._replan(args["action_id"], args["instructions"])
        if name == "CreateFollowup":
            return await self._create(
                args["instructions"],
                args.get("urgency", "medium"),
                bool(args.get("wants_meeting", False)),
                _to_utc_iso(args.get("requested_time") or None, self._tz_name),
                args.get("duration_minutes") or None,
            )
        return {"error": f"Unknown tool: {name}"}

    async def _health(self) -> dict[str, Any]:
        from followup.profile.service import ProfileNotFound

        try:
            narrative = await self._deps.profile_service.build_profile_narrative(
                self._opportunity_id
            )
        except ProfileNotFound:
            return {"error": "No profile available for this opportunity yet."}

        return {
            "narrative": narrative.narrative,
            "key_facts": narrative.key_facts,
            "relationships": narrative.relationships,
            "risk_score": narrative.risk_score,
        }

    async def _accept(self, action_id: str) -> dict[str, Any]:
        action = await self._deps.pipeline.pending_actions.get(uuid.UUID(action_id))
        if action is None:
            return {"error": "Action not found."}
        if action.status != "pending":
            return {"error": f"Action is '{action.status}', not 'pending'."}

        await self._accept_graph.ainvoke(
            {
                "action_id": action_id,
                "user_id": self._user_id,
                "workspace_id": str(action.workspace_id),
                "opportunity_id": str(action.opportunity_id),
            }
        )
        updated = await self._deps.pipeline.pending_actions.get(uuid.UUID(action_id))
        return {
            "action_id": action_id,
            "execution_status": (
                updated.execution_status if updated else "unknown"
            ),
        }

    async def _reject(self, action_id: str, reason: str) -> dict[str, Any]:
        action = await self._deps.pipeline.pending_actions.get(uuid.UUID(action_id))
        if action is None:
            return {"error": "Action not found."}
        if action.status != "pending":
            return {"error": f"Action is '{action.status}', not 'pending'."}

        action.status = "rejected"
        action.acted_on_at = datetime.now(timezone.utc)
        action.acted_on_by = uuid.UUID(self._user_id)
        await self._deps.pipeline.pending_actions.save(action)
        return {"action_id": action_id, "status": "rejected", "reason": reason}

    async def _revise_step(
        self,
        action_id: str,
        target: str,
        instructions: str,
        requested_time: Optional[str],
        requested_duration_minutes: Optional[int],
    ) -> dict[str, Any]:
        """Strategy A — edit one step in place, preserving the rest of the workflow."""
        from followup.orchestrator.revise import revise_step_in_place

        action = await self._deps.pipeline.pending_actions.get(uuid.UUID(action_id))
        if action is None:
            return {"error": "Action not found."}
        if action.status != "pending":
            return {"error": f"Action is '{action.status}', not 'pending'."}

        result = await revise_step_in_place(
            self._deps,
            action=action,
            target=target,
            instructions=instructions,
            requested_time=requested_time,
            requested_duration_minutes=requested_duration_minutes,
            user_id=self._user_id,
            tz_name=self._tz_name,
        )
        if result["status"] == "unavailable":
            return {
                "status": "unavailable",
                "requested_time": result.get("requested_time"),
                "alternatives": result.get("alternatives", []),
                "message": "That time is already booked — offer the alternatives instead.",
            }
        if result["status"] == "error":
            return {"error": result.get("error", "revise failed")}
        return {
            "status": "updated",
            "action_id": action_id,
            "target": target,
        }

    async def _replan(self, action_id: str, instructions: str) -> dict[str, Any]:
        """Strategy B — discard the workflow and re-plan from scratch."""
        action = await self._deps.pipeline.pending_actions.get(uuid.UUID(action_id))
        if action is None:
            return {"error": "Action not found."}
        if action.status != "pending":
            return {"error": f"Action is '{action.status}', not 'pending'."}

        action.status = "edited"
        action.acted_on_at = datetime.now(timezone.utc)
        action.acted_on_by = uuid.UUID(self._user_id)
        await self._deps.pipeline.pending_actions.save(action)

        result = await self._run_pipeline(
            deps=self._deps,
            graph=self._followup_graph,
            entry_point="orchestrator",
            trigger={
                "instructions": instructions,
                "previous_action_id": action_id,
                "prior_draft": action.draft_result or {},
                "urgency": action.urgency,
                "owner_user_id": self._user_id,
            },
            opportunity_id=str(action.opportunity_id),
            workspace_id=str(action.workspace_id),
        )
        return {
            "status": result.status,
            "new_action_id": result.pending_action_id,
            "error": result.error,
        }

    async def _create(
        self,
        instructions: str,
        urgency: str,
        wants_meeting: bool,
        requested_time: Optional[str] = None,
        duration_minutes: Optional[int] = None,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id or await self._infer_workspace_id()
        if workspace_id is None:
            return {
                "error": "Could not determine the workspace for this opportunity."
            }

        trigger: dict[str, Any] = {
            "instructions": instructions,
            "urgency": urgency,
            "wants_meeting": wants_meeting or bool(requested_time) or bool(duration_minutes),
            "owner_user_id": self._user_id,
            # Render proposed times in the rep's local timezone in the email.
            "timezone": self._tz_name,
        }
        # The rep named a time → it is the single source of truth the calendar
        # checks, so the booked slot AND the email both reflect the requested date
        # instead of an auto-searched one.
        if requested_time:
            trigger["proposed_times"] = [requested_time]
        # The rep named a length → the booked slot is that long, so the slot and
        # the email agree on duration (no more "15-min call" booked as 30 min).
        if duration_minutes:
            trigger["duration_minutes"] = int(duration_minutes)

        result = await self._run_pipeline(
            deps=self._deps,
            graph=self._followup_graph,
            entry_point="orchestrator",
            trigger=trigger,
            opportunity_id=self._opportunity_id,
            workspace_id=workspace_id,
        )
        return {
            "status": result.status,
            "new_action_id": result.pending_action_id,
            "error": result.error,
        }

    async def _infer_workspace_id(self) -> Optional[str]:
        # Fall back to the workspace of any existing action on this opportunity.
        actions = await self._deps.pipeline.pending_actions.list_pending(
            uuid.UUID(self._opportunity_id), status="pending"
        )
        if actions:
            return str(actions[0].workspace_id)
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_followup_chat(
    *,
    deps: Any,
    accept_graph: Any,
    followup_graph: Any,
    run_pipeline: RunPipeline,
    opportunity_id: str,
    workspace_id: Optional[str],
    user_id: str,
    message: str,
    history: Optional[list[dict[str, str]]] = None,
    model: Optional[str] = None,
    tz_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    on_tool_call: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> FollowupChatResult:
    """Run one conversational turn and return the reply + refreshed actions.

    ``system_prompt`` overrides ``_SYSTEM_PROMPT`` and ``on_tool_call(name, args)``
    fires for each tool the model invokes — the seams the DSPy prompt optimizer
    (optimization/followup) uses to swap a candidate prompt in and observe tool
    selection.
    """
    tools = _ChatTools(
        deps=deps,
        accept_graph=accept_graph,
        followup_graph=followup_graph,
        run_pipeline=run_pipeline,
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        tz_name=tz_name,
    )

    llm = get_chat_model(model)
    llm_with_tools = llm.bind_tools(_TOOLS)

    # Resolve dates in the rep's own timezone, and have the LLM emit the naive
    # local wall-clock — the dispatch layer converts it to UTC before booking, so
    # "1pm" books 1pm in the rep's day, not 1pm UTC.
    zone = _safe_zone(tz_name)
    tz_label = tz_name or "UTC"
    today = datetime.now(zone).strftime("%A, %Y-%m-%d %H:%M")
    messages: list = [
        SystemMessage(
            content=(
                f"{system_prompt or _SYSTEM_PROMPT}\n\nIt is currently {today} in the rep's timezone "
                f"({tz_label}). Interpret every date/time the rep mentions as local "
                f"to {tz_label}, and emit it as a naive ISO-8601 wall-clock time with "
                f"NO timezone offset (e.g. 2026-06-23T13:00:00 for 1pm). The system "
                f"converts it to UTC.\nCurrent opportunity id: {opportunity_id}"
            )
        )
    ]
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=message))

    reply = ""
    for _ in range(_MAX_TOOL_ROUNDS):
        response = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        tool_calls: list[dict[str, Any]] = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            reply = (
                response.content
                if isinstance(response.content, str)
                else str(response.content)
            )
            break

        for call in tool_calls:
            if on_tool_call is not None:
                on_tool_call(call["name"], call.get("args") or {})
            try:
                result = await tools.dispatch(call["name"], call.get("args") or {})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Follow-up chat tool %s failed", call.get("name"))
                result = {"error": str(exc)}
            messages.append(
                ToolMessage(
                    content=json.dumps(result, default=str),
                    tool_call_id=call["id"],
                )
            )
    else:
        reply = "I couldn't finish that request — could you rephrase or be more specific?"

    actions = await tools.list_actions()
    return FollowupChatResult(reply=reply or "", actions=actions)
