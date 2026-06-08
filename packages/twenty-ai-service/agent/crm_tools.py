"""Twenty CRM tools for the internal LangGraph agent.

Twenty exposes ~254 tools — too many to bind to an LLM directly (context blowup,
poor selection). So instead of 254 tools the agent gets three *meta-tools* and
discovers the rest at runtime:

    get_tool_catalog  -> browse what exists
    learn_tools       -> fetch exact input schemas for the ones it needs
    execute_tool      -> run any tool by name

Plus get_current_user for "act as me" attribution.

Workspace / role / user identity is injected here from configuration, never by
the LLM — the model only ever deals with tool names and arguments. This module
is also the natural place to enforce authorization/policy before forwarding to
the (intentionally unguarded) Node bridge.

Scope-aware factory
~~~~~~~~~~~~~~~~~~~
``build_crm_tools(scope)`` produces meta-tools that close over a ``ToolScope``:

- ``get_tool_catalog`` calls the bridge, then ``filter_catalog(…, scope)`` so
  the worker only sees tool names within its allowed capabilities.
- ``learn_tools`` errors if asked for an out-of-scope name.
- ``execute_tool`` guards: rejects out-of-scope tools *before* the bridge call.
  For writer scopes with a ``WritePolicy``, write tools are transparently gated
  (tier check + confirmation tokens for high-risk actions) — the LLM never knows
  the middleware exists.
- ``get_current_user`` is unchanged.

The legacy ``get_crm_tools()`` still works — it delegates to
``build_crm_tools(WRITER_SCOPE)`` for backward compatibility.

Usage::

    from agent.crm_tools import build_crm_tools
    from agent.tool_scope import READER_SCOPE

    tools = build_crm_tools(READER_SCOPE)
    model = model.bind_tools(tools)
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.tools import StructuredTool

from bridge_client import forward
from agent.tool_scope import (
    ToolScope,
    WRITER_SCOPE,
    filter_catalog,
    is_tool_allowed,
    is_write_tool,
)
from agent.workers.write_policy import WritePolicy


# ---------------------------------------------------------------------------
# Identity resolver
# ---------------------------------------------------------------------------

def _identity(scope: ToolScope) -> dict[str, str]:
    """Resolve the calling identity from configuration + scope.

    Deliberately out of the LLM-facing tool signatures: the model must never
    pass workspace/user UUIDs. Raises a clear error if misconfigured.

    The *role_id* is resolved from the scope (reader vs writer role), while
    workspace and user remain shared across all scopes.
    """
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    user_id = os.environ.get("TWENTY_USER_ID")

    try:
        role_id = scope.role_id  # reads scope.role_env_var, falls back to TWENTY_ROLE_ID
    except RuntimeError:
        role_id = None

    missing = [
        name
        for name, value in {
            "TWENTY_WORKSPACE_ID": workspace_id,
            scope.role_env_var + " (or TWENTY_ROLE_ID)": role_id,
            "TWENTY_USER_ID": user_id,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return {"workspace_id": workspace_id, "role_id": role_id, "user_id": user_id}


# ---------------------------------------------------------------------------
# Scoped meta-tool factory
# ---------------------------------------------------------------------------

def build_crm_tools(
    scope: ToolScope,
    *,
    write_policy: WritePolicy | None = None,
) -> list[StructuredTool]:
    """Build scope-filtered LangChain meta-tools for a worker.

    The returned tools close over *scope* so that:
    - catalog results are filtered to the scope's capabilities.
    - learn_tools rejects out-of-scope tool names.
    - execute_tool guards against out-of-scope calls before forwarding.
    - all bridge calls use the scope's role_id.

    If *write_policy* is provided, ``execute_tool`` transparently runs the
    tier/safety/duplicate protocol on every write — the LLM sees either a
    normal result (tier 1/2) or a ``CONFIRMATION_REQUIRED`` error with a
    token it must pass back (tier 3).
    """

    # -- get_tool_catalog ------------------------------------------------

    async def _get_tool_catalog(categories: list[str] | None = None) -> dict:
        """List the CRM tools available, grouped by category.

        Categories: DATABASE_CRUD, ACTION, WORKFLOW, METADATA, VIEW, VIEW_FIELD,
        DASHBOARD, LOGIC_FUNCTION. Pass ``categories`` to filter. Returns
        lightweight entries (name + description); call learn_tools for full
        input schemas.
        """
        ident = _identity(scope)
        payload: dict = {
            "workspaceId": ident["workspace_id"],
            "roleId": ident["role_id"],
        }
        if categories:
            payload["categories"] = categories

        result = await forward("catalog", payload)

        # Filter the catalog so the worker only sees tools in its scope.
        if result.get("ok") and "data" in result:
            data = result["data"]
            if isinstance(data, list):
                result["data"] = filter_catalog(data, scope)
            elif isinstance(data, dict):
                for key, entries in data.items():
                    if isinstance(entries, list):
                        data[key] = filter_catalog(entries, scope)
        return result

    # -- learn_tools -----------------------------------------------------

    async def _learn_tools(tool_names: list[str]) -> dict:
        """Fetch the full JSON input schema for specific tools before calling them.

        Always learn a tool's schema before execute_tool so arguments are shaped
        correctly. Example: learn_tools(["find_people", "create_person"]).
        """
        blocked = [name for name in tool_names if not is_tool_allowed(name, scope)]
        if blocked:
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": (
                        f"Tools not available in '{scope.name}' scope: "
                        + ", ".join(blocked)
                    ),
                },
            }

        ident = _identity(scope)
        return await forward(
            "learn",
            {
                "toolNames": tool_names,
                "workspaceId": ident["workspace_id"],
                "roleId": ident["role_id"],
            },
        )

    # -- execute_tool ----------------------------------------------------

    async def _execute_tool(
        tool: str,
        tool_args: dict | None = None,
        confirmation_token: str | None = None,
    ) -> dict:
        """Execute any Twenty CRM tool by name with the given arguments.

        Find tool names with get_tool_catalog and learn their schemas with
        learn_tools first. ``tool_args`` is the tool's argument object.
        Example: execute_tool(tool="find_people", tool_args={"limit": 5}).

        For high-risk actions that return ``CONFIRMATION_REQUIRED``, pass back
        the ``confirmation_token`` the system provided to confirm execution.
        """
        # ── Scope guard ────────────────────────────────────────────────
        if not is_tool_allowed(tool, scope):
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": (
                        f"Tool '{tool}' is not available in '{scope.name}' scope"
                    ),
                },
            }

        tool_args = tool_args or {}

        # ── Write-policy middleware (invisible to the LLM) ─────────────
        if write_policy and is_write_tool(tool):
            decision = await write_policy.gate(
                action=tool,
                tool_args=tool_args,
                confirmation_token=confirmation_token,
            )

            if not decision.allowed:
                # Build the error envelope the LLM will see.
                error: dict[str, Any] = {
                    "code": "CONFIRMATION_REQUIRED" if decision.confirmation_token else "WRITE_BLOCKED",
                    "message": decision.reason or "Write blocked by policy.",
                }
                if decision.confirmation_token:
                    error["confirmation_token"] = decision.confirmation_token
                    error["draft"] = {"tool": tool, "args": tool_args}

                return {"ok": False, "error": error}

        # ── Forward to the bridge ──────────────────────────────────────
        ident = _identity(scope)
        return await forward(
            "execute",
            {
                "tool": tool,
                "args": tool_args,
                "workspaceId": ident["workspace_id"],
                "roleId": ident["role_id"],
                "userId": ident["user_id"],
            },
        )

    # -- get_current_user ------------------------------------------------

    async def _get_current_user() -> dict:
        """Return the current workspace member (the "me" the agent acts as)."""
        ident = _identity(scope)
        return await forward(
            "current-user",
            {"workspaceId": ident["workspace_id"], "userId": ident["user_id"]},
        )

    # -- Assemble --------------------------------------------------------

    return [
        StructuredTool.from_function(
            coroutine=_get_tool_catalog, name="get_tool_catalog"
        ),
        StructuredTool.from_function(coroutine=_learn_tools, name="learn_tools"),
        StructuredTool.from_function(coroutine=_execute_tool, name="execute_tool"),
        StructuredTool.from_function(
            coroutine=_get_current_user, name="get_current_user"
        ),
    ]


# ---------------------------------------------------------------------------
# Backward-compatible default (uses WRITER_SCOPE = write + meta)
# ---------------------------------------------------------------------------

def get_crm_tools() -> list[StructuredTool]:
    """Build the LangChain tools the agent binds to its model.

    Uses the WRITER_SCOPE for backward compatibility with existing code
    that calls ``get_crm_tools()`` without a scope argument.
    """
    return build_crm_tools(WRITER_SCOPE)
