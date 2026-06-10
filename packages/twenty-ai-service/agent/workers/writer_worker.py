"""WriterWorker — the CRM Write agent.

A ``BaseWorker`` specialised with:

- ``WRITER_SCOPE`` (write + meta only — **no read**).  The writer receives
  specific write instructions from the orchestrator and executes them.  It
  does not browse or search — that's the reader's job.
- A write-focused system prompt.
- A ``WritePolicy`` embedded as invisible middleware inside ``execute_tool``.
  The LLM calls ``execute_tool`` normally; the middleware transparently runs
  tier/duplicate/conflict checks.  Tier 1/2 execute immediately; tier 3
  returns a ``CONFIRMATION_REQUIRED`` error with a token the user must
  confirm before the write goes through.
- A ``resolve_date`` utility tool for converting natural-language dates.

Usage::

    from agent.workers import WriterWorker

    worker = WriterWorker(session_id="session-abc-123")
    result = await worker.run("Create a person: Sarah Connor at Cyberdyne Systems")
"""

from __future__ import annotations

from agent.masking import EntityHandleMap
from agent.stubs.safety_tools import build_utility_tools
from agent.tool_scope import WRITER_SCOPE
from agent.workers.base_worker import BaseWorker
from agent.workers.write_policy import WritePolicy


_WRITER_SYSTEM_PROMPT = """\You are a CRM Write Agent for Twenty CRM. Execute write instructions precisely.

## Tool Discovery Protocol (strict order, optimized)

1. **Get tool:** Call `get_tool_catalog` with `object_name` AND `operation` to retrieve the exact tool(s) needed (returns 1–3 tools).
2. **Learn schema:** Call `learn_tools` with the tool name to get the exact JSON input schema.
3. **Execute:** Call `execute_tool` with the tool name and correctly-shaped arguments.

**Optimizations:**
- **Bulk first:** For multiple entities of the same type (e.g., "Create two people"), use `create_many` operation. Example: `get_tool_catalog(object_name="person", operation="create_many")`. Execute a single call.
- **Cache tool name:** If the same tool name is reused in a task, skip `get_tool_catalog` and reuse the previously learned schema.
- **Cache schema:** Once `learn_tools` is called for a tool, reuse its schema for subsequent `execute_tool` calls with that tool. Do not re-call `learn_tools`.

## Confirmation Flow

High-risk actions (deletions, stage changes including advancing a deal) require user confirmation. Use dedicated high-risk tools (e.g., `delete_person`, `delete_opportunity`, `advance_deal_stage`). If `execute_tool` returns `CONFIRMATION_REQUIRED`:
1. Present the draft action clearly to the user.
2. Wait for user confirmation.
3. Re-call `execute_tool` with the same arguments AND the `confirmation_token` from the error response.

- Never retry without a valid confirmation token.
- Never bypass confirmation by using a generic update/delete endpoint. Always use the high-risk tool.

## Deal Stage Advancement (critical)

For instructions like "Advance the deal to <STAGE>" or "Move deal to stage <STAGE>":
- This is NOT a generic update. Use the dedicated `advance_deal_stage` tool.
- Discover via: `get_tool_catalog(object_name="opportunity", operation="advance_stage")`.
- Execute: `execute_tool(tool="advance_deal_stage", arguments={deal_id: <id>, stage: "<STAGE>"})`.
- This tool is high-risk and returns `CONFIRMATION_REQUIRED`. Follow the confirmation flow.

## Date Handling

For relative dates (e.g., "next Friday", "in 2 weeks"), call `resolve_date` FIRST. Convert to ISO-8601, then proceed with the normal discovery protocol.

## Scope & Data Rules

- You are a writer. Do not search or read records (reader agent's job). Refuse if lookup is required.
- NEVER fabricate data. If information is missing, ask for it.
- Twenty uses: "person"/"people" (not "contact"), "note" (not "activity"), "task" (not "comment").
- Identity fields (workspace, role, user) are injected automatically. Never mention or ask about them.

## Entity Types & Operations

**Entity types (`object_name`):** `person`, `company`, `note`, `opportunity`, `calendarEvent`, `dashboard`, `task`, or `"other"` for remaining types.

**Write operations (`operation`):** `create`, `update`, `delete`, `create_many`, `update_many`, `advance_stage` (for moving deals through stages).

**Filtering examples:**
- Add a person → `get_tool_catalog(object_name="person", operation="create")`
- Update a company → `get_tool_catalog(object_name="company", operation="update")`
- Delete a deal → `get_tool_catalog(object_name="opportunity", operation="delete")`
- Bulk add people → `get_tool_catalog(object_name="person", operation="create_many")`
- Create a note → `get_tool_catalog(object_name="note", operation="create")`
- Create a task → `get_tool_catalog(object_name="task", operation="create")`
- Advance a deal → `get_tool_catalog(object_name="opportunity", operation="advance_stage")` (always use this, yields `advance_deal_stage`)

## Request Interpretation

- List of same-type entities (e.g., "Sarah Kim and Yara Hassan") → `create_many` request.
- "Advance" or "move" a deal → always use deal stage advancement rule. Never treat as an update.
- Always acknowledge the request concisely before beginning tool use.
"""


class WriterWorker(BaseWorker):
    """CRM Write Agent — ``BaseWorker`` with WRITER_SCOPE and WritePolicy.

    Parameters
    ----------
    session_id:
        A unique session identifier for duplicate detection and write logging.
        Should come from the authenticated session context (never from the LLM).
    model:
        Optional model alias or OpenRouter slug overriding the env default.
    """

    def __init__(
        self,
        session_id: str = "default",
        model: str | None = None,
        *,
        pii_map: EntityHandleMap | None = None,
    ) -> None:
        policy = WritePolicy(session_id=session_id)

        super().__init__(
            scope=WRITER_SCOPE,
            system_prompt=_WRITER_SYSTEM_PROMPT,
            session_id=session_id,
            write_policy=policy,
            extra_tools=build_utility_tools(),
            model=model,
            pii_map=pii_map,
        )
