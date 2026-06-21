"""Deterministic note/task → target linking guard for the writer graph.

Twenty links a note/task to a person/company/opportunity through a SEPARATE join
row (``noteTarget`` / ``taskTarget``), never a field on the create call. The
writer is *told* to create those rows after a create, but that is soft, LLM-driven
behaviour: the model frequently creates the note/task and then forgets the second
``create_*_target`` call — leaving the record floating with no relation, so it
never shows up under the person/company/opportunity in the UI.

This module closes that gap deterministically. After the writer finishes a turn,
:func:`reconcile_targets` looks at what was actually written and guarantees the
links exist — no LLM involved:

* **Intended targets** are the entities the orchestrator *named in this specific
  instruction* (matched by their handle, e.g. ``person001``, in the first
  HumanMessage). This is deliberately narrow: we link only what the instruction
  referenced, never the whole accumulated handle map, so we don't over-link
  entities resolved on earlier turns.
* **Created records** are the notes/tasks the writer actually created this turn
  (read from the ``execute_tool`` results).
* For every (created record × intended target) pair that does **not** already have
  a join row (the writer may have created some itself), we create it via one
  direct bridge call — one row per target, the shape Twenty expects.

The shared ``EntityHandleMap`` carries the real CRM ids (``handle.record_id``), so
this needs no extra reads. On the follow-up path the writer's handle map holds
only content handles (no resolved CRM entities), so ``_intended_targets`` is empty
and this is a no-op there — that path links explicitly via its instruction.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Type keyword (as it appears in an orchestrator instruction) → target FK field.
# Used to classify a UUID by the nearest preceding keyword, so "link note <id> to
# person <id>" attributes the second id to a person and ignores the note id.
_TYPE_KEYWORD_FIELD: tuple[tuple[str, str], ...] = (
    ("opportunit", "targetOpportunityId"),
    ("deal", "targetOpportunityId"),
    ("compan", "targetCompanyId"),
    ("account", "targetCompanyId"),
    ("organi", "targetCompanyId"),
    ("person", "targetPersonId"),
    ("people", "targetPersonId"),
    ("contact", "targetPersonId"),
)


def _typed_ids_in_instruction(instruction: str) -> list[tuple[str, str]]:
    """Parse ``"<type> … <uuid>"`` references → ``[(target_field, record_id)]``.

    The orchestrator follows id-first discipline and writes targets explicitly as
    "link the note to person <uuid>" / "to the opportunity <uuid>". An entity the
    READER surfaced (not resolved from the user's message) has no handle in the
    pii_map, so this is the only signal the guard has for it. Each UUID is
    classified by the NEAREST preceding type keyword within a short window; a UUID
    with no type keyword in front of it (e.g. the note/task id itself) is ignored.
    """
    if not instruction:
        return []
    targets: list[tuple[str, str]] = []
    for match in re.finditer(_UUID_RE, instruction):
        prefix = instruction[max(0, match.start() - 40): match.start()].lower()
        best: tuple[int, str] | None = None
        for keyword, field in _TYPE_KEYWORD_FIELD:
            idx = prefix.rfind(keyword)
            if idx >= 0 and (best is None or idx > best[0]):
                best = (idx, field)
        if best is not None:
            targets.append((best[1], match.group(0)))
    return targets


# Resolved-handle entity type → the flat foreign-key field the create_*_target
# tool expects (MANY_TO_ONE relations take ``<relation>Id`` in the create input).
_TARGET_ID_FIELD: dict[str, str] = {
    "person": "targetPersonId",
    "company": "targetCompanyId",
    "opportunity": "targetOpportunityId",
}

# Created record "kind" → (target tool, parent foreign-key field).
_LINK_SPEC: dict[str, tuple[str, str]] = {
    "note": ("create_note_target", "noteId"),
    "task": ("create_task_target", "taskId"),
}

# The CRM create tools whose output we link.
_CREATE_TOOL_TO_KIND: dict[str, str] = {
    "create_note": "note",
    "create_task": "task",
}

_TARGET_TOOLS = frozenset({"create_note_target", "create_task_target"})


def _decode(content: Any) -> Any:
    """ToolMessage content → dict when it carries a JSON envelope, else as-is."""
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


def _first_instruction(messages: list[Any]) -> str:
    """The text of the first HumanMessage — the instruction the writer was given."""
    from langchain_core.messages import HumanMessage

    for msg in messages:
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def _intended_targets(instruction: str, pii_map: Any) -> list[tuple[str, str]]:
    """Resolved entities the instruction names → ``[(target_field, record_id)]``.

    Narrow on purpose: only handles whose name appears in *this* instruction, so
    we link exactly what the orchestrator referenced — not every entity resolved
    earlier in the session.
    """
    if not instruction:
        return []
    instruction_lower = instruction.lower()
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for handle in getattr(pii_map, "handles", []) if pii_map is not None else []:
        if not getattr(handle, "is_resolved", False):
            continue
        field = _TARGET_ID_FIELD.get(handle.entity_type)
        if field is None:
            continue
        # Match the entity the instruction references, in ANY form the orchestrator
        # might use — it is nondeterministic about which it picks:
        #   • the masking token (``person001``),
        #   • the real UUID (``…person 8897…`` — id-first discipline),
        #   • the human display name (``Gabriel Robinson``) or a surface alias.
        # Matching the token alone (the old behaviour) silently linked nothing when
        # the orchestrator passed the id or the plain name.
        record_id = str(handle.record_id)
        canonical = (getattr(handle, "canonical", "") or "").lower()
        surfaces = getattr(handle, "surfaces", None) or ()
        matched = (
            handle.name in instruction
            or record_id in instruction
            or (canonical and canonical in instruction_lower)
            or any(surface and surface in instruction_lower for surface in surfaces)
        )
        if not matched:
            continue
        if record_id in seen:
            continue
        seen.add(record_id)
        targets.append((field, record_id))
    # Also honor typed ids the orchestrator wrote straight into the instruction
    # ("…to person <uuid>"). This is the only signal for an entity the READER
    # surfaced, which never enters the pii_map. Dedup against handle matches.
    for field, record_id in _typed_ids_in_instruction(instruction):
        if record_id in seen:
            continue
        seen.add(record_id)
        targets.append((field, record_id))
    return targets


def _paired_calls(messages: list[Any]) -> list[tuple[str, dict, Any]]:
    """Pair each ToolMessage with its originating tool_call → ``(name, args, result)``."""
    from langchain_core.messages import AIMessage, ToolMessage

    call_meta: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                call_meta[tc["id"]] = {"name": tc.get("name"), "args": tc.get("args", {})}

    pairs: list[tuple[str, dict, Any]] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            meta = call_meta.get(msg.tool_call_id, {})
            pairs.append((meta.get("name"), meta.get("args", {}), _decode(msg.content)))
    return pairs


def _record_id_from_data(data: Any) -> str | None:
    """The created record's id, across the create envelope's possible shapes.

    The record-crud create service returns ``{success, message, result: <record>,
    recordReferences: [{recordId}]}`` — so the id lives at ``data.result.id``. We
    also accept a bare ``data.id`` and the ``recordReferences`` form defensively,
    so a wrapper change can't silently drop the link.
    """
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, dict) and result.get("id"):
        return str(result["id"])
    if data.get("id"):
        return str(data["id"])
    refs = data.get("recordReferences")
    if isinstance(refs, list) and refs and isinstance(refs[0], dict) and refs[0].get("recordId"):
        return str(refs[0]["recordId"])
    return None


def _created_records(pairs: list[tuple[str, dict, Any]]) -> list[tuple[str, str]]:
    """Successful note/task creations this turn → ``[(kind, record_id)]``."""
    created: list[tuple[str, str]] = []
    for name, args, result in pairs:
        if name != "execute_tool":
            continue
        kind = _CREATE_TOOL_TO_KIND.get((args or {}).get("tool", ""))
        if kind is None:
            continue
        if not (isinstance(result, dict) and result.get("ok")):
            continue
        record_id = _record_id_from_data(result.get("data"))
        if record_id:
            created.append((kind, record_id))
    return created


def _existing_links(pairs: list[tuple[str, dict, Any]], pii_map: Any) -> set[tuple[str, str]]:
    """Join rows the writer already created → ``{(parent_id, target_id)}``.

    Target ids in the writer's own calls may still be handle references
    (``person001.id``); unmask them so dedup compares real ids.
    """
    existing: set[tuple[str, str]] = set()
    for name, args, _result in pairs:
        if name != "execute_tool":
            continue
        if (args or {}).get("tool") not in _TARGET_TOOLS:
            continue
        tool_args = (args or {}).get("tool_args") or {}
        if pii_map is not None:
            tool_args = pii_map.unmask_value(tool_args)
        parent_id = tool_args.get("noteId") or tool_args.get("taskId")
        if not parent_id:
            continue
        for field in _TARGET_ID_FIELD.values():
            target_id = tool_args.get(field)
            if target_id:
                existing.add((str(parent_id), str(target_id)))
    return existing


async def reconcile_targets(messages: list[Any], pii_map: Any) -> list[dict[str, Any]]:
    """Ensure every note/task created this turn links to the named targets.

    Returns the list of join rows it created (for tracing/tests). A best-effort
    guard: a failed link is logged, never raised, so it can't break a turn whose
    write already succeeded.
    """
    instruction = _first_instruction(messages)
    targets = _intended_targets(instruction, pii_map)
    if not targets:
        return []

    pairs = _paired_calls(messages)
    created = _created_records(pairs)
    if not created:
        return []

    existing = _existing_links(pairs, pii_map)

    from agent.progress import emit_progress
    from agent.tool_scope import WRITER_SCOPE
    from agent.tools.composite_reads import _exec, _identity
    from bridge_client import forward

    try:
        ident = _identity(WRITER_SCOPE)
    except RuntimeError as error:
        logger.warning("auto_link skipped — %s", error)
        return []

    linked: list[dict[str, Any]] = []
    for kind, record_id in created:
        target_tool, parent_field = _LINK_SPEC[kind]
        for target_field, target_id in targets:
            if (record_id, target_id) in existing:
                continue
            link_args = {parent_field: record_id, target_field: target_id}
            emit_progress(
                {"type": "tool_call", "name": "execute_tool",
                 "args": {"tool": target_tool, "tool_args": link_args}}
            )
            try:
                result = await forward("execute", _exec(target_tool, link_args, ident))
            except Exception as error:  # noqa: BLE001
                logger.warning("auto_link %s failed: %s", target_tool, error)
                continue
            if isinstance(result, dict) and result.get("ok"):
                existing.add((record_id, target_id))
                linked.append({"tool": target_tool, "args": link_args})
            else:
                logger.warning("auto_link %s rejected: %s", target_tool, result)
    return linked


__all__ = ["reconcile_targets"]
