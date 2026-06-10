"""CRMResolver — deterministic name → CRM record resolution for the mask layer.

When the masking layer detects a PERSON or COMPANY in the user's message, it
needs the concrete CRM record (id + fields) so the resulting handle can be used
in tool arguments. That resolution happens here, deterministically and *before*
the LLM runs — not via an LLM "reader" turn.

Behaviour the orchestrator relies on:

- **Normalized, fuzzy search.** Names are normalized and matched with the find
  tools' case-insensitive ``ilike`` operator, so casing/typos/partial names in
  the user's input still find the record.
- **Joint person + company with fallbacks.** When a message mentions both a
  person and a company, the person search is narrowed by the company, then falls
  back to name-only if that yields nothing — but a step that yields *several*
  matches routes to disambiguation rather than falling through.
- **Three outcomes.** ``single`` (one record → build a handle), ``multiple``
  (several → orchestrator asks the user), ``none`` (build a privacy handle / let
  the writer create a record).

The CRM is reached through an injected async ``search(tool, args)`` callable so
this module has no hard dependency on the bridge and is trivial to unit-test.
``build_bridge_search`` wires the real reader-scoped bridge call.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# search(tool_name, args) -> bridge envelope ({"ok": bool, "data"/"error": ...}).
Search = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_SEARCH_LIMIT = 10

# The Twenty find tools for the two resolvable entity types.
PERSON_TOOL = "find_people"
COMPANY_TOOL = "find_companies"


def _ilike(value: str) -> str:
    """Normalized case-insensitive contains-pattern (escapes LIKE wildcards)."""
    normalized = re.sub(r"\s+", " ", value).strip()
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass
class Resolution:
    """The outcome of resolving one name."""

    status: str  # "single" | "multiple" | "none"
    entity_type: str  # "person" | "company"
    query: str
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def record(self) -> dict[str, Any] | None:
        return self.records[0] if self.status == "single" else None


class CRMResolver:
    """Resolves person/company names to CRM records via an injected search call."""

    def __init__(self, search: Search, *, limit: int = _SEARCH_LIMIT) -> None:
        self._search = search
        self._limit = limit

    async def resolve_company(self, name: str) -> Resolution:
        """Resolve a company name to a record."""
        records = await self._run(COMPANY_TOOL, {"name": {"ilike": _ilike(name)}})
        return _outcome("company", name, records)

    async def resolve_person(self, name: str, company_name: str | None = None) -> Resolution:
        """Resolve a person, optionally narrowed by an accompanying company.

        Falls back to a name-only search when the company-narrowed search finds
        nothing; a narrowed search that finds *several* still returns
        ``multiple`` so the orchestrator can disambiguate.
        """
        company_id: str | None = None
        if company_name:
            company = await self.resolve_company(company_name)
            if company.status == "single" and company.record:
                company_id = company.record.get("id")

        if company_id:
            narrowed = await self._search_people(name, company_id)
            if narrowed.status != "none":
                return narrowed
            # Fallback: the pairing didn't match — widen to name-only.

        return await self._search_people(name, None)

    # -- Internal -------------------------------------------------------

    async def _search_people(self, name: str, company_id: str | None) -> Resolution:
        name_filter = _person_name_filter(name)
        if company_id:
            args: dict[str, Any] = {"and": [{"company": {"eq": company_id}}, name_filter]}
        else:
            args = name_filter
        records = await self._run(PERSON_TOOL, args)
        return _outcome("person", name, records)

    async def _run(self, tool: str, filter_args: dict[str, Any]) -> list[dict[str, Any]]:
        envelope = await self._search(tool, {"limit": self._limit, **filter_args})
        return _records(envelope)


def _person_name_filter(name: str) -> dict[str, Any]:
    """Build a people filter from a (possibly partial) name.

    Multi-word names match first AND last name; a single token matches either.
    """
    tokens = re.sub(r"\s+", " ", name).strip().split(" ")
    if len(tokens) >= 2:
        return {
            "name": {
                "firstName": {"ilike": _ilike(tokens[0])},
                "lastName": {"ilike": _ilike(tokens[-1])},
            }
        }
    return {
        "or": [
            {"name": {"firstName": {"ilike": _ilike(name)}}},
            {"name": {"lastName": {"ilike": _ilike(name)}}},
        ]
    }


def _outcome(entity_type: str, query: str, records: list[dict[str, Any]]) -> Resolution:
    if not records:
        status = "none"
    elif len(records) == 1:
        status = "single"
    else:
        status = "multiple"
    return Resolution(status=status, entity_type=entity_type, query=query, records=records)


def _records(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the record list out of a bridge envelope, tolerant of its shape."""
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return []
    data = envelope.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        # Find the first value that is a list of record-shaped dicts.
        for value in data.values():
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return []


def build_bridge_search(scope: Any = None) -> Search:
    """Wire a reader-scoped ``search`` callable backed by the Node bridge.

    Mirrors ``crm_tools.execute_tool`` but trimmed to the resolver's needs (no
    write policy, no LLM-facing envelope) and reusing the same identity resolver.
    """
    from bridge_client import forward
    from agent.crm_tools import _identity
    from agent.tool_scope import READER_SCOPE

    resolved_scope = scope or READER_SCOPE

    async def search(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        identity = _identity(resolved_scope)
        return await forward(
            "execute",
            {
                "tool": tool,
                "args": args,
                "workspaceId": identity["workspace_id"],
                "roleId": identity["role_id"],
                "userId": identity["user_id"],
            },
        )

    return search
