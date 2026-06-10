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

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from agent.masking import EntityHandleMap
from agent.tool_scope import ToolScope
from agent.workers.write_policy import WritePolicy


# Appended to the system prompt when masking is active so the model treats
# tokens as opaque identifiers instead of trying to "fix" or expand them.
_MASKING_SYSTEM_NOTE = """

## Entity handles

Real people, companies, emails, phone numbers, and locations are replaced with
opaque handles like person001, company002, or email001. Each handle stands in
for a private value the system translates back automatically.

- Reuse handles verbatim — never invent, rename, or guess the value behind one.
- A resolved person/company handle exposes fields via dotted access. Use
  person001.id when a tool needs the record's id, person001.name for its name,
  company002.domainName for a domain, and so on. The handles available to you,
  with their fields, are listed below.
- Use the bare handle (person001) in prose replies; it renders as the name.
- Only use handles that appear in the list below. If you need a record that has
  no handle, search for it with the tools rather than fabricating an id.
"""


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
    tools_override:
        If provided, the worker uses exactly these tools instead of building the
        CRM meta-tools from *scope*. ``extra_tools`` still appends on top. This
        lets non-CRM agents (e.g. the orchestrator, whose tools are the
        agent-discovery + memory meta-tools) reuse the loop without touching the
        bridge. When ``None`` (the default) behaviour is unchanged.
    model:
        Optional model alias or OpenRouter slug overriding the env default for
        this worker (hot-swap). Per-call override is also available via
        ``run(..., model=...)``.
    pii_map:
        The session's ``EntityHandleMap``. The worker masks inbound text (prompt,
        tool results) and unmasks outbound text (tool arguments, final answer)
        against it. Pass a shared map to keep tokens consistent across workers
        (e.g. reader and writer in one session); omit it for a fresh per-worker
        map. Set ``mask_pii=False`` to disable masking entirely.
    mask_pii:
        Whether to run the masking hook. Defaults to ``True``.
    unmasked_tools:
        Tool names whose *results* bypass the masking hook. Use this for
        control-plane meta-tools that return system metadata, not CRM data —
        e.g. the orchestrator's ``get_agent_catalog``/``learn_agent``, whose
        payloads (agent names, schemas) would otherwise be mangled by the NER
        masker (e.g. "reader" mis-tagged as a person). Their arguments are still
        unmasked before dispatch; only result masking is skipped.
    """

    def __init__(
        self,
        scope: ToolScope,
        system_prompt: str,
        *,
        session_id: str = "default",
        write_policy: WritePolicy | None = None,
        extra_tools: list[StructuredTool] | None = None,
        tools_override: list[StructuredTool] | None = None,
        model: str | None = None,
        pii_map: EntityHandleMap | None = None,
        mask_pii: bool = True,
        unmasked_tools: frozenset[str] | None = None,
    ) -> None:
        self.scope = scope
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.write_policy = write_policy
        self.model = model
        self.unmasked_tools = unmasked_tools or frozenset()
        # The masking hook is a single session map; None disables masking.
        # Note the explicit None check: an empty EntityHandleMap is falsy
        # (``__len__`` is 0), so ``pii_map or EntityHandleMap()`` would discard it.
        if not mask_pii:
            self.pii_map: EntityHandleMap | None = None
        elif pii_map is not None:
            self.pii_map = pii_map
        else:
            self.pii_map = EntityHandleMap()

        # Set once the map has been rebuilt from the chat's stored history, so a
        # multi-turn session primes only on the first turn.
        self._mask_primed = False

        # -- Assemble the toolset ------------------------------------------
        if tools_override is not None:
            # Non-CRM agents (e.g. the orchestrator) supply their own toolset and
            # never touch the bridge — so skip build_crm_tools entirely.
            self._tools: list[StructuredTool] = list(tools_override)
        else:
            # Deferred import to avoid circular dependency:
            # crm_tools → write_policy → (workers/__init__) → base_worker → crm_tools
            from agent.crm_tools import build_crm_tools

            self._tools = list(build_crm_tools(scope, write_policy=write_policy))

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
        try:
            return await tool.ainvoke(args or {})
        except Exception as error:  # noqa: BLE001
            # Surface bad-argument errors (e.g. a weak model passing a string
            # where a list is expected) back to the LLM as a recoverable result
            # instead of crashing the turn — it can re-read the schema and retry.
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENTS",
                    "message": (
                        f"Tool '{name}' rejected the arguments: {error}. "
                        "Re-check the schema from learn_tools and retry with "
                        "correctly-typed arguments."
                    ),
                },
            }

    async def run(
        self,
        user_message: str,
        *,
        model: str | None = None,
        history: list[str] | None = None,
        prior_messages: list[dict[str, str]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute the full agent loop for a single user message.

        This is a **simplified synchronous loop** (no streaming) suitable for
        dev and testing.  Production deployments should integrate with
        LangGraph's ``create_react_agent`` for streaming, checkpointing, etc.

        ``model`` (alias or OpenRouter slug) overrides the worker's model for
        this call; it falls back to the worker's ``model`` and then env default.

        ``history`` is the chat's prior messages (stored unmasked) as a flat list
        of texts. On the first turn it primes the PII map so reopened chats
        recover the same tokens — no token table is persisted, the mapping is
        rebuilt from the messages.

        ``prior_messages`` is the prior conversation replayed into the LLM context
        as real chat turns — a list of ``{"role", "content"}`` dicts
        (``role`` ∈ user/assistant/system). Unlike ``history`` (priming only),
        these are sent to the model so the agent reasons over earlier turns. The
        orchestrator passes a compacted view here (recent turns verbatim + a
        summary system message). Each content string is masked before the model
        sees it. Primes the PII map too when ``history`` is not given.

        ``on_event`` is an optional callback fired with progress events so a CLI
        can render a live trace. Event shapes::

            {"type": "model", "model": "<slug>"}
            {"type": "llm_call", "step": int}
            {"type": "tool_call", "name": str, "args": dict}
            {"type": "tool_result", "name": str, "result": dict}
            {"type": "final", "response": str}

        Returns a dict with::

            {
                "response": str,          # the final LLM text
                "tool_calls": [...],      # ordered list of {name, args, result}
            }
        """
        import json

        from agent.llm_client import LLMClient

        def _emit(event: dict[str, Any]) -> None:
            if on_event:
                on_event(event)

        client = LLMClient(model=model or self.model)
        openai_client = client.get_openai_client()
        model_id = client.model
        _emit({"type": "model", "model": model_id})

        # Build OpenAI-compatible tool schemas.
        oai_tools = [_to_openai_tool(t) for t in self._tools]

        # Masking hook: mask the user prompt and brief the model on tokens so
        # raw PII never reaches the LLM. No-ops when masking is disabled.
        system_prompt = self.system_prompt
        if self.pii_map is not None:
            system_prompt += _MASKING_SYSTEM_NOTE
            handle_context = self.pii_map.handle_context()
            if handle_context:
                system_prompt += "\n\n" + handle_context
            # Rebuild the map from stored history once, so a reopened chat keeps
            # its existing tokens before the new message is masked against them.
            # Prefer the flat `history`; fall back to the replayed conversation.
            if not self._mask_primed:
                prime_source = history or (
                    [m["content"] for m in prior_messages] if prior_messages else None
                )
                if prime_source:
                    self.pii_map.prime(prime_source)
                    self._mask_primed = True
            masked_message = self.pii_map.mask_text(user_message)
            # Surface the masked prompt so callers can verify masking engaged
            # (the final answer is unmasked, so it's otherwise invisible).
            if masked_message != user_message:
                _emit({"type": "prompt_masked", "masked": masked_message})
            user_message = masked_message

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # Replay the prior conversation (masked) so the model reasons over it.
        if prior_messages:
            for prior in prior_messages:
                content = prior["content"]
                if self.pii_map is not None:
                    content = self.pii_map.mask_text(content)
                messages.append({"role": prior["role"], "content": content})
        messages.append({"role": "user", "content": user_message})
        tool_calls_log: list[dict] = []

        # Agent loop — max 15 iterations to prevent runaway.
        for step in range(1, 16):
            _emit({"type": "llm_call", "step": step})
            response = openai_client.chat.completions.create(
                model=model_id,
                messages=messages,
                tools=oai_tools if oai_tools else None,
            )
            choice = response.choices[0]
            msg = choice.message

            # If the model produced a text response (no tool calls), we're done.
            if not msg.tool_calls:
                # Unmask the answer so the user sees real values, not tokens.
                final_response = msg.content or ""
                if self.pii_map is not None:
                    final_response = self.pii_map.unmask_text(final_response)
                _emit({"type": "final", "response": final_response})
                return {
                    "response": final_response,
                    "tool_calls": tool_calls_log,
                }

            # Append the assistant message (with tool_calls) to history.
            messages.append(msg.model_dump())

            # Execute each tool call.
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                # Outbound: unmask handle references so the real CRM is hit.
                unresolved: list[str] = []
                if self.pii_map is not None:
                    args = self.pii_map.unmask_value(args)
                    unresolved = self.pii_map.find_unresolved_references(args)

                _emit({"type": "tool_call", "name": name, "args": args})
                if unresolved:
                    # A handle reference didn't resolve (unknown handle or field).
                    # Reject before hitting the CRM so the model can self-correct.
                    result: dict = {
                        "ok": False,
                        "error": {
                            "code": "UNRESOLVED_HANDLE",
                            "message": (
                                "These handle references are not valid: "
                                + ", ".join(sorted(set(unresolved)))
                                + ". Use only a handle (and field) listed in the "
                                "entity-handles section, or search for the record first."
                            ),
                        },
                    }
                else:
                    result = await self.invoke_tool(name, args)
                _emit({"type": "tool_result", "name": name, "result": result})
                tool_calls_log.append({"name": name, "args": args, "result": result})

                # If delegate_to_agent returned a writer interrupt, short-circuit
                # immediately — no more LLM calls until the user approves/rejects.
                if (
                    isinstance(result, dict)
                    and result.get("ok")
                    and isinstance(result.get("data"), dict)
                    and result["data"].get("type") == "interrupt"
                ):
                    return result["data"]  # {"type":"interrupt","interrupt":...,"thread_id":...}

                # Inbound: mask any PII the tool returned before the LLM sees it.
                # Control-plane tools (e.g. get_agent_catalog) are exempt — their
                # payloads are system metadata, not CRM data, and masking them
                # would corrupt agent/tool names the model must use verbatim.
                llm_result = result
                if self.pii_map is not None and name not in self.unmasked_tools:
                    llm_result = self.pii_map.mask_value(result)
                    if llm_result != result:
                        _emit({"type": "tool_result_masked", "name": name, "result": llm_result})

                result_str = (
                    json.dumps(llm_result) if isinstance(llm_result, dict) else str(llm_result)
                )

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
