"""Agent router — the chat interface and write-approval endpoints.

Two endpoints:

``POST /agent/chat``
    Send a user message to the Orchestrator.  Returns either a normal response
    or an interrupt payload when the writer hits a tier-3 action.

    Normal response::

        {"type": "response", "response": "<text>", "tool_calls": [...]}

    Interrupt (writer paused, waiting for approval)::

        {"type": "interrupt", "interrupt": {"action": "...", "args": {...},
         "summary": "..."}, "thread_id": "<session_id>"}

``POST /agent/resume``
    Resume a paused writer graph after the user approves or rejects a write.
    Accepts the same ``session_id`` and ``approved`` flag.  Returns the same
    shape as a normal chat response once the graph finishes.

Security note: ``session_id`` must come from the authenticated session context.
Never let the user/LLM supply it.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.orchestrator import Orchestrator
from agent.workers.writer_worker import WriterWorker

router = APIRouter(prefix="/agent", tags=["agent"])

# One Orchestrator per session.  In production, use a proper session store
# (Redis-backed, TTL'd).  This in-process dict is fine for development.
_orchestrators: dict[str, Orchestrator] = {}


def _get_or_create_orchestrator(session_id: str) -> Orchestrator:
    if session_id not in _orchestrators:
        _orchestrators[session_id] = Orchestrator(session_id=session_id)
    return _orchestrators[session_id]


# Per-session conversation history for follow-up chats, so a session continues
# with full context across turns (keyed by the platform chat's session_id).
_followup_histories: dict[str, list[dict[str, str]]] = {}


async def _handle_followup_chat(body: "ChatRequest") -> str:
    """Route a message to the Follow-Up agent scoped to ``body.opportunity_id``.

    Maintains per-session history so the conversation continues with context on
    the standing plan / deal, then returns the assistant reply text.
    """
    from followup.api.dependencies import (
        get_accept_graph,
        get_deps,
        get_followup_graph,
    )
    from followup.api.routes import _run_followup_pipeline
    from followup.chat import run_followup_chat

    history = _followup_histories.setdefault(body.session_id, [])
    result = await run_followup_chat(
        deps=get_deps(),
        accept_graph=get_accept_graph(),
        followup_graph=get_followup_graph(),
        run_pipeline=_run_followup_pipeline,
        opportunity_id=body.opportunity_id,  # type: ignore[arg-type]
        workspace_id=body.workspace_id,
        user_id=body.user_id or "chat",
        message=body.message,
        history=list(history),
        tz_name=body.timezone,
    )

    history.append({"role": "user", "content": body.message})
    history.append({"role": "assistant", "content": result.reply})
    return result.reply


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Opaque session id from the authenticated context")
    message: str = Field(..., min_length=1, description="The user's message")
    # When present, the message is routed to the Follow-Up agent (scoped to this
    # opportunity) instead of the generic Orchestrator — this is how the platform
    # chat embedded on the opportunity Follow-Up tab talks to the follow-up agent.
    opportunity_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    workspace_id: Optional[str] = Field(default=None)
    # The rep's IANA timezone (e.g. "America/New_York") so clock times they type
    # ("1pm") are interpreted as local and converted to UTC before booking.
    timezone: Optional[str] = Field(default=None)


class ResumeRequest(BaseModel):
    session_id: str = Field(..., description="Same session_id that produced the interrupt")
    approved: bool = Field(..., description="True = proceed with the action, False = cancel")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat(body: ChatRequest) -> dict[str, Any]:
    """Send a user message to the Orchestrator.

    Returns a normal response or an interrupt payload when the writer needs
    approval before executing a high-risk action.
    """
    if body.opportunity_id:
        reply = await _handle_followup_chat(body)
        return {"type": "response", "response": reply, "tool_calls": []}

    orchestrator = _get_or_create_orchestrator(body.session_id)
    result = await orchestrator.handle(body.message)

    if result.get("type") == "interrupt":
        # Return the interrupt payload directly — the frontend shows the
        # approval UI and then calls /agent/resume.
        return result

    return {
        "type": "response",
        "response": result.get("response", ""),
        "tool_calls": result.get("tool_calls", []),
    }


# ---------------------------------------------------------------------------
# Streaming endpoints (NDJSON)
# ---------------------------------------------------------------------------
#
# The non-streaming endpoints above return one blocking JSON payload. The
# streaming variants instead emit newline-delimited JSON events as the work
# happens, so the caller can render live progress and type the answer out:
#
#   {"kind": "stage",     "text": "Looking up records…"}   # progress label
#   {"kind": "token",     "text": "Hello "}                # answer chunk
#   {"kind": "interrupt", "interrupt": {...}}              # tier-3 approval
#   {"kind": "error",     "message": "..."}
#   {"kind": "done"}                                       # terminal


def _ndjson(obj: dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


# Word-with-trailing-space chunks so the answer types out naturally. The final
# response is produced whole (the worker loop is not token-streaming), so we
# re-chunk it here for the typing effect on the client.
def _word_chunks(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"\S+\s*", text) or [text]


_AGENT_STAGE_LABELS = {
    "reader": "Looking up records…",
    "writer": "Applying changes…",
    "researcher": "Researching…",
    "followup": "Preparing follow-up…",
}

# CRM action verb (first token of the action name) → present-participle label.
_ACTION_VERB_LABELS = {
    "create": "Creating",
    "add": "Adding",
    "update": "Updating",
    "edit": "Editing",
    "set": "Updating",
    "change": "Updating",
    "delete": "Deleting",
    "remove": "Removing",
    "advance": "Advancing",
    "move": "Moving",
    "search": "Searching",
    "find": "Finding",
    "list": "Listing",
    "get": "Reading",
    "read": "Reading",
    "fetch": "Reading",
    "lookup": "Looking up",
    "resolve": "Resolving",
    "close": "Closing",
    "send": "Sending",
    "schedule": "Scheduling",
    "reassign": "Reassigning",
    "onboard": "Onboarding",
    "bulk": "Bulk updating",
}


def _action_label(action: str) -> str | None:
    """Turn a CRM action name into a readable stage, e.g.

    ``search_people`` → "Searching people…", ``delete_company`` → "Deleting
    company…", ``advance_deal_stage`` → "Advancing deal stage…".
    """
    if not action:
        return None
    parts = action.split("_")
    verb = _ACTION_VERB_LABELS.get(parts[0], parts[0].capitalize())
    rest = " ".join(parts[1:]).replace("-", " ").strip()
    return f"{verb} {rest}…".strip() if rest else f"{verb}…"


def _to_stage(event: dict[str, Any]) -> str | None:
    """Map a raw BaseWorker progress event to a human-readable stage label.

    Surfaces both orchestrator-level routing (which sub-agent) and the concrete
    sub-agent actions (which CRM operation), so the UI shows what is actually
    happening. Returns ``None`` for events that should not surface as a stage.
    """
    event_type = event.get("type")
    if event_type == "tool_call":
        name = event.get("name")
        args = event.get("args") or {}
        if name == "delegate_to_agent":
            agent = args.get("agent", "")
            return _AGENT_STAGE_LABELS.get(agent, f"Delegating to {agent}…")
        if name == "execute_tool":
            return _action_label(args.get("tool", ""))
        if name == "get_tool_catalog":
            return "Finding the right tool…"
        if name == "resolve_date":
            return "Working out dates…"
        if name in ("get_agent_catalog", "learn_agent", "learn_tools"):
            return "Planning…"
        if name == "get_current_user":
            return None
    if event_type == "llm_call":
        return "Thinking…"
    return None


async def _stream_final_response(result: dict[str, Any]) -> AsyncIterator[str]:
    """Emit the terminal NDJSON events for a finished worker result."""
    if result.get("type") == "interrupt":
        yield _ndjson({"kind": "interrupt", "interrupt": result.get("interrupt", {})})
        return
    for chunk in _word_chunks(result.get("response", "")):
        yield _ndjson({"kind": "token", "text": chunk})
        await asyncio.sleep(0.012)
    yield _ndjson({"kind": "done"})


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest) -> StreamingResponse:
    """Stream the orchestrator's progress and answer as NDJSON events."""
    if body.opportunity_id:
        # Follow-up agent path: run the (non-streaming) follow-up turn, then
        # type the reply out so the platform chat renders it like any answer.
        async def generate_followup() -> AsyncIterator[str]:
            yield _ndjson({"kind": "stage", "text": "Reviewing the deal…"})
            try:
                reply = await _handle_followup_chat(body)
            except Exception as error:  # noqa: BLE001 — surface, never crash the stream
                yield _ndjson({"kind": "error", "message": str(error)})
                yield _ndjson({"kind": "done"})
                return
            async for event in _stream_final_response({"response": reply}):
                yield event

        return StreamingResponse(
            generate_followup(), media_type="application/x-ndjson"
        )

    orchestrator = _get_or_create_orchestrator(body.session_id)
    queue: asyncio.Queue = asyncio.Queue()
    last_stage: dict[str, str | None] = {"text": None}

    def on_event(event: dict[str, Any]) -> None:
        stage = _to_stage(event)
        if stage is not None and stage != last_stage["text"]:
            last_stage["text"] = stage
            queue.put_nowait({"kind": "stage", "text": stage})

    async def run() -> None:
        try:
            result = await orchestrator.handle(body.message, on_event=on_event)
            queue.put_nowait({"kind": "result", "result": result})
        except Exception as error:  # noqa: BLE001 — surface, never crash the stream
            queue.put_nowait({"kind": "error", "message": str(error)})
        finally:
            queue.put_nowait(None)

    async def generate() -> AsyncIterator[str]:
        # Immediate feedback before the first (loop-blocking) LLM call returns.
        yield _ndjson({"kind": "stage", "text": "Thinking…"})
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind = item.get("kind")
                if kind in ("stage", "error"):
                    yield _ndjson(item)
                elif kind == "result":
                    async for event in _stream_final_response(item["result"]):
                        yield event
        finally:
            await task

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/resume/stream")
async def resume_stream(body: ResumeRequest) -> StreamingResponse:
    """Stream the writer's continuation after the user approves/rejects."""
    writer = WriterWorker.get_session(body.session_id)
    if writer is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active writer session for '{body.session_id}'",
        )

    async def generate() -> AsyncIterator[str]:
        yield _ndjson(
            {
                "kind": "stage",
                "text": "Applying changes…" if body.approved else "Cancelling…",
            }
        )
        try:
            result = await writer.resume(body.approved)
        except Exception as error:  # noqa: BLE001
            yield _ndjson({"kind": "error", "message": str(error)})
            return
        async for event in _stream_final_response(result):
            yield event

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/resume")
async def resume(body: ResumeRequest) -> dict[str, Any]:
    """Resume a paused writer graph after the user approves or rejects.

    The writer graph is looked up by session_id.  If no paused graph is found
    (e.g. the interrupt already expired or was already resumed), returns 404.
    """
    writer = WriterWorker.get_session(body.session_id)
    if writer is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active writer session for '{body.session_id}'",
        )

    result = await writer.resume(body.approved)

    if result.get("type") == "interrupt":
        # A second tier-3 action appeared after the first was approved —
        # surface it the same way.
        return result

    return {
        "type": "response",
        "response": result.get("response", ""),
        "tool_calls": result.get("tool_calls", []),
    }
