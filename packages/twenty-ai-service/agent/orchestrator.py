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

import os
import re
from dataclasses import dataclass, field
from typing import Any

from tracing import annotate_run, get_traceable

traceable = get_traceable()

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


# Default model for the orchestrator's planning loop when neither the caller nor
# the ORCHESTRATOR_MODEL env var specifies one. Kept distinct from the sub-agent
# default (env LLM_MODEL) so routing stays cheap/fast independent of the workers.
DEFAULT_ORCHESTRATOR_MODEL = "qwen/qwen3-next-80b-a3b-instruct"

# Replay the conversation verbatim until it exceeds this many tokens, then
# compact older turns into a summary. Tunable — start at 35k and watch.
MEMORY_COMPACTION_TOKEN_LIMIT = 35_000

# How many of the most recent turns to always keep verbatim when compacting.
RECENT_TURNS_KEPT = 6


_ORCHESTRATOR_PROMPT_TEMPLATE = """\
You are the Orchestrator of an agentic CRM. You sit in the chat interface and
coordinate specialist sub-agents — you never read or write the CRM yourself.

## Your sub-agents (this roster is complete and stable for the whole session)

{roster}

Every sub-agent takes ONE self-contained natural-language instruction and
returns ``{{"ok": true, "data": {{...}}}}`` on success or
``{{"ok": false, "error": {{...}}}}`` on failure.

## How to work

1. **Plan.** Break the user's message into an ordered list of concrete
   sub-tasks. Map each sub-task to exactly ONE sub-agent using the
   ``when_to_use`` cues above. Prefer the FEWEST agents that do the job — most
   messages need only the reader, or the reader and then the writer. Do not
   involve an agent a sub-task does not clearly call for.
2. **You already know your roster — do not re-discover it.** The list above is
   everything you can delegate to and does not change during the session. Only
   call ``get_agent_catalog`` / ``learn_agent`` if you need an agent's detailed
   input/output schema that the roster does not already give you, and learn ONLY
   the agents in your current plan. Never re-learn an agent you have already used
   this session.
3. **Read the handle list before planning — resolved vs private matters.**
   People and companies in the message are listed in the entity-handles section
   in one of two states:
   - **Resolved** (shown with ``fields:``, e.g. ``company002 (company) — fields:
     id, name``) — an existing CRM record. Pass the handle or a field
     (company002.id) straight through; do NOT ask the reader to "find" it again.
   - **Private** (shown as ``(…, private)``, e.g. ``company001 (company,
     private)``) — a name that matched NO existing record. Do NOT try to look it
     up; there is nothing to find. In a create/add request it is the thing to
     CREATE (hand it to the writer). Only send a private handle to the reader if
     the user's intent is genuinely to locate/match an existing record.
4. **Route by the verb, then order resolve → act → follow-up.** "Create / add /
   new X" is a WRITER task — never "find" an entity you are about to create.
   "Update / delete / advance" is also the writer, but first use the reader to
   resolve any *existing* record it must touch. Read only what you actually need
   to reference (e.g. to copy Notion's contact person onto a new company: reader
   gets company002's contact person, then the writer creates the new company with
   that person — you never look up the new company).
5. **Decompose relational lookups — never hand a sub-agent a riddle.** A request
   like "the email of the person who works at Notion" is TWO steps, not one:
   (a) make sure the company has a handle (it usually already does), then
   (b) delegate one reader call such as "List the people at company001". If a
   sub-task names a person *and* a company but only the company is resolved, ask
   the reader for that company's people first, then act on the result. Each
   delegated instruction must be executable on its own with the IDs/handles it
   carries.
   - **"What opportunity does <person> handle / is the contact for"** is the same
     two-step shape: FIRST have the reader resolve the person to an id, THEN ask
     the reader for the opportunity whose POINT OF CONTACT is that person id
     (e.g. "Find the opportunity whose point of contact is person <id>"). Never
     hand the reader the bare riddle "find the opportunity John handles" — it will
     guess the wrong field. A person handles a deal as its point of contact, not
     as its owner.
6. **Disambiguate, don't guess.** If the reader returns ``resolution: "multiple"``
   or ``"none"``, ask the user to choose instead of guessing. (Ambiguous names in
   the user's message are already turned into a clarifying question before you
   run.)
7. **Carry context across turns — reuse resolved ids, never re-resolve a
   pronoun.** Your only memory between turns is the conversation text, so you must
   keep resolved record ids IN that text:
   - When you report a record you (or the reader) resolved, ALWAYS include its id
     in your reply (e.g. "Airbnb — Platform Integration (id a45a119e-…)"). This is
     what lets the next turn refer back to it.
   - When the user says "it", "that deal", "this person", etc., they mean the
     record most recently in focus. REUSE the id already established earlier in
     the conversation — pass it straight to the reader/writer. Do NOT re-derive
     the record from a relationship again (that is how you lose the thread and
     pull up the wrong record). Only re-resolve if no id was ever established.
   - ``remember`` / ``recall`` / ``get_session_context`` are also available for
     the record currently in focus.
8. **Wrap up.** When all sub-tasks are done, reply to the user with one
   consolidated, natural-language summary of what happened.

## Composite operations (plan for ONE delegated call, not many)

Your sub-agents can collapse a whole multi-record operation into a single call —
plan for that instead of choreographing many.

- **Rich reads in one reader call.** For "everything about X", a full profile, an
  activity timeline, what's linked to a record, or an account-health/overview
  request, send the reader ONE instruction — it has composite reads that fan out
  internally. Do NOT split such a request into several reader calls.
- **Composite reads need an id.** They only work on an already-identified record.
  If the record isn't resolved yet, the reader resolves it first (or you resolve
  the handle), then the rich read runs — still one reader task.
- **Multi-step writes in one writer call.** For onboard / close deal / change
  budget / schedule review / reassign account / bulk stage move / send proposal,
  hand the writer ONE instruction — it has composite writes that create or update
  the whole cluster (records + notes + tasks) at once. Do NOT issue several writer
  calls for the parts.
- **id-first handoff.** Writers act on ids, not names. Resolve any *existing*
  record with the reader first, then pass its id to the writer (the only exception
  is creating brand-new records, e.g. onboarding, where there is nothing to resolve).

## Persistence — never give up at the first error

A sub-agent returning ``{{"ok": false, ...}}``, an error, or an empty/"not
found" result is a signal to RECOVER, not to stop. Before you EVER tell the user
you could not do something, you MUST have already tried the obvious fix yourself
in the same turn:

- **A name or handle is never a dead end.** The most common failure is an id
  being needed where a name/handle was passed (e.g. "expected a UUID, got
  company001"). The fix is always the same: send the reader to resolve that
  name/handle to a real record id, then retry the original call with the id. Do
  this automatically — never bounce it back to the user as "I need to resolve it
  first" or "the handle needs to be translated". That translation is YOUR job.
- **"Not found" usually means look the other way.** If a record "doesn't appear
  to have" something, try the relationship the request implies before concluding
  nothing exists. "What opportunity does X handle / own / manage" → have the
  reader find the opportunity whose point of contact (or owner) is X — do not
  answer "none" off a single failed lookup.
- **Retry the corrected call, THEN report.** Only surface an inability to the
  user after a genuine resolve→act recovery attempt has ALSO failed, and say
  specifically what you tried. One failed tool call is never a final answer.
- **Linking is always possible.** A note or task that "can't be linked" simply
  needs its target join row. Get the entity's id from the reader and instruct
  the writer to call ``create_note_target`` / ``create_task_target`` with that
  id (``targetPersonId`` / ``targetCompanyId`` / ``targetOpportunityId``). Never
  tell the user a note/task cannot be attached, and never settle for an unlinked
  record — an unlinked note/task is a failure, not a partial success.

## Rules

- Delegate; do not fabricate CRM data or invent record ids.
- React to `{{"ok": false, "error": {{...}}}}` results — don't ignore them.
- Identity (workspace, role, user) is handled automatically — never ask about it.
- **Linking tasks & notes:** When asking the writer to create a task or note, you MUST name EVERY relevant entity it should attach to — person AND/OR company AND/OR opportunity — by passing their handles/ids in the instruction, and explicitly tell the writer to link the record to each of them (one `create_task_target` / `create_note_target` row per entity). "Add a note to <person>" must link the note to that person — a person-only note is valid and common, not just deal notes. A task/note with no target floats invisibly and is a failure.
"""


def _format_roster(roster: list[dict[str, Any]]) -> str:
    """Render the registry roster as a compact, model-readable block.

    One stanza per agent: name (+STUB marker), role, when to use it, and how to
    phrase the instruction. Embedding this in the system prompt means the roster
    is present every turn, so the orchestrator never has to re-run discovery to
    remember what its sub-agents are (the discovery tool traces do not persist
    across turns — only this prompt and the replayed turn text do).
    """
    stanzas: list[str] = []
    for entry in roster:
        marker = " [STUB — not yet implemented]" if entry.get("is_stub") else ""
        stanzas.append(
            f"### {entry['name']}{marker}\n"
            f"- Role: {entry['role']}\n"
            f"- When to use: {entry['when_to_use']}\n"
            f"- How to instruct: {entry['how_to_instruct']}"
        )
    return "\n\n".join(stanzas)


def build_orchestrator_system_prompt(roster: list[dict[str, Any]]) -> str:
    """Build the orchestrator system prompt with the live agent roster embedded."""
    return _ORCHESTRATOR_PROMPT_TEMPLATE.format(roster=_format_roster(roster))


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
        orchestrator_model: str | None = None,
        compaction_token_limit: int = MEMORY_COMPACTION_TOKEN_LIMIT,
        resolver: CRMResolver | None = None,
    ) -> None:
        self.session_id = session_id
        # Two models, decoupled on purpose: the orchestrator's own planning loop
        # (and memory compaction) runs on ORCHESTRATOR_MODEL — a fast model for
        # cheap routing — while sub-agents (reader/writer) fall back to the env
        # LLM_MODEL default unless an explicit *model* is passed.
        self.model = model
        self.orchestrator_model = (
            orchestrator_model
            or os.environ.get("ORCHESTRATOR_MODEL")
            or DEFAULT_ORCHESTRATOR_MODEL
        )
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

        # Embed the live roster in the system prompt so the orchestrator always
        # knows its sub-agents without re-running discovery every turn.
        system_prompt = build_orchestrator_system_prompt(
            self.registry.roster(ORCHESTRATOR_SCOPE)
        )

        self._worker = BaseWorker(
            scope=READER_SCOPE,  # unused — tools_override wins
            system_prompt=system_prompt,
            session_id=session_id,
            model=self.orchestrator_model,
            pii_map=self.pii_map,
            tools_override=tools,
            label="orchestrator",
            # Agent-discovery results are control metadata (agent names, schemas),
            # not CRM data — never mask them. delegate_to_agent IS masked, since
            # it carries the sub-agent's CRM payload back to the planner.
            unmasked_tools=frozenset({"get_agent_catalog", "learn_agent"}),
        )

        # Conversation memory: a running summary of compacted-away older turns,
        # plus recent turns kept verbatim as {"role", "content"} dicts.
        self._summary: str | None = None
        self._turns: list[dict[str, str]] = []

    @traceable(name="Orchestrator.handle", run_type="chain")
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
        # Tag the top-level turn span so a trace is searchable by session and
        # shows the request at a glance without drilling into the tree.
        annotate_run(
            metadata={
                "session_id": self.session_id,
                "user_message": user_message[:300],
            },
            tags=["orchestrator", "turn"],
        )

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

    @traceable(name="Orchestrator.compact_memory", run_type="chain")
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
        client = LLMClient(model=self.orchestrator_model)
        openai_client = client.get_openai_client()
        response = openai_client.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": "You summarise conversations faithfully and tersely."},
                {"role": "user", "content": instruction},
            ],
        )
        return response.choices[0].message.content or (prior_summary or "")


async def delegate_write(
    instruction: str,
    *,
    pii_map: Any = None,
    session_id: str,
    model: str | None = None,
    auto_approve: bool = False,
) -> dict[str, Any]:
    """Orchestrator → writer seam: run one write through the writer sub-agent.

    Feature code (e.g. the follow-up agent) must NOT construct or call the writer
    directly — it reaches the writer through this seam. The caller owns the handle
    map: it has hidden the write's content behind handles (real ids stay real for
    targeting). The writer unmasks tool args at its execute step via ``pii_map``,
    so the writer LLM only ever sees handles — never the real content.

    When ``auto_approve`` is set, the caller has already obtained the user's
    approval for the write (e.g. a follow-up action the rep explicitly accepted),
    so the writer's tier-3 confirmation gate is auto-confirmed instead of being
    surfaced as an interrupt. The same worker instance (and its checkpointer) is
    reused to resume.

    Returns the writer's result dict (``{"response", "tool_calls", "type"}`` or an
    interrupt payload).
    """
    from agent.workers.writer_worker import WriterWorker

    # With auto_approve the writer's tier-3 gate never interrupts, so the write
    # runs to completion in a single pass (no nested-interrupt to resume — which
    # would otherwise raise when the writer runs inside the accept graph).
    writer = WriterWorker(
        session_id=session_id, model=model, pii_map=pii_map, auto_approve=auto_approve
    )
    return await writer.run(instruction)


orchestrator = Orchestrator()
