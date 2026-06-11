"""Agent Registry — the in-process catalog of sub-agents.

This is the agent-layer parallel to the Node bridge's *tool* catalog.  The one
structural difference: CRM tools live in Node and are fetched over
``bridge_client.forward``, whereas sub-agents are in-process Python objects, so
this registry is a local data structure rather than a remote call.

Each sub-agent is described by an ``AgentSpec`` exposing:

- ``name`` / ``role`` — the lightweight catalog view (parallels a tool catalog
  entry's name + description).
- ``description`` / ``input_schema`` / ``output_schema`` — the detailed
  "how to interact" view returned by ``learn`` (parallels ``learn_tools``).
- ``invoke`` — an async callable taking a natural-language instruction and
  returning a result dict.  This signature matches ``BaseWorker.run`` (which
  returns ``{"response", "tool_calls"}``), so wrapping a worker is trivial.

``build_default_registry`` wires Reader/Writer (real workers) and Follow-up/
Researcher (stubs).  Workers are constructed lazily *inside* the function — not
at module import — to avoid eager coupling to the workers package (mirroring how
``crm_tools`` defers its imports to dodge the circular dependency).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent.agent_scope import AgentScope, is_agent_allowed
from agent.masking import EntityHandleMap


# A free-text instruction in, a result dict out.
AgentInvoke = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class AgentSpec:
    """Describes one discoverable sub-agent."""

    name: str
    role: str  # one-line catalog description
    when_to_use: str  # routing cue: which kinds of sub-tasks belong to this agent
    description: str  # fuller "how to interact" text for learn_agent
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    invoke: AgentInvoke
    is_stub: bool = False

    def catalog_entry(self) -> dict[str, str]:
        """Lightweight view returned by get_agent_catalog."""
        return {"name": self.name, "role": self.role, "when_to_use": self.when_to_use}

    def roster_entry(self) -> dict[str, Any]:
        """Planner-facing view embedded directly in the orchestrator prompt.

        Carries enough to route and phrase a delegation without a learn_agent
        round-trip: role, routing cue, and how to instruct the agent.
        """
        return {
            "name": self.name,
            "role": self.role,
            "when_to_use": self.when_to_use,
            "how_to_instruct": self.description,
            "is_stub": self.is_stub,
        }

    def learn_entry(self) -> dict[str, Any]:
        """Detailed interaction schema returned by learn_agent."""
        return {
            "name": self.name,
            "role": self.role,
            "when_to_use": self.when_to_use,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "is_stub": self.is_stub,
        }


@dataclass
class AgentRegistry:
    """An in-process catalog of ``AgentSpec`` objects, keyed by name."""

    _agents: dict[str, AgentSpec] = field(default_factory=dict)

    def register(self, spec: AgentSpec) -> None:
        # Keyed lower-case so lookups tolerate model casing ("Reader" → reader).
        self._agents[spec.name.lower()] = spec

    def get(self, name: str) -> AgentSpec | None:
        return self._agents.get(name.lower())

    def catalog(self, scope: AgentScope) -> list[dict[str, str]]:
        """Lightweight entries for agents within *scope* (parallels tool catalog)."""
        return [
            spec.catalog_entry()
            for spec in self._agents.values()
            if is_agent_allowed(spec.name, scope)
        ]

    def learn(self, names: list[str], scope: AgentScope) -> list[dict[str, Any]]:
        """Detailed entries for the named in-scope agents (parallels learn_tools)."""
        entries = []
        for name in names:
            spec = self._agents.get(name.lower())
            if spec is not None and is_agent_allowed(spec.name, scope):
                entries.append(spec.learn_entry())
        return entries

    def roster(self, scope: AgentScope) -> list[dict[str, Any]]:
        """Planner-facing entries for every in-scope agent.

        Richer than ``catalog`` (adds routing + how-to-instruct), used to embed
        the full, stable roster in the orchestrator's system prompt so it can
        route and delegate without re-running discovery every turn.
        """
        return [
            spec.roster_entry()
            for spec in self._agents.values()
            if is_agent_allowed(spec.name, scope)
        ]


# ---------------------------------------------------------------------------
# Schemas — documented once so the planner knows how to interact
# ---------------------------------------------------------------------------

_INSTRUCTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instruction": {
            "type": "string",
            "description": "A single, self-contained natural-language task.",
        }
    },
    "required": ["instruction"],
}

# Mirrors the reader's documented resolution contract (see reader_worker.py) so
# the planner knows it must resolve an entity before asking the writer to act.
_READER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Reader returns a structured resolution in `response` (JSON).",
    "properties": {
        "resolution": {"enum": ["single", "multiple", "none"]},
        "entity_type": {"type": "string"},
        "record": {"type": "object", "description": "Present when resolution=single"},
        "candidates": {"type": "array", "description": "Present when resolution=multiple"},
    },
}

_WRITER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Writer returns a natural-language confirmation in `response`.",
    "properties": {
        "response": {"type": "string"},
    },
}

_STUB_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"const": "stub"},
        "agent": {"type": "string"},
        "message": {"type": "string"},
    },
}


def build_default_registry(
    session_id: str = "default",
    model: str | None = None,
    pii_map: EntityHandleMap | None = None,
) -> AgentRegistry:
    """Build the registry the orchestrator uses: reader/writer + stub agents.

    Reader and Writer wrap real ``BaseWorker`` instances sharing *session_id*,
    *model*, and *pii_map* with the orchestrator so PII tokens stay consistent
    across the whole session.  Follow-up and Researcher are stubs.
    """
    # Deferred imports — keep this module decoupled from the workers package at
    # import time (the workers import chain pulls in crm_tools/write_policy).
    from agent.stubs.agent_stubs import followup_stub, researcher_stub
    from agent.workers.reader_worker import ReaderWorker
    from agent.workers.writer_worker import WriterWorker

    reader = ReaderWorker(session_id=session_id, model=model, pii_map=pii_map)
    writer = WriterWorker(session_id=session_id, model=model, pii_map=pii_map)

    async def _invoke_reader(instruction: str) -> dict:
        return await reader.run(instruction)

    async def _invoke_writer(instruction: str) -> dict:
        return await writer.run(instruction)

    registry = AgentRegistry()
    registry.register(
        AgentSpec(
            name="reader",
            role="Looks up and resolves existing CRM records (people, companies, opportunities, notes, tasks) to concrete records + IDs.",
            when_to_use=(
                "Any sub-task that needs to find, list, read, or resolve existing "
                "data — turn a name or handle into a record, list a company's people "
                "or deals, fetch a record's details. Always run the reader BEFORE the "
                "writer so writes act on resolved IDs."
            ),
            description=(
                "Give ONE self-contained lookup about ONE target. The reader resolves "
                "a single entity per call and CANNOT chase a relationship it was not "
                "handed. For relational lookups, pass the anchor you already have: "
                "e.g. 'List the people at company001' or 'Find the person named Dana "
                "at company001'. NEVER send a riddle like 'find the email of the "
                "person who works at Notion' — first resolve Notion to a company "
                "handle, then ask the reader for that company's people. It returns a "
                "structured resolution: single | multiple | none."
            ),
            input_schema=_INSTRUCTION_INPUT_SCHEMA,
            output_schema=_READER_OUTPUT_SCHEMA,
            invoke=_invoke_reader,
        )
    )
    registry.register(
        AgentSpec(
            name="writer",
            role="Creates, updates, deletes, or advances CRM records, enforcing write policies.",
            when_to_use=(
                "Any sub-task that CHANGES data — create / update / delete a record, "
                "advance a deal stage. Run only AFTER the reader has resolved every "
                "target the write touches to a handle or ID."
            ),
            description=(
                "Send a specific write instruction that already contains the handles "
                "or IDs the reader resolved (e.g. 'Update opportunity002 amount to "
                "50000'). Do NOT ask the writer to search or resolve names — that is "
                "the reader's job. High-risk writes (deletes, terminal stage moves, "
                "bulk updates) pause for the user's approval automatically."
            ),
            input_schema=_INSTRUCTION_INPUT_SCHEMA,
            output_schema=_WRITER_OUTPUT_SCHEMA,
            invoke=_invoke_writer,
        )
    )
    registry.register(
        AgentSpec(
            name="followup",
            role="Handles calendar scheduling, reminders, and draft preparation.",
            when_to_use=(
                "Scheduling, reminders, or draft preparation that runs after the core "
                "read/write work (e.g. book a meeting, draft a follow-up email). STUB."
            ),
            description=(
                "Delegate scheduling/reminder/draft tasks here (e.g. 'Book a 1-hour "
                "meeting at 9:30am Monday and prepare a draft'). Currently a stub."
            ),
            input_schema=_INSTRUCTION_INPUT_SCHEMA,
            output_schema=_STUB_OUTPUT_SCHEMA,
            invoke=followup_stub,
            is_stub=True,
        )
    )
    registry.register(
        AgentSpec(
            name="researcher",
            role="Gathers background details or external/internal context.",
            when_to_use=(
                "Enrichment or background research that is not already in the CRM "
                "(e.g. recent news about a company). STUB."
            ),
            description=(
                "Delegate research/enrichment tasks here (e.g. 'Find recent news "
                "about Acme Corp'). Currently a stub."
            ),
            input_schema=_INSTRUCTION_INPUT_SCHEMA,
            output_schema=_STUB_OUTPUT_SCHEMA,
            invoke=researcher_stub,
            is_stub=True,
        )
    )
    return registry
