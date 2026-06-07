"""Twenty CRM tools for the internal LangGraph agent.

Twenty exposes 254 tools — too many to bind to an LLM directly (context blowup,
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

Usage in a LangGraph agent:

    from agent import get_crm_tools

    tools = get_crm_tools()
    model = model.bind_tools(tools)          # or pass tools= to create_react_agent
"""

import os

from langchain_core.tools import StructuredTool

from bridge_client import forward


def _identity() -> dict:
    """Resolve the calling identity from configuration.

    Deliberately out of the LLM-facing tool signatures: the model must never
    pass workspace/user UUIDs. Raises a clear error if misconfigured.
    """
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    role_id = os.environ.get("TWENTY_ROLE_ID")
    user_id = os.environ.get("TWENTY_USER_ID")

    missing = [
        name
        for name, value in {
            "TWENTY_WORKSPACE_ID": workspace_id,
            "TWENTY_ROLE_ID": role_id,
            "TWENTY_USER_ID": user_id,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return {"workspace_id": workspace_id, "role_id": role_id, "user_id": user_id}


async def _get_tool_catalog(categories: list[str] | None = None) -> dict:
    """List the CRM tools available, grouped by category.

    Categories: DATABASE_CRUD, ACTION, WORKFLOW, METADATA, VIEW, VIEW_FIELD,
    DASHBOARD, LOGIC_FUNCTION. Pass `categories` to filter. Returns lightweight
    entries (name + description); call learn_tools for full input schemas.
    """
    ident = _identity()
    payload: dict = {
        "workspaceId": ident["workspace_id"],
        "roleId": ident["role_id"],
    }

    if categories:
        payload["categories"] = categories

    return await forward("catalog", payload)


async def _learn_tools(tool_names: list[str]) -> dict:
    """Fetch the full JSON input schema for specific tools before calling them.

    Always learn a tool's schema before execute_tool so arguments are shaped
    correctly. Example: learn_tools(["find_people", "create_person"]).
    """
    ident = _identity()

    return await forward(
        "learn",
        {
            "toolNames": tool_names,
            "workspaceId": ident["workspace_id"],
            "roleId": ident["role_id"],
        },
    )


# Param is named tool_args (not args) to avoid colliding with BaseTool.args,
# which LangChain would otherwise mangle into "v__args" in the schema the LLM sees.
async def _execute_tool(tool: str, tool_args: dict | None = None) -> dict:
    """Execute any Twenty CRM tool by name with the given arguments.

    Find tool names with get_tool_catalog and learn their schemas with
    learn_tools first. `tool_args` is the tool's argument object.
    Example: execute_tool(tool="find_people", tool_args={"limit": 5}).
    """
    ident = _identity()

    return await forward(
        "execute",
        {
            "tool": tool,
            "args": tool_args or {},
            "workspaceId": ident["workspace_id"],
            "roleId": ident["role_id"],
            "userId": ident["user_id"],
        },
    )


async def _get_current_user() -> dict:
    """Return the current workspace member (the "me" the agent acts as)."""
    ident = _identity()

    return await forward(
        "current-user",
        {"workspaceId": ident["workspace_id"], "userId": ident["user_id"]},
    )


def get_crm_tools() -> list[StructuredTool]:
    """Build the LangChain tools the agent binds to its model.

    StructuredTool.from_function infers each tool's name, description, and args
    schema from the wrapped coroutine's signature and docstring.
    """
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
