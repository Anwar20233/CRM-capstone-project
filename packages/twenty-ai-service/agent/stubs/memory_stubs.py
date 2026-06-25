"""Stub session-memory tools for the orchestrator.

The orchestrator is the only agent with memory.  A teammate is building the real
session-memory tools; until then these stubs give the orchestrator's LLM a
``remember`` / ``recall`` / ``get_session_context`` surface backed by an
in-process dict.  They return the same ``{ ok, data }`` / ``{ ok, error }``
envelope as the bridge, so swapping in the real tools later changes only this
file — the orchestrator wiring stays put.

These are deliberately given names distinct from the ``session_*`` names
reserved as INTERNAL in ``tool_scope.py`` (those are non-LLM-facing middleware
hooks); the orchestrator surfaces *these* as genuine LLM tools.

Note: this in-session ``remember``/``recall`` store is separate from the
orchestrator's conversation-history replay + compaction (see
``agent/orchestrator.py``), which is what actually keeps multi-turn context.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

STUB = True


class SessionMemory:
    """In-process key/value memory scoped to a single session (stub)."""

    def __init__(self, session_id: str = "default") -> None:
        self.session_id = session_id
        self._store: dict[str, str] = {}

    def remember(self, key: str, value: str) -> dict:
        self._store[key] = value
        return {"ok": True, "data": {"key": key, "value": value}}

    def recall(self, key: str) -> dict:
        if key not in self._store:
            return {
                "ok": False,
                "error": {"code": "NOT_FOUND", "message": f"No memory for '{key}'"},
            }
        return {"ok": True, "data": {"key": key, "value": self._store[key]}}

    def context(self) -> dict:
        return {"ok": True, "data": dict(self._store)}


def build_memory_tools(memory: SessionMemory) -> list[StructuredTool]:
    """Return the LLM-facing session-memory tools bound to *memory*."""

    async def _remember(key: str, value: str) -> dict:
        """Store a fact for later turns in this session (e.g. the entity in focus)."""
        return memory.remember(key, value)

    async def _recall(key: str) -> dict:
        """Retrieve a previously remembered fact by key."""
        return memory.recall(key)

    async def _get_session_context() -> dict:
        """Return everything remembered so far in this session."""
        return memory.context()

    return [
        StructuredTool.from_function(coroutine=_remember, name="remember"),
        StructuredTool.from_function(coroutine=_recall, name="recall"),
        StructuredTool.from_function(
            coroutine=_get_session_context, name="get_session_context"
        ),
    ]
