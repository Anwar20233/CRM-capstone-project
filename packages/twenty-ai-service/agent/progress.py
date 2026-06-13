"""Progress sink — a context-local channel for live agent progress events.

The orchestrator streams progress to the UI by passing an ``on_event`` callback
into its worker loop. Sub-agents (reader/writer), however, are invoked deep
inside that loop through the ``delegate_to_agent`` meta-tool, which cannot thread
the callback through its LLM-supplied arguments.

A ``ContextVar`` bridges the gap: the top-level worker publishes its ``on_event``
here, and any nested worker that wasn't handed its own callback inherits this one
automatically (ContextVars propagate across ``await`` within the same task). That
lets a reader's "searching people" or a writer's "creating note" step surface as
a stage, instead of the orchestrator only ever reporting "delegating".
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable

ProgressSink = Callable[[dict[str, Any]], None]

_sink: ContextVar[ProgressSink | None] = ContextVar(
    "agent_progress_sink", default=None
)


def set_progress_sink(sink: ProgressSink | None) -> Token:
    """Install *sink* as the current progress channel; returns a reset token."""
    return _sink.set(sink)


def reset_progress_sink(token: Token) -> None:
    """Restore the previous progress channel (pair with ``set_progress_sink``)."""
    _sink.reset(token)


def get_progress_sink() -> ProgressSink | None:
    """The progress channel in scope, or ``None`` when nothing is listening."""
    return _sink.get()


def emit_progress(event: dict[str, Any]) -> None:
    """Send a progress event to the current sink, if any (no-op otherwise)."""
    sink = _sink.get()
    if sink is not None:
        sink(event)
