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

from typing import Any

from fastapi import APIRouter, HTTPException
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


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Opaque session id from the authenticated context")
    message: str = Field(..., min_length=1, description="The user's message")


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
