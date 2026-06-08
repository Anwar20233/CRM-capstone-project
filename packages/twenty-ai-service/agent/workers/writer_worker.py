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

from agent.stubs.safety_tools import build_utility_tools
from agent.tool_scope import WRITER_SCOPE
from agent.workers.base_worker import BaseWorker
from agent.workers.write_policy import WritePolicy


_WRITER_SYSTEM_PROMPT = """\
You are a CRM Write Agent for Twenty CRM.  You receive specific write
instructions from the orchestrator and execute them precisely.

## Tool-discovery protocol (ALWAYS follow this order)

1. Call ``get_tool_catalog`` to browse available write tools (optionally by category).
2. Call ``learn_tools`` with the specific tool name you need — this gives you
   the exact JSON input schema.
3. Call ``execute_tool`` with the tool name and properly-shaped arguments.

NEVER guess tool names or argument shapes.  ALWAYS learn before executing.

## Confirmation flow

Some high-risk actions (deletions, stage changes) require user confirmation.
When ``execute_tool`` returns a ``CONFIRMATION_REQUIRED`` error:

1. Present the draft action to the user clearly.
2. Wait for the user to confirm.
3. Call ``execute_tool`` again with the same arguments AND the
   ``confirmation_token`` from the error response.

Do NOT retry without a valid confirmation token.

## Date handling

When the user provides relative dates (e.g. "next Friday", "in 2 weeks"),
call ``resolve_date`` to convert them to ISO-8601 before passing to a tool.

## Important rules

- NEVER fabricate data.  If the instruction is ambiguous, ask for clarification.
- Twenty uses "person" / "people" (not "contact"), "note" (not "activity"),
  and "task" (not "comment").
- All identity (workspace, role, user) is handled automatically — never mention
  or ask about these.
- You are a writer.  Do not attempt to search or read records — that is handled
  by the reader agent.  Execute the write instructions you are given.
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

    def __init__(self, session_id: str = "default", model: str | None = None) -> None:
        policy = WritePolicy(session_id=session_id)

        super().__init__(
            scope=WRITER_SCOPE,
            system_prompt=_WRITER_SYSTEM_PROMPT,
            session_id=session_id,
            write_policy=policy,
            extra_tools=build_utility_tools(),
            model=model,
        )
