"""FastAPI router for session state endpoints.

Mounts at ``/session`` and exposes the ``SessionStore`` over HTTP so the
Orchestrator (and any other service) can manage session state without importing
Python modules directly.

All endpoints use POST with a JSON body so they are easy to call from any
language.  All responses use the same ``{ ok, data }`` / ``{ ok, error }``
envelope as the bridge, keeping the whole layer consistent.

Endpoints
~~~~~~~~~
``POST /session/set-topic``        set the current topic for a session
``POST /session/get-topic``        read the current topic
``POST /session/log-write``        append a write entry to the audit log
``POST /session/get-log``          read the full write log
``POST /session/check-duplicate``  check if a write is a duplicate
``POST /session/clear``            wipe all state for a session
``GET  /session/health``           liveness check

Security note: these endpoints are internal — the Orchestrator is the only
caller.  In production, bind the service to localhost or put the session router
behind the same auth middleware as the bridge proxy (``routers/bridge.py``).
Session ids MUST come from the authenticated session context, never from
user/LLM input.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent.session.store import (
    session_set_topic,
    session_get_topic,
    session_log_write,
    session_get_write_log,
    session_check_duplicate,
    session_clear,
)

router = APIRouter(prefix="/session", tags=["session"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SetTopicRequest(BaseModel):
    session_id: str = Field(..., description="Opaque session identifier from the authenticated context")
    topic: str = Field(..., description="Current conversation topic / intent summary")


class GetTopicRequest(BaseModel):
    session_id: str


class LogWriteRequest(BaseModel):
    session_id: str
    tool: str = Field(..., description="Bridge tool name that was executed, e.g. 'create_person'")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments passed to the tool")
    old_value: Any = Field(None, description="State before the write (capture before calling execute)")
    new_value: Any = Field(None, description="State after the write (result from bridge)")


class GetLogRequest(BaseModel):
    session_id: str


class CheckDuplicateRequest(BaseModel):
    session_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ClearRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/set-topic")
async def set_topic(body: SetTopicRequest) -> dict:
    """Set the conversation topic for a session.

    The Orchestrator calls this at the start of each user turn once it has
    classified the user's intent.  The topic is surfaced in the write log and
    used for context in conflict resolution.
    """
    return await session_set_topic(body.session_id, body.topic)


@router.post("/get-topic")
async def get_topic(body: GetTopicRequest) -> dict:
    """Return the current topic for a session (``null`` if not set)."""
    return await session_get_topic(body.session_id)


@router.post("/log-write")
async def log_write(body: LogWriteRequest) -> dict:
    """Append a completed write to the session audit log.

    The WriterWorker (or the Orchestrator on its behalf) calls this after every
    successful bridge write.  Capturing ``old_value`` before the write and
    ``new_value`` from the bridge result enables before/after diffing and
    full rollback planning.

    Returns the created log entry including its generated ``id`` and ``ts``.
    """
    return await session_log_write(
        body.session_id,
        body.tool,
        body.args,
        body.old_value,
        body.new_value,
    )


@router.post("/get-log")
async def get_log(body: GetLogRequest) -> dict:
    """Return the full ordered write log for a session.

    Entries are in append order (oldest first).  The Orchestrator uses this
    to build a correction plan when the user says "undo that" or to provide
    a full diff to the user on request.
    """
    return await session_get_write_log(body.session_id)


@router.post("/check-duplicate")
async def check_duplicate(body: CheckDuplicateRequest) -> dict:
    """Check whether a write with the same tool + args was recently logged.

    Scans the last 5 minutes of the session write log.  The Orchestrator calls
    this before dispatching any write instruction to the WriterWorker so that
    network retries and ambiguous re-confirmations don't produce double-writes.

    Response::

        { "ok": true, "data": { "duplicate": false } }
        { "ok": true, "data": { "duplicate": true, "entry": { ...log_entry } } }
    """
    return await session_check_duplicate(body.session_id, body.tool, body.args)


@router.post("/clear")
async def clear_session(body: ClearRequest) -> dict:
    """Wipe all state (topic + write log) for a session.

    Call at the end of a conversation or when starting a fresh context for the
    same session id.
    """
    return await session_clear(body.session_id)


@router.get("/health")
async def session_health() -> dict:
    """Liveness check — confirms the session store is initialised."""
    return {"ok": True, "data": {"status": "ok", "backend": "in-memory"}}
