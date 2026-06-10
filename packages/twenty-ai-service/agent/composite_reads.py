"""Composite read tools — fan-out aggregators for the CRM agent.

Each tool fans out several bridge calls in parallel (``asyncio.gather``) and
merges the results into one structured response.  A single ``get_company_overview``
call replaces the reader having to discover and call four separate tools
sequentially — the LLM spends one turn getting a rich context object instead of
four.

Tool list
~~~~~~~~~
``get_company_overview``    company + team + open opportunities + recent notes + open tasks
``get_entity_timeline``     notes + tasks merged and sorted chronologically for any entity
``get_related_entities``    every entity linked to a given record across all object types
``search_all_records``      parallel full-text search across people, companies, and opportunities
``get_pipeline_stages``     metadata read of the opportunity ``stage`` field options

All tools use the bridge ``{ ok, data } / { ok, error }`` envelope so failures
are surfaced consistently.  They are classified as READ by the prefix rules
(``get_*`` / ``search_*``) and are automatically in-scope for the ReaderWorker.

Usage::

    from agent.tools.composite_reads import build_composite_read_tools
    from agent.tool_scope import READER_SCOPE

    extra = build_composite_read_tools(READER_SCOPE)
    reader = ReaderWorker(extra_tools=extra)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.tools import StructuredTool

from bridge_client import forward
from agent.tool_scope import ToolScope


# ---------------------------------------------------------------------------
# Identity helper (mirrors crm_tools._identity — keeps scope encapsulated)
# ---------------------------------------------------------------------------

def _identity(scope: ToolScope) -> dict[str, str]:
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    user_id = os.environ.get("TWENTY_USER_ID")
    try:
        role_id = scope.role_id
    except RuntimeError:
        role_id = None

    missing = [
        name
        for name, value in {
            "TWENTY_WORKSPACE_ID": workspace_id,
            scope.role_env_var: role_id,
            "TWENTY_USER_ID": user_id,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing required env vars: " + ", ".join(missing))
    return {"workspace_id": workspace_id, "role_id": role_id, "user_id": user_id}


def _exec(tool: str, args: dict, ident: dict) -> dict:
    """Shorthand bridge execute payload dict (NOT async — pass to gather)."""
    return {
        "tool": tool,
        "args": args,
        "workspaceId": ident["workspace_id"],
        "roleId": ident["role_id"],
        "userId": ident["user_id"],
    }


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _extract(result: dict, label: str) -> tuple[bool, Any]:
    """Pull data from a bridge result; returns (ok, data_or_error_str)."""
    if not result.get("ok"):
        return False, result.get("error", {}).get("message", f"{label} failed")
    return True, result.get("data")


# ---------------------------------------------------------------------------
# get_company_overview
# ---------------------------------------------------------------------------

async def _get_company_overview(company_id: str) -> dict:
    """Return a 360° snapshot of a company: profile, team, pipeline, and activity.

    Fans out five parallel bridge calls and merges into one structured object:
    ``company``, ``people`` (linked contacts), ``opportunities`` (open deals),
    ``recent_notes`` (last 10), ``open_tasks``.  Pass the Twenty company UUID.
    """
    ident = _identity(_get_company_overview._scope)  # type: ignore[attr-defined]

    company_call, people_call, opps_call, notes_call, tasks_call = await asyncio.gather(
        forward("execute", _exec("find_one_company", {"id": company_id}, ident)),
        forward("execute", _exec("find_people", {"filter": {"company": {"id": {"eq": company_id}}}, "orderBy": {"name": {"firstName": "AscNullsLast"}}}, ident)),
        forward("execute", _exec("find_opportunities", {"filter": {"company": {"id": {"eq": company_id}}, "stage": {"neq": "CLOSED_LOST"}}, "orderBy": {"updatedAt": "DescNullsLast"}}, ident)),
        forward("execute", _exec("find_notes", {"filter": {"noteTargets": {"some": {"companyId": {"eq": company_id}}}}, "orderBy": {"updatedAt": "DescNullsLast"}, "first": 10}, ident)),
        forward("execute", _exec("find_tasks", {"filter": {"taskTargets": {"some": {"companyId": {"eq": company_id}}}, "status": {"neq": "DONE"}}, "orderBy": {"dueAt": "AscNullsLast"}}, ident)),
    )

    ok_company, company = _extract(company_call, "company")
    if not ok_company:
        return _err("COMPANY_NOT_FOUND", f"Could not fetch company {company_id}: {company}")

    _, people = _extract(people_call, "people")
    _, opps = _extract(opps_call, "opportunities")
    _, notes = _extract(notes_call, "notes")
    _, tasks = _extract(tasks_call, "tasks")

    people_list = (people or {}).get("edges", []) if isinstance(people, dict) else []
    opps_list = (opps or {}).get("edges", []) if isinstance(opps, dict) else []
    notes_list = (notes or {}).get("edges", []) if isinstance(notes, dict) else []
    tasks_list = (tasks or {}).get("edges", []) if isinstance(tasks, dict) else []

    total_pipeline = sum(
        (e.get("node", {}).get("amount", {}) or {}).get("amountMicros", 0) or 0
        for e in opps_list
    )

    return _ok({
        "company": company,
        "team": [e.get("node") for e in people_list],
        "open_opportunities": [e.get("node") for e in opps_list],
        "recent_notes": [e.get("node") for e in notes_list],
        "open_tasks": [e.get("node") for e in tasks_list],
        "summary": {
            "team_size": len(people_list),
            "open_deal_count": len(opps_list),
            "total_pipeline_micros": total_pipeline,
            "open_task_count": len(tasks_list),
        },
    })


# ---------------------------------------------------------------------------
# get_entity_timeline
# ---------------------------------------------------------------------------

async def _get_entity_timeline(
    entity_id: str,
    entity_type: str,
    limit: int = 20,
) -> dict:
    """Return a unified chronological timeline of notes and tasks for any entity.

    ``entity_type`` is one of ``person``, ``company``, ``opportunity``.
    Fetches notes and tasks in parallel, merges them by ``updatedAt``, and
    returns a single sorted list of ``{ type, item }`` objects newest-first.
    ``limit`` caps the merged result (default 20).
    """
    ident = _identity(_get_entity_timeline._scope)  # type: ignore[attr-defined]

    entity_type_lower = entity_type.lower().rstrip("s")  # person / company / opportunity

    # Build target filters for both notes and tasks.
    note_filter: dict[str, Any]
    task_filter: dict[str, Any]

    if entity_type_lower == "person":
        note_filter = {"noteTargets": {"some": {"personId": {"eq": entity_id}}}}
        task_filter = {"taskTargets": {"some": {"personId": {"eq": entity_id}}}}
    elif entity_type_lower == "company":
        note_filter = {"noteTargets": {"some": {"companyId": {"eq": entity_id}}}}
        task_filter = {"taskTargets": {"some": {"companyId": {"eq": entity_id}}}}
    elif entity_type_lower in ("opportunity", "deal"):
        note_filter = {"noteTargets": {"some": {"opportunityId": {"eq": entity_id}}}}
        task_filter = {"taskTargets": {"some": {"opportunityId": {"eq": entity_id}}}}
    else:
        return _err("UNKNOWN_ENTITY_TYPE", f"entity_type must be person, company, or opportunity — got '{entity_type}'")

    notes_call, tasks_call = await asyncio.gather(
        forward("execute", _exec("find_notes", {"filter": note_filter, "orderBy": {"updatedAt": "DescNullsLast"}, "first": limit}, ident)),
        forward("execute", _exec("find_tasks", {"filter": task_filter, "orderBy": {"updatedAt": "DescNullsLast"}, "first": limit}, ident)),
    )

    _, notes_data = _extract(notes_call, "notes")
    _, tasks_data = _extract(tasks_call, "tasks")

    notes_edges = (notes_data or {}).get("edges", []) if isinstance(notes_data, dict) else []
    tasks_edges = (tasks_data or {}).get("edges", []) if isinstance(tasks_data, dict) else []

    events: list[dict] = [
        {"type": "note", "updatedAt": e["node"].get("updatedAt", ""), "item": e["node"]}
        for e in notes_edges if "node" in e
    ] + [
        {"type": "task", "updatedAt": e["node"].get("updatedAt", ""), "item": e["node"]}
        for e in tasks_edges if "node" in e
    ]

    events.sort(key=lambda x: x.get("updatedAt") or "", reverse=True)

    return _ok({
        "entity_id": entity_id,
        "entity_type": entity_type_lower,
        "timeline": events[:limit],
        "total_events": len(events),
    })


# ---------------------------------------------------------------------------
# get_related_entities
# ---------------------------------------------------------------------------

async def _get_related_entities(entity_id: str, entity_type: str) -> dict:
    """Return every entity related to a given record across all CRM object types.

    Fans out parallel lookups for the associations that make sense given the
    entity type:

    - **person** → company, opportunities, notes, tasks
    - **company** → people, opportunities, notes, tasks
    - **opportunity** → company, people (assigned to + contacts), notes, tasks

    Returns a dict keyed by relation name.
    """
    ident = _identity(_get_related_entities._scope)  # type: ignore[attr-defined]
    entity_type_lower = entity_type.lower().rstrip("s")

    if entity_type_lower == "person":
        person_call, opps_call, notes_call, tasks_call = await asyncio.gather(
            forward("execute", _exec("find_one_person", {"id": entity_id}, ident)),
            forward("execute", _exec("find_opportunities", {"filter": {"pointOfContactId": {"eq": entity_id}}}, ident)),
            forward("execute", _exec("find_notes", {"filter": {"noteTargets": {"some": {"personId": {"eq": entity_id}}}}}, ident)),
            forward("execute", _exec("find_tasks", {"filter": {"taskTargets": {"some": {"personId": {"eq": entity_id}}}}}, ident)),
        )
        _, person = _extract(person_call, "person")
        company_id = (person or {}).get("company", {}) and (person or {}).get("company", {}).get("id")
        company_call = None
        if company_id:
            company_call = await forward("execute", _exec("find_one_company", {"id": company_id}, ident))

        _, opps = _extract(opps_call, "opportunities")
        _, notes = _extract(notes_call, "notes")
        _, tasks = _extract(tasks_call, "tasks")
        _, company = _extract(company_call, "company") if company_call else (True, None)

        return _ok({
            "entity_id": entity_id,
            "entity_type": "person",
            "profile": person,
            "company": company,
            "opportunities": [(e.get("node")) for e in (opps or {}).get("edges", [])],
            "notes": [(e.get("node")) for e in (notes or {}).get("edges", [])],
            "tasks": [(e.get("node")) for e in (tasks or {}).get("edges", [])],
        })

    elif entity_type_lower == "company":
        company_call, people_call, opps_call, notes_call, tasks_call = await asyncio.gather(
            forward("execute", _exec("find_one_company", {"id": entity_id}, ident)),
            forward("execute", _exec("find_people", {"filter": {"company": {"id": {"eq": entity_id}}}}, ident)),
            forward("execute", _exec("find_opportunities", {"filter": {"company": {"id": {"eq": entity_id}}}}, ident)),
            forward("execute", _exec("find_notes", {"filter": {"noteTargets": {"some": {"companyId": {"eq": entity_id}}}}}, ident)),
            forward("execute", _exec("find_tasks", {"filter": {"taskTargets": {"some": {"companyId": {"eq": entity_id}}}}}, ident)),
        )
        _, company = _extract(company_call, "company")
        _, people = _extract(people_call, "people")
        _, opps = _extract(opps_call, "opportunities")
        _, notes = _extract(notes_call, "notes")
        _, tasks = _extract(tasks_call, "tasks")
        return _ok({
            "entity_id": entity_id,
            "entity_type": "company",
            "profile": company,
            "people": [(e.get("node")) for e in (people or {}).get("edges", [])],
            "opportunities": [(e.get("node")) for e in (opps or {}).get("edges", [])],
            "notes": [(e.get("node")) for e in (notes or {}).get("edges", [])],
            "tasks": [(e.get("node")) for e in (tasks or {}).get("edges", [])],
        })

    elif entity_type_lower in ("opportunity", "deal"):
        opp_call, notes_call, tasks_call = await asyncio.gather(
            forward("execute", _exec("find_one_opportunity", {"id": entity_id}, ident)),
            forward("execute", _exec("find_notes", {"filter": {"noteTargets": {"some": {"opportunityId": {"eq": entity_id}}}}}, ident)),
            forward("execute", _exec("find_tasks", {"filter": {"taskTargets": {"some": {"opportunityId": {"eq": entity_id}}}}}, ident)),
        )
        _, opp = _extract(opp_call, "opportunity")
        _, notes = _extract(notes_call, "notes")
        _, tasks = _extract(tasks_call, "tasks")
        company_id = (opp or {}).get("company", {}) and (opp or {}).get("company", {}).get("id")
        company_call = await forward("execute", _exec("find_one_company", {"id": company_id}, ident)) if company_id else None
        _, company = _extract(company_call, "company") if company_call else (True, None)
        return _ok({
            "entity_id": entity_id,
            "entity_type": "opportunity",
            "profile": opp,
            "company": company,
            "notes": [(e.get("node")) for e in (notes or {}).get("edges", [])],
            "tasks": [(e.get("node")) for e in (tasks or {}).get("edges", [])],
        })

    return _err("UNKNOWN_ENTITY_TYPE", f"entity_type must be person, company, or opportunity — got '{entity_type}'")


# ---------------------------------------------------------------------------
# search_all_records
# ---------------------------------------------------------------------------

async def _search_all_records(query: str, limit: int = 5) -> dict:
    """Full-text search across people, companies, and opportunities simultaneously.

    Runs three searches in parallel and returns ranked results grouped by type.
    ``limit`` applies per object type (default 5 each → up to 15 results total).
    Use this as the first step when the user says a name but you don't know
    whether they mean a person, company, or deal.
    """
    ident = _identity(_search_all_records._scope)  # type: ignore[attr-defined]

    people_call, companies_call, opps_call = await asyncio.gather(
        forward("execute", _exec("find_people", {"filter": {"or": [{"name": {"firstName": {"like": f"%{query}%"}}}, {"name": {"lastName": {"like": f"%{query}%"}}}, {"emails": {"primaryEmail": {"like": f"%{query}%"}}}]}, "first": limit}, ident)),
        forward("execute", _exec("find_companies", {"filter": {"name": {"like": f"%{query}%"}}, "first": limit}, ident)),
        forward("execute", _exec("find_opportunities", {"filter": {"name": {"like": f"%{query}%"}}, "first": limit}, ident)),
    )

    _, people = _extract(people_call, "people")
    _, companies = _extract(companies_call, "companies")
    _, opps = _extract(opps_call, "opportunities")

    people_list = [(e.get("node")) for e in (people or {}).get("edges", [])]
    company_list = [(e.get("node")) for e in (companies or {}).get("edges", [])]
    opps_list = [(e.get("node")) for e in (opps or {}).get("edges", [])]

    total = len(people_list) + len(company_list) + len(opps_list)

    return _ok({
        "query": query,
        "total_results": total,
        "people": people_list,
        "companies": company_list,
        "opportunities": opps_list,
    })


# ---------------------------------------------------------------------------
# get_pipeline_stages
# ---------------------------------------------------------------------------

async def _get_pipeline_stages() -> dict:
    """Return the available pipeline stages for opportunities.

    Reads the ``stage`` field metadata from Twenty's metadata API.  Use this
    before checking stage validity or building stage-advance workflows.
    Returns a list of ``{ value, label, position }`` objects.
    """
    ident = _identity(_get_pipeline_stages._scope)  # type: ignore[attr-defined]

    result = await forward(
        "execute",
        _exec(
            "get_field_metadata",
            {"objectNameSingular": "opportunity", "fieldName": "stage"},
            ident,
        ),
    )

    ok, data = _extract(result, "pipeline_stages")
    if not ok:
        # Fallback to a known-good static list so the writer can still proceed.
        return _ok({
            "source": "fallback_static",
            "stages": [
                {"value": "NEW", "label": "New", "position": 0},
                {"value": "SCREENING", "label": "Screening", "position": 1},
                {"value": "MEETING", "label": "Meeting", "position": 2},
                {"value": "PROPOSAL", "label": "Proposal", "position": 3},
                {"value": "CUSTOMER", "label": "Customer", "position": 4},
                {"value": "CLOSED_WON", "label": "Closed Won", "position": 5},
                {"value": "CLOSED_LOST", "label": "Closed Lost", "position": 6},
            ],
        })

    options = data.get("options", []) if isinstance(data, dict) else []
    return _ok({"source": "metadata", "stages": options})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_composite_read_tools(scope: ToolScope) -> list[StructuredTool]:
    """Return all composite read tools closed over *scope* for identity.

    These are READ-classified tools suitable for the ReaderWorker's
    ``extra_tools``.  Each tool calls ``get_pipeline_stages._scope`` etc. at
    runtime — we inject the scope by setting a function attribute before
    wrapping in a StructuredTool.
    """
    # Inject scope via function attribute (avoids a class; mirrors the pattern in
    # build_crm_tools where closures capture scope directly).
    _get_company_overview._scope = scope  # type: ignore[attr-defined]
    _get_entity_timeline._scope = scope  # type: ignore[attr-defined]
    _get_related_entities._scope = scope  # type: ignore[attr-defined]
    _search_all_records._scope = scope  # type: ignore[attr-defined]
    _get_pipeline_stages._scope = scope  # type: ignore[attr-defined]

    return [
        StructuredTool.from_function(
            coroutine=_get_company_overview,
            name="get_company_overview",
            description=(
                "360° snapshot of a company: profile, team, open opportunities, "
                "recent notes, and open tasks — all in one call. "
                "Pass the Twenty company UUID as company_id."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_entity_timeline,
            name="get_entity_timeline",
            description=(
                "Unified chronological timeline (notes + tasks) for any CRM entity. "
                "entity_type: 'person', 'company', or 'opportunity'. "
                "Results are merged and sorted newest-first. limit defaults to 20."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_related_entities,
            name="get_related_entities",
            description=(
                "All entities related to a given record across every CRM object type. "
                "entity_type: 'person', 'company', or 'opportunity'. "
                "Returns a dict keyed by relation name."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_search_all_records,
            name="search_all_records",
            description=(
                "Parallel full-text search across people, companies, and opportunities "
                "in a single call. Use when you have a name but don't know the entity type. "
                "limit caps results per type (default 5)."
            ),
        ),
        StructuredTool.from_function(
            coroutine=_get_pipeline_stages,
            name="get_pipeline_stages",
            description=(
                "Return the available pipeline stages for opportunities "
                "(value, label, position). Call this before advancing a deal stage "
                "to validate the target stage."
            ),
        ),
    ]
