"""Writer StateGraph — the WriterWorker's LangGraph-powered execution graph.

Topology::

    llm ──► route_after_llm ──► write_gate  (tier-3 write call present)
                            ├──► tools       (tier-1/2 or non-write calls)
                            └──► END         (no tool calls → done)
    write_gate ──► route_after_gate ──► tools  (approved / pass-through)
                                   └──► llm    (rejected — ToolMessages injected)
    tools ──► llm

The write_gate node calls ``interrupt()`` for tier-3 writes, pausing the graph
until the API layer sends ``Command(resume=True/False)``.

Tools are built WITHOUT WritePolicy — write_gate IS the tier-3 gate.
Tier-1/2 writes flow straight to ToolNode with no extra friction.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode

from agent.crm_tools import build_crm_tools
from agent.stubs.safety_tools import _ACTION_TIER_MAP, build_utility_tools
from agent.tool_scope import WRITER_SCOPE
from agent.workers.write_gate import build_write_gate_node


# ---------------------------------------------------------------------------
# Message conversion helpers
# ---------------------------------------------------------------------------

def _to_openai_message(msg: BaseMessage) -> dict[str, Any]:
    """Convert a LangChain BaseMessage to an OpenAI-compatible dict."""
    from langchain_core.messages import HumanMessage

    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content or ""}
    if isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content or ""}
    if isinstance(msg, AIMessage):
        d: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("args", {})),
                    },
                }
                for tc in msg.tool_calls
            ]
        return d
    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content or "",
        }
    return {"role": "user", "content": str(msg.content or "")}


def _parse_ai_message(oai_msg: Any) -> AIMessage:
    """Convert an OpenAI response message to a LangChain AIMessage."""
    tool_calls = []
    for tc in oai_msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(
            {"id": tc.id, "name": tc.function.name, "args": args, "type": "tool_call"}
        )
    return AIMessage(content=oai_msg.content or "", tool_calls=tool_calls)


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def _has_tier3_write(message: AIMessage) -> bool:
    # The writer uses the meta-tool pattern: LLM calls execute_tool(tool="...", ...)
    # so the tool_call name is always "execute_tool" — inspect args for the real action.
    for tc in message.tool_calls or []:
        if tc.get("name") == "execute_tool":
            action = tc.get("args", {}).get("tool", "")
            entry = _ACTION_TIER_MAP.get(action)
            if entry is None or entry.get("tier", 3) >= 3:
                # Unknown actions default to tier 3 (fail-safe).
                if action:  # only gate if there's an actual action name
                    return True
    return False


def _route_after_llm(
    state: dict,
) -> Literal["write_gate", "tools", "auto_link"]:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        # Terminal — run the deterministic link guard before ending, so a note/
        # task the writer created but forgot to link still gets its target rows.
        return "auto_link"
    if _has_tier3_write(last):
        return "write_gate"
    return "tools"


def _route_after_gate(state: dict) -> Literal["tools", "llm"]:
    """After the gate: rejected injections route back to llm; else to tools."""
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and "USER_REJECTED" in (last.content or ""):
        return "llm"
    return "tools"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_writer_graph(
    system_prompt: str,
    model: str | None = None,
    *,
    checkpointer=None,
    pii_map=None,
    auto_approve: bool = False,
):
    """Compile and return the writer StateGraph.

    Parameters
    ----------
    system_prompt:
        The writer's system message text.
    model:
        Optional model alias or OpenRouter slug (falls back to LLMClient default).
    checkpointer:
        LangGraph checkpointer for state persistence.  Pass ``MemorySaver()``
        for in-process use; swap for a Redis/Postgres saver in production.
    pii_map:
        Optional handle map. When set, ``execute_tool`` unmasks tool arguments at
        the execute step — mapping handles back to real values right before the
        bridge write, so the writer LLM only ever sees handles, never PII.
    """
    from agent.llm_client import LLMClient
    from agent.workers.base_worker import _to_openai_tool

    # Build CRM tools WITHOUT WritePolicy — write_gate owns tier-3 gating.
    crm_tools = build_crm_tools(WRITER_SCOPE, write_policy=None, pii_map=pii_map)
    all_tools = crm_tools + build_utility_tools()

    client = LLMClient(model=model)
    openai_client = client.get_openai_client()
    model_id = client.model
    oai_tool_schemas = [_to_openai_tool(t) for t in all_tools]

    # -- LLM node ----------------------------------------------------------

    def llm_node(state: dict) -> dict:
        messages = state["messages"]
        # Prepend system message if not already present.
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=system_prompt), *messages]

        oai_messages = [_to_openai_message(m) for m in messages]

        # Run the sync OpenAI client in a thread so we don't block the event loop.
        def _call() -> Any:
            return openai_client.chat.completions.create(
                model=model_id,
                messages=oai_messages,
                tools=oai_tool_schemas,
            )

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()

        if loop.is_running():
            # Inside an async context — run sync call in thread executor.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call)
                response = future.result()
        else:
            response = _call()

        ai_msg = _parse_ai_message(response.choices[0].message)

        # Surface the writer's concrete actions as progress (the writer is a
        # graph, not a BaseWorker, so it emits here rather than via on_event).
        # Fires before write_gate, so a gated delete shows its label then pauses.
        from agent.progress import emit_progress

        for tc in ai_msg.tool_calls or []:
            emit_progress({"type": "tool_call", "name": tc.get("name"), "args": tc.get("args", {})})

        return {"messages": [ai_msg]}

    # -- auto-link node ----------------------------------------------------
    # Deterministic guard: after the writer is done, ensure every note/task it
    # created links to the entities the instruction named (one join row each),
    # regardless of whether the LLM remembered to issue the create_*_target calls.

    async def auto_link_node(state: dict) -> dict:
        from agent.workers.auto_link import reconcile_targets

        try:
            await reconcile_targets(state["messages"], pii_map)
        except Exception:  # noqa: BLE001 — never let linking break a completed write
            from agent.progress import emit_progress

            emit_progress({"type": "log", "message": "auto_link guard failed"})
        return {}

    # -- Assemble graph ----------------------------------------------------

    tool_node = ToolNode(all_tools)

    builder = StateGraph(MessagesState)
    builder.add_node("llm", llm_node)
    builder.add_node("write_gate", build_write_gate_node(auto_approve))
    builder.add_node("tools", tool_node)
    builder.add_node("auto_link", auto_link_node)

    builder.set_entry_point("llm")
    builder.add_conditional_edges(
        "llm",
        _route_after_llm,
        {"write_gate": "write_gate", "tools": "tools", "auto_link": "auto_link"},
    )
    builder.add_conditional_edges(
        "write_gate",
        _route_after_gate,
        {"tools": "tools", "llm": "llm"},
    )
    builder.add_edge("tools", "llm")
    builder.add_edge("auto_link", END)

    # Name the graph so traces read "writer-agent" instead of a generic
    # "LangGraph" — otherwise reader/writer/followup graphs are indistinguishable.
    return builder.compile(checkpointer=checkpointer, name="writer-agent")
