"""Orchestrator — the planner/activator at the chat interface.

The orchestrator interprets a (possibly multi-intent) user message, plans the
work, and activates sub-agents to carry it out. It does NOT touch the CRM
directly: it discovers and delegates to sub-agents (reader, writer, and the
follow-up/researcher stubs) through the agent-discovery meta-tools, exactly the
way a worker discovers CRM tools. Its system prompt never hardcodes which
sub-agents exist or how to talk to them.

It is built on the same ``BaseWorker`` loop as every other agent, but its
toolset is the agent-discovery + session-memory meta-tools instead of CRM tools
(passed via ``tools_override``).

Memory
~~~~~~
The orchestrator is the only agent that remembers across turns. Two layers:

1. **Coordinator/specialist split (free).** Sub-agents are stateless and return
   only their final answer; their internal tool-call traces never accumulate in
   the orchestrator's context.
2. **Replay + compaction.** The full conversation is replayed into the LLM
   context each turn, until it exceeds ``MEMORY_COMPACTION_TOKEN_LIMIT`` tokens;
   then the older turns are summarised into a running summary (kept verbatim:
   recent turns) — the "condenser" pattern. No turns are silently dropped.

The in-session ``remember``/``recall`` tools are separate stubs (see
``agent/stubs/memory_stubs.py``) awaiting the teammate's real implementation.
"""

from __future__ import annotations

from typing import Any

from agent.agent_registry import build_default_registry
from agent.agent_scope import ORCHESTRATOR_SCOPE
from agent.agent_tools import build_agent_tools
from agent.masking import PIISessionMap
from agent.stubs.memory_stubs import SessionMemory, build_memory_tools
from agent.tool_scope import READER_SCOPE
from agent.workers.base_worker import BaseWorker


# Replay the conversation verbatim until it exceeds this many tokens, then
# compact older turns into a summary. Tunable — start at 35k and watch.
MEMORY_COMPACTION_TOKEN_LIMIT = 35_000

# How many of the most recent turns to always keep verbatim when compacting.
RECENT_TURNS_KEPT = 6


ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the Orchestrator of an agentic CRM. You sit in the chat interface and
coordinate specialist sub-agents — you never read or write the CRM yourself.

## How to work

1. Decompose the user's message into an ordered list of concrete sub-tasks.
2. Discover your sub-agents with ``get_agent_catalog``, then ``learn_agent`` to
   see how to instruct the ones you need. NEVER assume which agents exist or what
   they accept — always discover them.
3. Activate sub-agents with ``delegate_to_agent`` (one clear instruction each).
   Respect dependencies: resolve entities FIRST (the reader turns a name like
   "John" into a concrete record), THEN act (give the writer the resolved record
   id), THEN any follow-up/scheduling/research.
4. If the reader returns ``resolution: "multiple"`` or ``"none"``, ask the user
   to disambiguate instead of guessing.
5. Use ``remember``/``recall``/``get_session_context`` to carry key facts (e.g.
   the record currently in focus) across turns.
6. When all sub-tasks are done, reply to the user with one consolidated,
   natural-language summary of what happened.

## Rules

- Delegate; do not fabricate CRM data or invent record ids.
- A sub-agent result arrives as ``{"ok": true, "data": {...}}``; a failure as
  ``{"ok": false, "error": {...}}`` — react to errors, don't ignore them.
- Identity (workspace, role, user) is handled automatically — never ask about it.
"""


def _estimate_tokens(text: str) -> int:
    """Estimate token count, preferring tiktoken with a chars/4 fallback."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:  # noqa: BLE001 — tiktoken optional; heuristic is fine
        return len(text) // 4


class Orchestrator:
    """Planner/activator agent: plans intents and activates sub-agents."""

    def __init__(
        self,
        session_id: str = "default",
        model: str | None = None,
        *,
        compaction_token_limit: int = MEMORY_COMPACTION_TOKEN_LIMIT,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.compaction_token_limit = compaction_token_limit
        # Mask/unmask is owned by the inner worker; exposed for REPL/trace tools.

        # One PII map shared with every sub-agent so tokens stay consistent.
        self.pii_map = PIISessionMap()
        self.memory = SessionMemory(session_id)
        self.registry = build_default_registry(
            session_id=session_id, model=model, pii_map=self.pii_map
        )

        tools = build_agent_tools(self.registry, ORCHESTRATOR_SCOPE)
        tools += build_memory_tools(self.memory)

        self._worker = BaseWorker(
            scope=READER_SCOPE,  # unused — tools_override wins
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            session_id=session_id,
            model=model,
            pii_map=self.pii_map,
            tools_override=tools,
            # Agent-discovery results are control metadata (agent names, schemas),
            # not CRM data — never mask them. delegate_to_agent IS masked, since
            # it carries the sub-agent's CRM payload back to the planner.
            unmasked_tools=frozenset({"get_agent_catalog", "learn_agent"}),
        )

        # Conversation memory: a running summary of compacted-away older turns,
        # plus recent turns kept verbatim as {"role", "content"} dicts.
        self._summary: str | None = None
        self._turns: list[dict[str, str]] = []

    async def handle(
        self,
        user_message: str,
        *,
        on_event: Any = None,
    ) -> dict[str, Any]:
        """Plan and activate sub-agents for one user message; return the result."""
        await self._maybe_compact()
        prior = self._build_prior_messages()

        result = await self._worker.run(
            user_message, prior_messages=prior, on_event=on_event
        )

        self._turns.append({"role": "user", "content": user_message})
        self._turns.append({"role": "assistant", "content": result["response"]})
        return result

    # ``handle`` is the orchestrator's entry-point; ``run`` is an alias so it is
    # drop-in compatible with the ``BaseWorker``-driven REPL/trace harness.
    async def run(self, user_message: str, *, on_event: Any = None, **_: Any) -> dict[str, Any]:
        return await self.handle(user_message, on_event=on_event)

    @property
    def tool_names(self) -> list[str]:
        return self._worker.tool_names

    # -- Memory ---------------------------------------------------------

    def _build_prior_messages(self) -> list[dict[str, str]]:
        """The conversation to replay: summary (if any) as a system note + turns."""
        messages: list[dict[str, str]] = []
        if self._summary:
            messages.append(
                {
                    "role": "system",
                    "content": "Summary of earlier conversation:\n" + self._summary,
                }
            )
        messages.extend(self._turns)
        return messages

    async def _maybe_compact(self) -> None:
        """Summarise older turns once the conversation exceeds the token limit."""
        total = _estimate_tokens(self._summary or "")
        total += sum(_estimate_tokens(turn["content"]) for turn in self._turns)
        if total <= self.compaction_token_limit:
            return

        older = self._turns[:-RECENT_TURNS_KEPT]
        recent = self._turns[-RECENT_TURNS_KEPT:]
        if not older:
            return  # nothing old enough to fold away yet

        self._summary = await self._summarize(self._summary, older)
        self._turns = recent

    async def _summarize(
        self, prior_summary: str | None, turns: list[dict[str, str]]
    ) -> str:
        """Condense *prior_summary* + *turns* into a single updated summary.

        Uses the configured LLM. Kept small and patchable for tests.
        """
        from agent.llm_client import LLMClient

        transcript = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        instruction = (
            "Update the running summary of this CRM conversation. Preserve facts "
            "that matter for later turns: entities in focus and their record ids, "
            "decisions made, and pending actions. Be concise.\n\n"
            f"Existing summary:\n{prior_summary or '(none)'}\n\n"
            f"New turns to fold in:\n{transcript}"
        )
        client = LLMClient(model=self.model)
        openai_client = client.get_openai_client()
        response = openai_client.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": "You summarise conversations faithfully and tersely."},
                {"role": "user", "content": instruction},
            ],
        )
        return response.choices[0].message.content or (prior_summary or "")


orchestrator = Orchestrator()
