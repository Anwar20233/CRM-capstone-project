"""Session state for the CRM agent layer.

Two modules:

- ``store``     — in-memory session store (Redis-ready) with topic, write log,
                  and duplicate detection.
- ``endpoints`` — FastAPI router that exposes the store over HTTP so the
                  Orchestrator can call it without coupling to Python imports.

These are **NOT LLM-facing tools**.  The session id always comes from the
authenticated identity context, never from the model.  The Orchestrator calls
these endpoints; the Writer/Reader workers call store functions directly via
``session_log_write`` when they complete a write.

Usage::

    # Programmatic (workers)
    from agent.session.store import session_log_write, session_check_duplicate

    # HTTP (Orchestrator)
    # POST /session/set-topic   { "session_id": "...", "topic": "..." }
    # POST /session/get-topic   { "session_id": "..." }
    # POST /session/log-write   { "session_id": "...", "tool": "...", ... }
    # POST /session/get-log     { "session_id": "..." }
    # POST /session/check-dup   { "session_id": "...", "tool": "...", "args": {...} }
    # POST /session/clear       { "session_id": "..." }
"""

from agent.session.store import (
    SessionStore,
    session_set_topic,
    session_get_topic,
    session_log_write,
    session_get_write_log,
    session_check_duplicate,
    session_clear,
    _store,
)

__all__ = [
    "SessionStore",
    "session_set_topic",
    "session_get_topic",
    "session_log_write",
    "session_get_write_log",
    "session_check_duplicate",
    "session_clear",
    "_store",
]
