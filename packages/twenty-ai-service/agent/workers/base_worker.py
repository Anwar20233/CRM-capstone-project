"""BaseWorker — the reusable LLM tool-calling loop.

Every agent worker is a ``BaseWorker`` parametrised by:

- **scope** — a ``ToolScope`` that decides which bridge tools the worker can
  see and call.
- **system_prompt** — the worker's persona / instructions.
- **write_policy** — an optional ``WritePolicy`` (invisible middleware) that
  gates mutation calls inside ``execute_tool``.  The LLM never calls the
  policy directly — it's embedded in the meta-tool.

Future agents (Reader, Analytics, …) are simply::

    reader = BaseWorker(scope=READER_SCOPE, system_prompt="...")
    analytics = BaseWorker(scope=ANALYTICS_SCOPE, system_prompt="...")

No new plumbing is needed — just a scope and a prompt.

The worker assembles its toolset from ``build_crm_tools(scope, write_policy)``
plus any extra utility tools.  The LLM model is configured via ``LLMClient``
/ environment variables.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from agent.tool_scope import ToolScope
from agent.workers.write_policy import WritePolicy


class BaseWorker:
    """A generic tool-calling agent loop, scoped to a ``ToolScope``.

    Parameters
    ----------
    scope:
        The ``ToolScope`` defining which bridge tools this worker can access.
    system_prompt:
        The system-message instruction text for the LLM.
    session_id:
        An opaque session id used by the write-policy middleware.  Defaults
        to ``"default"`` for dev convenience.
    write_policy:
        An optional ``WritePolicy`` embedded as invisible middleware inside
        ``execute_tool``.  If ``None``, writes go through without checks.
    extra_tools:
        Additional LangChain tools to include in the worker's toolset
        (e.g. ``resolve_date``).
    """

    def __init__(
        self,
        scope: ToolScope,
        system_prompt: str,
        *,
        session_id: str = "default",
        write_policy: WritePolicy | None = None,
        extra_tools: list[StructuredTool] | None = None,
    ) -> None:
        self.scope = scope
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.write_policy = write_policy

        # -- Assemble the toolset ------------------------------------------
        # Deferred import to avoid circular dependency:
        # crm_tools → write_policy → (workers/__init__) → base_worker → crm_tools
        from agent.crm_tools import build_crm_tools

        self._tools: list[StructuredTool] = list(
            build_crm_tools(scope, write_policy=write_policy)
        )

        # Include any extra tools (e.g. resolve_date utility).
        if extra_tools:
            self._tools.extend(extra_tools)

        # Index by name for fast dispatch.
        self._tools_by_name: dict[str, StructuredTool] = {
            t.name: t for t in self._tools
        }

    @property
    def tools(self) -> list[StructuredTool]:
        """The full list of tools bound to this worker (read-only)."""
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        """Sorted list of tool names this worker has access to."""
        return sorted(self._tools_by_name.keys())

    async def invoke_tool(self, name: str, args: dict[str, Any] | None = None) -> dict:
        """Invoke a single tool by name and return the bridge envelope.

        This is the programmatic entry-point for tests.  It does **not** go
        through the LLM — it runs the tool function directly.
        """
        tool = self._tools_by_name.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_TOOL",
                    "message": f"Tool '{name}' is not in this worker's toolset",
                },
            }
        return await tool.ainvoke(args or {})

    async def run(self, user_message: str) -> dict[str, Any]:
        """Execute the full agent loop for a single user message.

        This is a **simplified synchronous loop** (no streaming) suitable for
        dev and testing.  Production deployments should integrate with
        LangGraph's ``create_react_agent`` for streaming, checkpointing, etc.

        Returns a dict with::

            {
                "response": str,          # the final LLM text
                "tool_calls": [...],      # ordered list of {name, args, result}
            }
        """
        from agent.llm_client import LLMClient

        client = LLMClient()
        openai_client = client.get_openai_client()
        model = client.model

        # Build OpenAI-compatible tool schemas.
        oai_tools = [_to_openai_tool(t) for t in self._tools]

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        tool_calls_log: list[dict] = []

        # Agent loop — max 15 iterations to prevent runaway.
        for _ in range(15):
            response = openai_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=oai_tools if oai_tools else None,
            )
            choice = response.choices[0]
            msg = choice.message

            # If the model produced a text response (no tool calls), we're done.
            if not msg.tool_calls:
                return {
                    "response": msg.content or "",
                    "tool_calls": tool_calls_log,
                }

            # Append the assistant message (with tool_calls) to history.
            messages.append(msg.model_dump())

            # Execute each tool call.
            for tc in msg.tool_calls:
                import json

                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                result = await self.invoke_tool(name, args)

                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_calls_log.append({"name": name, "args": args, "result": result})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        # Exhausted iterations.
        return {
            "response": "[Agent reached maximum iterations without a final response]",
            "tool_calls": tool_calls_log,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_openai_tool(tool: StructuredTool) -> dict:
    """Convert a LangChain StructuredTool to an OpenAI-compatible tool dict."""
    schema = tool.args_schema.schema() if tool.args_schema else {"type": "object", "properties": {}}
    schema.pop("title", None)

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }
