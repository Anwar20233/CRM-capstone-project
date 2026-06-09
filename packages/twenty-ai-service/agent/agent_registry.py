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
from agent.masking import PIISessionMap


# A free-text instruction in, a result dict out.
AgentInvoke = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class AgentSpec:
    """Describes one discoverable sub-agent."""

    name: str
    role: str  # one-line catalog description
    description: str  # fuller "how to interact" text for learn_agent
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    invoke: AgentInvoke
    is_stub: bool = False

    def catalog_entry(self) -> dict[str, str]:
        """Lightweight view returned by get_agent_catalog."""
        return {"name": self.name, "role": self.role}

    def learn_entry(self) -> dict[str, Any]:
        """Detailed interaction schema returned by learn_agent."""
        return {
            "name": self.name,
            "role": self.role,
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
    pii_map: PIISessionMap | None = None,
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
            role="Resolves people, companies, opportunities, notes, and tasks to concrete CRM records.",
            description=(
                "Send a natural-language lookup instruction (e.g. 'Find the person "
                "John who we just had a call with'). The reader resolves the entity "
                "and returns a structured resolution. Use it to turn names into "
                "record IDs BEFORE asking the writer to change anything."
            ),
            input_schema=_INSTRUCTION_INPUT_SCHEMA,
            output_schema=_READER_OUTPUT_SCHEMA,
            invoke=_invoke_reader,
        )
    )
    registry.register(
        AgentSpec(
            name="writer",
            role="Creates, updates, or deletes CRM records following write policies.",
            description=(
                "Send a specific write instruction including any record IDs the "
                "reader resolved (e.g. 'Update opportunity <id> amount to 50000'). "
                "High-risk writes may require a confirmation step. Do not ask the "
                "writer to search — resolve entities with the reader first."
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
