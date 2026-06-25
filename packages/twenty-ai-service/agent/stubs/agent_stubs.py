"""Stub sub-agents — Follow-up and Researcher.

The orchestrator's spec splits agents into two classes:

- **True sub-agents** (Reader, Writer) — simple read/write on the CRM. These
  wrap real ``BaseWorker`` instances (see ``agent/agent_registry.py``).
- **Complex agents** (Follow-up, Researcher) — they own their own sub-agents and
  scheduled tasks, and are out of scope for now.

These stubs let the orchestrator *discover and delegate to* the complex agents
exactly as it would the real ones, while only recording that they were called.
Tests assert against ``CALL_LOG`` to verify the orchestrator activated the right
agent for a given query (requirement: "stubs that verify they were called").

Each stub returns the same ``{ ok, ... }`` shape sub-agents return, so swapping
in a real worker later is a drop-in replacement.
"""

from __future__ import annotations

STUB = True

# Records every stub delegation as {"agent": str, "instruction": str}.  Tests
# inspect this to confirm the orchestrator activated the expected agent.  Call
# ``reset_call_log()`` between tests.
CALL_LOG: list[dict[str, str]] = []


def reset_call_log() -> None:
    """Clear the recorded stub delegations (use in test setup/teardown)."""
    CALL_LOG.clear()


async def followup_stub(instruction: str) -> dict:
    """Stub Follow-up agent — records the call and echoes a not-implemented note."""
    CALL_LOG.append({"agent": "followup", "instruction": instruction})
    return {
        "status": "stub",
        "agent": "followup",
        "message": "Follow-up agent not yet implemented",
        "instruction": instruction,
    }


async def researcher_stub(instruction: str) -> dict:
    """Stub Researcher agent — records the call and echoes a not-implemented note."""
    CALL_LOG.append({"agent": "researcher", "instruction": instruction})
    return {
        "status": "stub",
        "agent": "researcher",
        "message": "Researcher agent not yet implemented",
        "instruction": instruction,
    }
