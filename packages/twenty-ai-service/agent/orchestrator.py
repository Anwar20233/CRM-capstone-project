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

import re
from dataclasses import dataclass, field
from typing import Any

from agent.agent_registry import build_default_registry
from agent.agent_scope import ORCHESTRATOR_SCOPE
from agent.agent_tools import build_agent_tools
from agent.masking import (
    DEFAULT_LABEL_TO_TYPE,
    RESOLVABLE_TYPES,
    CRMResolver,
    EntityHandleMap,
    Resolution,
    build_bridge_search,
)
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
   People and companies in the user's message are already resolved into handles
   (e.g. person001, company002) listed in the entity-handles section. Pass the
   handle or a field like person001.id straight to the writer — you usually do
   NOT need the reader to resolve a name that already has a handle. Use the
   reader only for lookups with no handle yet. Resolve FIRST, then act, then any
   follow-up/scheduling/research.
4. If the reader returns ``resolution: "multiple"`` or ``"none"``, ask the user
   to disambiguate instead of guessing. (Ambiguous names in the user's message
   are already turned into a clarifying question before you run.)
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


@dataclass
class _Ambiguity:
    """A name in the user's message that matched more than one CRM record."""

    entity_type: str  # "person" | "company"
    query: str  # the name as the user wrote it
    candidates: list[dict[str, Any]]  # the matching records


@dataclass
class _PendingDisambiguation:
    """State carried to the next turn while waiting for the user to choose."""

    original_message: str
    ambiguities: list[_Ambiguity] = field(default_factory=list)


def _candidate_label(record: dict[str, Any]) -> str:
    """A short human label distinguishing one candidate from another."""
    name = record.get("name")
    if isinstance(name, dict):
        display = " ".join(
            part for part in (name.get("firstName"), name.get("lastName")) if part
        )
    else:
        display = str(name or "").strip()
    email = _primary(record.get("emails"), "primaryEmail")
    company = record.get("company")
    company_name = company.get("name") if isinstance(company, dict) else None
    detail = email or company_name
    return f"{display} ({detail})" if detail else display or record.get("id", "?")


def _primary(value: Any, key: str) -> str | None:
    return value.get(key) if isinstance(value, dict) else None


def _format_disambiguation(ambiguities: list[_Ambiguity]) -> str:
    """Build the clarifying question listing each ambiguous name's candidates."""
    blocks: list[str] = []
    for ambiguity in ambiguities:
        lines = [f'Which "{ambiguity.query}" ({ambiguity.entity_type}) did you mean?']
        for index, candidate in enumerate(ambiguity.candidates, start=1):
            lines.append(f"  {index}. {_candidate_label(candidate)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _match_choice(reply: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a user's reply to one candidate, by ordinal or distinguishing text.

    Accepts "1"/"2" (1-based), or any candidate whose label/email/company name
    the reply contains. Returns ``None`` when the reply is too ambiguous to bind.
    """
    normalized = reply.strip().casefold()

    ordinals = re.findall(r"\b(\d+)\b", normalized)
    if len(ordinals) == 1:
        index = int(ordinals[0]) - 1
        if 0 <= index < len(candidates):
            return candidates[index]

    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        label = _candidate_label(candidate).casefold()
        email = (_primary(candidate.get("emails"), "primaryEmail") or "").casefold()
        if (label and label in normalized) or (email and email in normalized):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


class Orchestrator:
    """Planner/activator agent: plans intents and activates sub-agents."""

    def __init__(
        self,
        session_id: str = "default",
        model: str | None = None,
        *,
        compaction_token_limit: int = MEMORY_COMPACTION_TOKEN_LIMIT,
        resolver: CRMResolver | None = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.compaction_token_limit = compaction_token_limit
        # Mask/unmask is owned by the inner worker; exposed for REPL/trace tools.

        # One handle map shared with every sub-agent so handles stay consistent.
        self.pii_map = EntityHandleMap()
        # Deterministic name → CRM record resolution at mask time. Injectable for
        # tests; defaults to the reader-scoped bridge search.
        self.resolver = resolver or CRMResolver(build_bridge_search())
        # Candidates awaiting the user's disambiguation, set when a name in the
        # last message matched more than one record (see _resolve_message).
        self._pending: _PendingDisambiguation | None = None
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
        """Plan and activate sub-agents for one user message; return the result.

        Before any LLM work, the message's people/companies are resolved to CRM
        records and registered as handles in the shared map. If a name matches
        more than one record, we short-circuit and ask the user to choose rather
        than guessing — the chosen record is bound on the next turn.
        """
        # If we're waiting on a disambiguation, try to bind the user's choice and
        # resume the original request; otherwise treat this as a fresh message.
        message, gate = await self._resolve_turn(user_message)
        if gate is not None:
            self._turns.append({"role": "user", "content": user_message})
            self._turns.append({"role": "assistant", "content": gate})
            return {"response": gate, "tool_calls": []}

        await self._maybe_compact()
        prior = self._build_prior_messages()

        result = await self._worker.run(
            message, prior_messages=prior, on_event=on_event
        )

        # The writer interrupted for user approval — propagate without recording
        # the turn (the conversation hasn't actually advanced yet).
        if result.get("type") == "interrupt":
            return result

        self._turns.append({"role": "user", "content": user_message})
        self._turns.append({"role": "assistant", "content": result["response"]})
        return result

    # -- Mask-time resolution & disambiguation --------------------------

    async def _resolve_turn(self, user_message: str) -> tuple[str, str | None]:
        """Resolve entities for this turn into handles.

        Returns ``(message_to_run, gate)``. When *gate* is non-``None`` the turn
        is a clarifying question to the user and no sub-agent should run. The
        returned *message_to_run* is the original message, or — when a pending
        disambiguation is satisfied here — the message that triggered it.
        """
        message = user_message
        if self._pending is not None:
            # Try to bind the reply to a pending candidate; on success resume the
            # request that needed it. Either way the pending state is consumed —
            # if the reply was a new topic, it's re-resolved fresh below.
            if self._bind_pending_choice(user_message):
                message = self._pending.original_message
            self._pending = None

        ambiguities = await self._resolve_message(message)
        if ambiguities:
            self._pending = _PendingDisambiguation(
                original_message=message, ambiguities=ambiguities
            )
            return message, _format_disambiguation(ambiguities)
        return message, None

    async def _resolve_message(self, message: str) -> list[_Ambiguity]:
        """Detect entities, resolve people/companies, register handles.

        People are resolved with the single mentioned company as context (joint
        search with fallbacks lives in the resolver). Returns the list of names
        that matched several records.
        """
        spans = self.pii_map.detect(message)
        by_type: dict[str, list[str]] = {}
        for span in spans:
            entity_type = DEFAULT_LABEL_TO_TYPE.get(span.get("label", ""))
            surface = (span.get("text") or "").strip()
            if entity_type and surface:
                by_type.setdefault(entity_type, []).append(surface)

        companies = by_type.get("company", [])
        company_context = companies[0] if len(companies) == 1 else None

        ambiguities: list[_Ambiguity] = []
        for name in companies:
            if self._already_resolved(name):
                continue
            ambiguities += self._register(await self.resolver.resolve_company(name), name)
        for name in by_type.get("person", []):
            if self._already_resolved(name):
                continue
            ambiguities += self._register(
                await self.resolver.resolve_person(name, company_context), name
            )
        # Privacy-only entities (email/phone/location/url) are masked, not resolved.
        for entity_type, surfaces in by_type.items():
            if entity_type in RESOLVABLE_TYPES:
                continue
            for surface in surfaces:
                self.pii_map.register_privacy(entity_type, surface)
        return ambiguities

    def _already_resolved(self, name: str) -> bool:
        """True if *name* already maps to a CRM-backed handle (resolved earlier).

        Skips re-resolving — both within a turn and after a disambiguation choice
        is bound — so a name the user already pinned down isn't re-questioned.
        """
        handle = self.pii_map.handle_for_surface(name)
        return handle is not None and handle.is_resolved

    def _register(self, resolution: Resolution, name: str) -> list[_Ambiguity]:
        """Apply one resolution to the handle map; collect ambiguities.

        ``single`` → a resolved handle. ``none`` → a privacy handle (masked, but
        unresolved; the writer may create the record). ``multiple`` → deferred:
        no handle is created yet so the chosen record can cleanly claim the
        surface once the user disambiguates.
        """
        if resolution.status == "single" and resolution.record is not None:
            self.pii_map.register_resolved(resolution.entity_type, resolution.record)
            return []
        if resolution.status == "multiple":
            return [_Ambiguity(resolution.entity_type, name, resolution.records)]
        self.pii_map.register_privacy(resolution.entity_type, name)
        return []

    def _bind_pending_choice(self, reply: str) -> bool:
        """Bind the user's reply to pending candidates; True if all resolved."""
        assert self._pending is not None
        bound_any = False
        for ambiguity in self._pending.ambiguities:
            chosen = _match_choice(reply, ambiguity.candidates)
            if chosen is not None:
                self.pii_map.register_resolved(ambiguity.entity_type, chosen)
                bound_any = True
        return bound_any

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
