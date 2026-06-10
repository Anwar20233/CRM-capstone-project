"""Live-bridge runtime for the optimization harness.

Runs the Writer agent (with PII masking left **on**, exactly like production)
against the **live Twenty bridge** for a single request, captures the full
trajectory + progress events, and then **tears down** any records the rollout
created so repeated optimization runs don't pile up duplicates in the workspace.

Why a thin wrapper instead of ``WriterWorker``: ``WriterWorker`` hard-codes its
system prompt. The optimizer needs to swap a *candidate* prompt in, so we build
the same worker out of its parts (``WRITER_SCOPE`` + ``WritePolicy`` +
``resolve_date``) via ``BaseWorker``, parameterised by ``system_prompt``. This is
the single seam the whole optimization turns on.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.stubs.safety_tools import build_utility_tools
from agent.tool_scope import WRITER_SCOPE
from agent.workers.base_worker import BaseWorker
from agent.workers.write_policy import WritePolicy
from bridge_client import forward


# create_* action -> its delete_* counterpart (for teardown).
_CREATE_TO_DELETE: dict[str, str] = {
    "create_person": "delete_person",
    "create_company": "delete_company",
    "create_opportunity": "delete_opportunity",
    "create_note": "delete_note",
    "create_task": "delete_task",
    "create_many_people": "delete_person",  # delete each returned id
}


@dataclass
class RunResult:
    """Everything the metric needs to score one rollout."""

    request: str
    response: str
    # Ordered [{name, args, result}, ...] — note the writer's real action lives
    # at tool_calls[i]["args"]["tool"] when name == "execute_tool".
    tool_calls: list[dict[str, Any]]
    events: list[dict[str, Any]] = field(default_factory=list)
    prompt_masked: bool = False
    created_records: list[dict[str, str]] = field(default_factory=list)
    torn_down: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Identity (system-level, for teardown — bypasses the LLM + WritePolicy)
# ---------------------------------------------------------------------------

def _writer_identity() -> dict[str, str]:
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    user_id = os.environ.get("TWENTY_USER_ID")
    role_id = os.environ.get("TWENTY_WRITER_ROLE_ID") or os.environ.get("TWENTY_ROLE_ID")
    return {"workspaceId": workspace_id, "roleId": role_id, "userId": user_id}


def _extract_record_ids(result: dict[str, Any]) -> list[str]:
    """Best-effort pull of created-record id(s) from an execute_tool envelope."""
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    data = result.get("data")
    ids: list[str] = []

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 3 or len(ids) > 50:
            return
        if isinstance(node, dict):
            value = node.get("id")
            if isinstance(value, str) and value:
                ids.append(value)
            for child in node.values():
                _walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                _walk(child, depth + 1)

    _walk(data)
    # De-dup, keep order.
    return list(dict.fromkeys(ids))


def _collect_created_records(tool_calls: list[dict[str, Any]]) -> list[dict[str, str]]:
    created: list[dict[str, str]] = []
    for call in tool_calls:
        if call.get("name") != "execute_tool":
            continue
        action = (call.get("args") or {}).get("tool", "")
        delete_action = _CREATE_TO_DELETE.get(action)
        if not delete_action:
            continue
        for record_id in _extract_record_ids(call.get("result") or {}):
            created.append({"delete_action": delete_action, "id": record_id})
    return created


async def _teardown(created: list[dict[str, str]]) -> int:
    """Delete created records directly via the bridge (no policy, no LLM)."""
    if not created:
        return 0
    identity = _writer_identity()
    deleted = 0
    for record in created:
        envelope = await forward("execute", {
            "tool": record["delete_action"],
            "args": {"id": record["id"]},
            **identity,
        })
        if isinstance(envelope, dict) and envelope.get("ok"):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Public: run one request live
# ---------------------------------------------------------------------------

def _build_worker(system_prompt: str, session_id: str, model: str | None) -> BaseWorker:
    """Reconstruct the Writer worker with a candidate prompt, masking ON."""
    return BaseWorker(
        scope=WRITER_SCOPE,
        system_prompt=system_prompt,
        session_id=session_id,
        write_policy=WritePolicy(session_id=session_id),
        extra_tools=build_utility_tools(),
        model=model,
        # masking left at its default (on) — mimic a real interaction.
    )


async def _run_async(
    system_prompt: str,
    request: str,
    *,
    model: str | None,
    teardown: bool,
) -> RunResult:
    session_id = f"opt-{uuid.uuid4().hex[:12]}"
    events: list[dict[str, Any]] = []
    worker = _build_worker(system_prompt, session_id, model)

    try:
        outcome = await worker.run(request, on_event=events.append)
    except Exception as error:  # noqa: BLE001 — surface as a scored failure, don't crash the sweep
        return RunResult(request=request, response="", tool_calls=[], events=events,
                         error=f"{type(error).__name__}: {error}")

    tool_calls = outcome.get("tool_calls", [])
    created = _collect_created_records(tool_calls)
    torn_down = await _teardown(created) if teardown else 0

    return RunResult(
        request=request,
        response=outcome.get("response", ""),
        tool_calls=tool_calls,
        events=events,
        prompt_masked=any(event.get("type") == "prompt_masked" for event in events),
        created_records=created,
        torn_down=torn_down,
    )


def run_request(
    system_prompt: str,
    request: str,
    *,
    model: str | None = None,
    teardown: bool = True,
) -> RunResult:
    """Run one request against the live bridge and return a ``RunResult``.

    Synchronous (DSPy optimizers call the program from worker threads). Each call
    spins its own event loop via ``asyncio.run``.
    """
    return asyncio.run(_run_async(system_prompt, request, model=model, teardown=teardown))
