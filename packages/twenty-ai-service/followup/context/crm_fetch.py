from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from bridge_client import forward
from followup.context.bridge_parse import (
    build_find_collection_args,
    extract_stage_options_from_bridge_result,
    extract_records_from_bridge_result,
    extract_single_record_from_bridge_result,
    format_bridge_result_for_debug,
)
from followup.context.completeness import build_bridge_fetch_completeness
from followup.context.errors import ContextLoadError
from followup.context.opportunity_scalar_timeline import (
    build_opportunity_scalar_timeline_events,
)

logger = logging.getLogger(__name__)


class RawOpportunityBundle(BaseModel):
    opportunity_id: str
    opportunity: dict[str, Any] | None = None
    company: dict[str, Any] | None = None
    point_of_contact: dict[str, Any] | None = None
    notes: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_stages: list[dict[str, Any]] = Field(default_factory=list)
    context_completeness: dict[str, Any] | None = None
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


def _exec(tool: str, args: dict[str, Any], identity: CrmIdentity) -> dict[str, Any]:
    return {
        "tool": tool,
        "args": args,
        "workspaceId": identity.workspace_id,
        "roleId": identity.role_id,
        "userId": identity.user_id,
    }


def _require_single_record(
    result: dict[str, Any],
    *,
    label: str,
    expected_id: str | None = None,
) -> dict[str, Any]:
    record, status = extract_single_record_from_bridge_result(result)
    if status == "bridge_error":
        error = result.get("error") or {}
        raise ContextLoadError(
            "BRIDGE_ERROR",
            error.get("message", f"{label} bridge request failed"),
        )
    if status == "tool_error":
        data = result.get("data") or {}
        raise ContextLoadError(
            "BRIDGE_TOOL_ERROR",
            str(data.get("message") or data.get("error") or f"{label} tool failed"),
        )
    if status in {"no_data", "unrecognized", "empty"} or record is None:
        logger.warning(
            "%s bridge request succeeded, but no record was found in the response.",
            label,
        )
        logger.debug(
            "%s bridge response: %s",
            label,
            format_bridge_result_for_debug(result),
        )
        raise ContextLoadError(
            "RECORD_NOT_FOUND",
            (
                f"Bridge request succeeded, but no {label} record was found "
                "in the response."
            ),
            detail={"response": sanitize_response_shape(result)},
        )
    if expected_id and str(record.get("id")) != expected_id:
        raise ContextLoadError(
            "RECORD_NOT_FOUND",
            f"{label} record id mismatch (expected {expected_id})",
        )
    return record


def _extract_optional_single_record(result: dict[str, Any]) -> dict[str, Any] | None:
    record, status = extract_single_record_from_bridge_result(result)
    if status == "ok" and record is not None:
        return record
    if status in {"bridge_error", "tool_error"}:
        data = result.get("data") or {}
        logger.warning(
            "Optional bridge fetch failed: %s",
            (result.get("error") or {}).get("message")
            or data.get("message")
            or data.get("error"),
        )
    return None


def _extract_collection_records(result: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    records, status = extract_records_from_bridge_result(result)
    if status == "bridge_error":
        error = result.get("error") or {}
        logger.warning(
            "%s bridge request failed: %s",
            label,
            error.get("message", "unknown error"),
        )
        return []
    if status == "tool_error":
        data = result.get("data") or {}
        logger.warning(
            "%s tool request failed: %s",
            label,
            data.get("message") or data.get("error"),
        )
        return []
    if status == "unrecognized":
        logger.warning(
            "%s bridge request succeeded, but the response shape was not recognized.",
            label,
        )
        logger.debug(
            "%s bridge response: %s",
            label,
            format_bridge_result_for_debug(result),
        )
        return []
    return records


def sanitize_response_shape(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        return {"top_level_keys": list(result.keys())}
    payload = data.get("result")
    shape: dict[str, Any] = {
        "data_keys": list(data.keys()),
        "success": data.get("success"),
    }
    if isinstance(payload, dict):
        shape["result_keys"] = list(payload.keys())
        if isinstance(payload.get("records"), list):
            shape["records_count"] = len(payload["records"])
    return shape


def _merge_timeline(
    notes: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    opportunity: dict[str, Any] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if opportunity is not None:
        events.extend(build_opportunity_scalar_timeline_events(opportunity))
    events.extend(
        [
            {
                "type": "note",
                "updatedAt": note.get("updatedAt", ""),
                "item": note,
            }
            for note in notes
        ]
        + [
            {
                "type": "task",
                "updatedAt": task.get("updatedAt", ""),
                "item": task,
            }
            for task in tasks
        ],
    )
    events.sort(key=lambda event: event.get("updatedAt") or "", reverse=True)
    return events[:limit]


async def fetch_opportunity_bundle(
    opportunity_id: str,
    identity: CrmIdentity,
) -> RawOpportunityBundle:
    try:
        (
            opportunity_result,
            notes_result,
            tasks_result,
            pipeline_result,
        ) = await asyncio.gather(
            forward(
                "execute",
                _exec("find_one_opportunity", {"id": opportunity_id}, identity),
            ),
            forward(
                "execute",
                _exec(
                    "find_notes",
                    build_find_collection_args(limit=20),
                    identity,
                ),
            ),
            forward(
                "execute",
                _exec(
                    "find_tasks",
                    build_find_collection_args(limit=20),
                    identity,
                ),
            ),
            forward(
                "execute",
                _exec(
                    "get_field_metadata",
                    {
                        "objectNameSingular": "opportunity",
                        "fieldName": "stage",
                    },
                    identity,
                ),
            ),
        )
    except Exception as error:
        raise ContextLoadError(
            "BRIDGE_UNREACHABLE",
            f"Could not reach agent-bridge: {error}",
        ) from error

    try:
        opportunity = _require_single_record(
            opportunity_result,
            label="opportunity",
            expected_id=opportunity_id,
        )
    except ContextLoadError as error:
        if error.code == "RECORD_NOT_FOUND":
            raise ContextLoadError(
                "OPPORTUNITY_NOT_FOUND",
                f"Opportunity {opportunity_id} not found",
                detail=error.detail,
            ) from error
        raise

    notes = _extract_collection_records(notes_result, label="notes")
    tasks = _extract_collection_records(tasks_result, label="tasks")
    timeline = _merge_timeline(notes, tasks, opportunity=opportunity)

    pipeline_stages = extract_stage_options_from_bridge_result(pipeline_result)
    pipeline_source = "crm_metadata" if pipeline_stages else None
    has_scalar_timeline = bool(
        build_opportunity_scalar_timeline_events(opportunity),
    )

    company: dict[str, Any] | None = None
    company_id = opportunity.get("companyId")
    if not company_id:
        company_ref = opportunity.get("company")
        if isinstance(company_ref, dict):
            company_id = company_ref.get("id")
    if company_id:
        company_result = await forward(
            "execute",
            _exec("find_one_company", {"id": company_id}, identity),
        )
        company = _extract_optional_single_record(company_result)

    point_of_contact: dict[str, Any] | None = None
    point_of_contact_id = opportunity.get("pointOfContactId")
    if not point_of_contact_id:
        point_of_contact_ref = opportunity.get("pointOfContact")
        if isinstance(point_of_contact_ref, dict):
            point_of_contact_id = point_of_contact_ref.get("id")
    if point_of_contact_id:
        person_result = await forward(
            "execute",
            _exec("find_one_person", {"id": point_of_contact_id}, identity),
        )
        point_of_contact = _extract_optional_single_record(person_result)

    completeness = build_bridge_fetch_completeness(
        has_company=company is not None,
        has_contact=point_of_contact is not None,
        pipeline_source=pipeline_source,
        has_scalar_timeline=has_scalar_timeline,
    )

    return RawOpportunityBundle(
        opportunity_id=opportunity_id,
        opportunity=opportunity,
        company=company,
        point_of_contact=point_of_contact,
        notes=notes,
        tasks=tasks,
        timeline=timeline,
        pipeline_stages=pipeline_stages,
        context_completeness=completeness.model_dump(),
    )
