"""Tool Capability Registry — read/write scope enforcement for agent workers.

This module is the **single source of truth** for which bridge tools a given
worker is allowed to see and execute.  Three independent enforcement layers
(defense-in-depth):

    1. Catalog filtering — a worker's ``get_tool_catalog`` only returns names
       in its scope.
    2. Execute guard — ``is_tool_allowed`` rejects any tool name outside scope
       *before* it reaches the bridge.
    3. Bridge role identity — each scope carries a role-id env-var name so the
       reader forwards a read-only ``roleId`` and the writer forwards a
       write-capable one (Twenty's ``database-tool.provider.ts`` does the final
       enforcement on its side).

Capabilities
~~~~~~~~~~~~
Each tool is tagged with exactly one capability from its verb prefix:

    read   — find_*, find_one_*, group_by_*, get_*, search_*, list_*
    write  — create_*, create_many_*, update_*, update_many_*, delete_*,
             advance_*, link_*, transfer_*, merge_*, restore_*
    meta   — the four progressive-disclosure meta-tools

Safety / session / tier checks are **not** capabilities that appear in any
scope — they are invisible middleware enforced structurally inside
``execute_tool``, never exposed to the LLM.

A ``ToolScope`` bundles a name, the set of allowed capabilities, and the env-var
that holds the Twenty role-id for bridge calls.

Configurable allow/deny
~~~~~~~~~~~~~~~~~~~~~~~~
Two module-level collections are the editable surface for tuning what workers
can reach (add/remove entries anytime):

    _CAPABILITY_OVERRIDES — assign a capability to tools the prefix rule misses
                            (mainly ACTION-category tools like ``send_email``).
    DENIED_TOOLS          — hard deny-list; these are never exposed to any
                            worker (mirrors Twenty's ``MCP_EXCLUDED_TOOL_NAMES``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------

class Capability(str, Enum):
    """The kind of operation a tool performs.

    Only READ, WRITE, and META appear in worker scopes.  INTERNAL is used to
    classify tools that exist but are never directly exposed (safety/session
    stubs) — they are called by the structural middleware, not the LLM.
    """

    READ = "read"
    WRITE = "write"
    META = "meta"
    INTERNAL = "internal"  # safety, session — never in any scope


# ---------------------------------------------------------------------------
# Verb-prefix → capability classification
# ---------------------------------------------------------------------------

# Ordered longest-prefix-first so ``create_many_`` matches before ``create_``.
_READ_PREFIXES: tuple[str, ...] = (
    "find_one_",
    "find_",
    "group_by_",
    "get_",
    "search_",
    "list_",
)

_WRITE_PREFIXES: tuple[str, ...] = (
    "create_many_",
    "create_",
    "update_many_",
    "update_",
    "delete_many_",
    "delete_",
    "advance_",
    "link_",
    "transfer_",
    "merge_",
    "restore_",
)

# Meta-tools (the four progressive-disclosure tools the LLM actually binds to).
_META_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_tool_catalog",
        "learn_tools",
        "execute_tool",
        "get_current_user",
    }
)

# Reserved tool names that are never LLM-facing: write-policy middleware helpers
# (e.g. lookup_action_tier) and orchestrator-owned session helpers.  Reserving
# the names keeps them out of every worker's scope even if the bridge ever
# surfaces a tool of the same name.
_INTERNAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "lookup_action_tier",
        "check_conflicts",
        "resolve_date",
        "session_set_topic",
        "session_get_topic",
        "session_log_write",
        "session_get_write_log",
        "session_check_duplicate",
    }
)

# ---------------------------------------------------------------------------
# Configurable allow/deny surface  (edit these to add or remove tools anytime)
# ---------------------------------------------------------------------------

# Explicit capability overrides for tools whose name breaks the verb-prefix
# rule — chiefly the ACTION-category tools, which have no read/write prefix and
# would otherwise fall through to the default.  Add an entry here to (re)assign
# a tool to a scope.
_CAPABILITY_OVERRIDES: dict[str, Capability] = {
    # Outbound actions — writer-only.
    "send_email": Capability.WRITE,
    "draft_email": Capability.WRITE,
    # Read-only helpers.
    "search_help_center": Capability.READ,
    # Add ACTION/VIEW/WORKFLOW tools here as the catalog grows.
}

# Hard deny-list: tools no worker may ever see or execute, regardless of scope
# or capability.  Mirrors Twenty's ``MCP_EXCLUDED_TOOL_NAMES``.  ``is_tool_allowed``
# rejects these before any capability check.  Add/remove names freely.
DENIED_TOOLS: frozenset[str] = frozenset(
    {
        "code_interpreter",
        "http_request",
    }
)


def classify_tool(tool_name: str) -> Capability:
    """Return the capability of *tool_name* using prefix rules + overrides.

    Falls back to ``Capability.READ`` for unknown names (safe default — the
    reader scope includes READ so unknown tools won't be silently hidden from
    everyone).
    """
    # 1. Explicit overrides win.
    if tool_name in _CAPABILITY_OVERRIDES:
        return _CAPABILITY_OVERRIDES[tool_name]

    # 2. Meta / internal — exact name match.
    if tool_name in _META_TOOL_NAMES:
        return Capability.META
    if tool_name in _INTERNAL_TOOL_NAMES:
        return Capability.INTERNAL

    # 3. Verb-prefix classification (longest prefix first).
    for prefix in _WRITE_PREFIXES:
        if tool_name.startswith(prefix):
            return Capability.WRITE
    for prefix in _READ_PREFIXES:
        if tool_name.startswith(prefix):
            return Capability.READ

    # 4. Unknown — default to READ (conservative).
    return Capability.READ


def is_write_tool(tool_name: str) -> bool:
    """Return ``True`` if *tool_name* is a CRM write operation.

    Used by the write-policy middleware to decide whether to gate a call.
    """
    return classify_tool(tool_name) == Capability.WRITE


# ---------------------------------------------------------------------------
# ToolScope dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolScope:
    """Declares a worker's allowed tool capabilities and its bridge role."""

    name: str
    allowed_capabilities: frozenset[Capability]
    role_env_var: str  # e.g. "TWENTY_READER_ROLE_ID"

    @property
    def role_id(self) -> str:
        """Resolve the Twenty role id from the environment.

        Falls back to the generic ``TWENTY_ROLE_ID`` if the scope-specific
        variable is not set (smooth migration for existing single-role setups).
        """
        value = os.environ.get(self.role_env_var) or os.environ.get("TWENTY_ROLE_ID")
        if not value:
            raise RuntimeError(
                f"Neither {self.role_env_var} nor TWENTY_ROLE_ID is set"
            )
        return value


# ---------------------------------------------------------------------------
# Pre-built scopes
# ---------------------------------------------------------------------------

READER_SCOPE = ToolScope(
    name="reader",
    allowed_capabilities=frozenset({Capability.READ, Capability.META}),
    role_env_var="TWENTY_READER_ROLE_ID",
)

WRITER_SCOPE = ToolScope(
    name="writer",
    allowed_capabilities=frozenset({Capability.WRITE, Capability.META}),
    role_env_var="TWENTY_WRITER_ROLE_ID",
)


# ---------------------------------------------------------------------------
# Guard / filter helpers
# ---------------------------------------------------------------------------

def is_tool_allowed(tool_name: str, scope: ToolScope) -> bool:
    """Return ``True`` if *tool_name* may be used within *scope*.

    Denied tools (``DENIED_TOOLS``) are rejected first, regardless of scope;
    otherwise the tool's capability must be in the scope's allowed set.
    """
    if tool_name in DENIED_TOOLS:
        return False
    return classify_tool(tool_name) in scope.allowed_capabilities


def filter_catalog(
    catalog_entries: list[dict[str, Any]],
    scope: ToolScope,
) -> list[dict[str, Any]]:
    """Return only the catalog entries whose tools fall within *scope*.

    Each *catalog_entry* must have a ``"name"`` key.  Entries whose name is
    not in scope are silently dropped — the worker never even knows they exist.
    """
    return [
        entry
        for entry in catalog_entries
        if is_tool_allowed(entry.get("name", ""), scope)
    ]
