"""WriterWorker — the CRM Write agent backed by a LangGraph StateGraph.

The writer is now a proper LangGraph graph (see ``writer_graph.py``) with a
``write_gate`` node that calls ``interrupt()`` for tier-3 actions.  The public
interface is unchanged — callers still ``await writer.run(instruction)`` and
get back ``{"response": ..., "tool_calls": [...]}`` — but high-risk writes now
return an interrupt payload instead:

    {"type": "interrupt", "interrupt": {...}, "thread_id": "<session_id>"}

Callers (``agent_registry._invoke_writer`` → ``delegate_to_agent`` →
``BaseWorker.run``) propagate this up to the API layer, which pauses the
response and shows an approval UI to the user.  The user's choice is then sent
to ``writer.resume(approved)`` which feeds ``Command(resume=...)`` back into the
graph and lets it continue from the gate node.

Session registry
~~~~~~~~~~~~~~~~
``WriterWorker`` keeps a class-level ``_sessions`` dict so the ``/agent/resume``
endpoint can find the right worker by session id without threading the instance
through the whole call stack.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.crm_tools import build_crm_tools
from agent.masking import EntityHandleMap
from agent.stubs.safety_tools import build_utility_tools
from agent.tool_scope import WRITER_SCOPE
from agent.workers.writer_graph import build_writer_graph


_WRITER_SYSTEM_PROMPT = """\
You are a CRM Write Agent for Twenty CRM. Execute write instructions precisely.

## Tool Discovery Protocol (strict order, optimized)

1. **Get tool:** Call `get_tool_catalog` with `object_name` AND `operation` to retrieve the exact tool(s) needed (returns 1–3 tools).
2. **Learn schema:** Call `learn_tools` with the tool name to get the exact JSON input schema.
3. **Execute:** Call `execute_tool` with the tool name and correctly-shaped arguments.

**Optimizations:**
- **Bulk first:** For multiple entities of the same type (e.g., "Create two people"), use `create_many` operation. Example: `get_tool_catalog(object_name="person", operation="create_many")`. Execute a single call.
- **Cache tool name:** If the same tool name is reused in a task, skip `get_tool_catalog` and reuse the previously learned schema.
- **Cache schema:** Once `learn_tools` is called for a tool, reuse its schema for subsequent `execute_tool` calls with that tool. Do not re-call `learn_tools`.

## Confirmation Flow

High-risk actions (deletions, terminal stage moves, bulk updates) are intercepted
automatically before execution and require user approval. You do NOT manage
confirmation tokens. Simply attempt the action via `execute_tool`; if the user
approves, execution proceeds transparently. If rejected, you will receive a
USER_REJECTED error — inform the user calmly and stop.

## Deal Stage Advancement (critical)

For instructions like "Advance the deal to <STAGE>" or "Move deal to stage <STAGE>":
- This is NOT a generic update. Use the dedicated `advance_deal_stage` tool.
- Discover via: `get_tool_catalog(object_name="opportunity", operation="advance_stage")`.
- Execute: `execute_tool(tool="advance_deal_stage", arguments={deal_id: <id>, stage: "<STAGE>"})`.

## Date Handling

For relative dates (e.g., "next Friday", "in 2 weeks"), call `resolve_date` FIRST.
Convert to ISO-8601, then proceed with the normal discovery protocol.

## Scope & Data Rules

- You are a writer. Do not search or read records (reader agent's job). Refuse if lookup is required.
- NEVER fabricate data. If information is missing, ask for it.
- Twenty uses: "person"/"people" (not "contact"), "note" (not "activity"), "task" (not "comment").
- Identity fields (workspace, role, user) are injected automatically. Never mention or ask about them.

## Entity Types & Operations

**Entity types (`object_name`):** `person`, `company`, `note`, `opportunity`, `calendarEvent`, `dashboard`, `task`, or `"other"` for remaining types.

**Write operations (`operation`):** `create`, `update`, `delete`, `create_many`, `update_many`, `advance_stage`.

## Request Interpretation

- List of same-type entities (e.g., "Sarah Kim and Yara Hassan") → `create_many` request.
- "Advance" or "move" a deal → always use deal stage advancement rule.
- Always acknowledge the request concisely before beginning tool use.
"""


# Class-level registry: session_id → WriterWorker instance.
# Used by the /agent/resume endpoint to find the right worker.
_sessions: dict[str, "WriterWorker"] = {}


class WriterWorker:
    """CRM Write Agent driven by a LangGraph StateGraph.

    Parameters
    ----------
    session_id:
        Unique session identifier — also the LangGraph thread_id so the
        checkpointer can restore state between turns and after an interrupt.
    model:
        Optional model alias or OpenRouter slug overriding the env default.
    pii_map:
        Shared ``EntityHandleMap`` (passed by the orchestrator so PII tokens
        stay consistent across reader/writer in the same session).
    """

    def __init__(
        self,
        session_id: str = "default",
        model: str | None = None,
        *,
        pii_map: EntityHandleMap | None = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.pii_map = pii_map

        self._checkpointer = MemorySaver()
        self._graph = build_writer_graph(
            system_prompt=_WRITER_SYSTEM_PROMPT,
            model=model,
            checkpointer=self._checkpointer,
        )
        self._config = {"configurable": {"thread_id": session_id}}

        # Pre-compute tool name list so chat.py's preflight() can display it.
        _tools = build_crm_tools(WRITER_SCOPE, write_policy=None) + build_utility_tools()
        self._tool_names: list[str] = sorted(t.name for t in _tools)

        # Register for resume lookups.
        _sessions[session_id] = self

    @property
    def tool_names(self) -> list[str]:
        return self._tool_names

    # ------------------------------------------------------------------
    # Public interface (same shape as BaseWorker.run)
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        *,
        on_event: Any = None,  # accepted for interface parity; graph emits no events yet
        **_: Any,
    ) -> dict[str, Any]:
        """Run one instruction through the writer graph.

        Returns either a normal response dict::

            {"response": str, "tool_calls": [...]}

        or an interrupt payload when a tier-3 action needs approval::

            {"type": "interrupt", "interrupt": {...}, "thread_id": str}
        """
        await self._graph.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config=self._config,
        )
        return self._extract_result()

    async def resume(self, approved: bool) -> dict[str, Any]:
        """Resume after the user approves or rejects a write action.

        Feeds ``Command(resume=approved)`` into the graph, which re-enters the
        ``write_gate`` node.  Returns the same shape as ``run()``.
        """
        await self._graph.ainvoke(
            Command(resume=approved),
            config=self._config,
        )
        return self._extract_result()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_result(self) -> dict[str, Any]:
        """Read the current graph state and build the caller-facing result."""
        state = self._graph.get_state(self._config)

        # Graph paused at an interrupt — return the approval request.
        if state.next:
            interrupts = state.tasks[0].interrupts if state.tasks else []
            interrupt_data = interrupts[0].value if interrupts else {}
            return {
                "type": "interrupt",
                "interrupt": interrupt_data,
                "thread_id": self.session_id,
            }

        # Graph finished — extract the last AIMessage as the response.
        messages = state.values.get("messages", [])
        response = ""
        for msg in reversed(messages):
            from langchain_core.messages import AIMessage
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                response = msg.content or ""
                break

        return {"response": response, "tool_calls": [], "type": "response"}

    @classmethod
    def get_session(cls, session_id: str) -> "WriterWorker | None":
        """Return the WriterWorker for an existing session, or None."""
        return _sessions.get(session_id)
