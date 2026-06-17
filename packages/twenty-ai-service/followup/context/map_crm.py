from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from followup.context.completeness import ContextCompleteness
from followup.context.crm_fetch import RawOpportunityBundle
from followup.context.schemas import (
    CompanySnapshot,
    ContactSnapshot,
    DealContext,
    MeetingSnapshot,
    OpportunitySnapshot,
    PipelineMeta,
    TaskSnapshot,
    TimelineItem,
)
from followup.context.stage_normalization import (
    FALLBACK_PIPELINE_STAGES,
    build_stage_sla_days,
    normalize_stage,
)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_amount(opportunity: dict[str, Any]) -> float | None:
    amount = opportunity.get("amount")
    if isinstance(amount, dict):
        micros = amount.get("amountMicros")
        if micros is not None:
            return float(micros) / 1_000_000
    if isinstance(amount, (int, float)):
        return float(amount)
    return None


def _stage_label(opportunity: dict[str, Any]) -> str:
    return normalize_stage(opportunity.get("stage"))


def _stage_entered_at(opportunity: dict[str, Any]) -> datetime | None:
    return _parse_datetime(opportunity.get("stageEnteredAt"))


def _person_name(person: dict[str, Any]) -> str:
    name = person.get("name")
    if isinstance(name, dict):
        first_name = name.get("firstName") or ""
        last_name = name.get("lastName") or ""
        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            return full_name
    return str(person.get("id", "Unknown contact"))


def _note_title(note: dict[str, Any]) -> str:
    title = note.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    body = note.get("bodyV2") or note.get("body")
    if isinstance(body, dict):
        markdown = body.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown.strip()[:80]
    if isinstance(body, str) and body.strip():
        return body.strip()[:80]
    return "Note"


def _note_summary(note: dict[str, Any]) -> str | None:
    body = note.get("bodyV2") or note.get("body")
    if isinstance(body, dict):
        markdown = body.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown.strip()[:240]
    if isinstance(body, str) and body.strip():
        return body.strip()[:240]
    return None


def _task_is_overdue(task: dict[str, Any], now: datetime) -> bool:
    due_at = _parse_datetime(task.get("dueAt"))
    status = str(task.get("status", "")).upper()
    if due_at is None or status == "DONE":
        return False
    return due_at < now


def _build_pipeline_meta(bundle: RawOpportunityBundle) -> PipelineMeta:
    stages: list[str] = []
    stage_sla_days: dict[str, int] = {}
    source: Literal["crm_metadata", "fallback_defaults"] = "fallback_defaults"

    if bundle.pipeline_stages:
        sorted_stages = sorted(
            bundle.pipeline_stages,
            key=lambda stage: stage.get("position", 0),
        )
        for stage in sorted_stages:
            canonical_stage = normalize_stage(
                stage.get("value") or stage.get("label"),
            )
            if canonical_stage == "UNKNOWN":
                continue
            stages.append(canonical_stage)
        if stages:
            stage_sla_days = build_stage_sla_days(stages)
            source = "crm_metadata"

    if not stages:
        stages = list(FALLBACK_PIPELINE_STAGES)
        stage_sla_days = build_stage_sla_days(stages)
        source = "fallback_defaults"

    return PipelineMeta(
        stages=stages,
        stage_sla_days=stage_sla_days,
        source=source,
    )


def _timeline_provenance(item: dict) -> tuple[str | None, str | None]:
    return (
        str(item["source"]) if item.get("source") else None,
        str(item["timestamp_source"]) if item.get("timestamp_source") else None,
    )


def _is_undated_scalar_timeline_item(item: dict) -> bool:
    return item.get("timestamp_source") == "unavailable"


def _append_timeline_item(
    timeline_items: list[TimelineItem],
    *,
    event_type: str,
    item: dict,
    occurred_at,
) -> None:
    source, timestamp_source = _timeline_provenance(item)
    if event_type == "note":
        timeline_items.append(
            TimelineItem(
                type="note",
                title=_note_title(item),
                summary=_note_summary(item)
                or str(item.get("summary") or "")[:240]
                or None,
                occurred_at=occurred_at,
                source=source,
                timestamp_source=timestamp_source,
            ),
        )
    elif event_type == "email":
        timeline_items.append(
            TimelineItem(
                type="email",
                title=str(item.get("title") or "Email"),
                summary=str(item.get("summary") or item.get("body") or "")[:240]
                or None,
                occurred_at=occurred_at,
                source=source,
                timestamp_source=timestamp_source,
            ),
        )
    elif event_type == "task":
        timeline_items.append(
            TimelineItem(
                type="task",
                title=str(item.get("title") or "Task"),
                summary=str(item.get("bodyV2") or item.get("body") or "")[:240]
                or None,
                occurred_at=occurred_at,
                source=source,
                timestamp_source=timestamp_source,
            ),
        )


def _build_timeline(bundle: RawOpportunityBundle) -> list[TimelineItem]:
    timeline_items: list[TimelineItem] = []
    for event in bundle.timeline:
        event_type = str(event.get("type", "activity"))
        item = event.get("item") or {}
        occurred_at = _parse_datetime(event.get("updatedAt")) or _parse_datetime(
            item.get("updatedAt"),
        )
        if occurred_at is None and not _is_undated_scalar_timeline_item(item):
            continue
        _append_timeline_item(
            timeline_items,
            event_type=event_type,
            item=item,
            occurred_at=occurred_at,
        )
    return timeline_items


def _build_tasks(bundle: RawOpportunityBundle) -> list[TaskSnapshot]:
    now = datetime.now(timezone.utc)
    tasks: list[TaskSnapshot] = []
    for task in bundle.tasks:
        task_id = task.get("id")
        if not task_id:
            continue
        tasks.append(
            TaskSnapshot(
                id=str(task_id),
                title=str(task.get("title") or "Task"),
                status=str(task.get("status") or "TODO"),
                due_at=_parse_datetime(task.get("dueAt")),
                is_overdue=_task_is_overdue(task, now),
            ),
        )
    return tasks


def map_deal_context_fallback(bundle: RawOpportunityBundle) -> DealContext:
    opportunity = bundle.opportunity or {}
    opportunity_id = str(opportunity.get("id") or bundle.opportunity_id)

    company_snapshot: CompanySnapshot | None = None
    if bundle.company:
        company_snapshot = CompanySnapshot(
            id=str(bundle.company.get("id", "")),
            name=str(bundle.company.get("name") or "Unknown company"),
            industry=(
                str(bundle.company.get("industry"))
                if bundle.company.get("industry")
                else None
            ),
        )

    contacts: list[ContactSnapshot] = []
    if bundle.point_of_contact:
        contacts.append(
            ContactSnapshot(
                id=str(bundle.point_of_contact.get("id", "")),
                name=_person_name(bundle.point_of_contact),
                role=(
                    str(bundle.point_of_contact.get("jobTitle"))
                    if bundle.point_of_contact.get("jobTitle")
                    else None
                ),
                is_decision_maker=False,
            ),
        )

    owner = opportunity.get("owner") or opportunity.get("createdBy")
    owner_id = None
    if isinstance(owner, dict):
        owner_id = owner.get("id")
    if owner_id is None:
        owner_id = opportunity.get("ownerId")

    context_completeness = None
    if bundle.context_completeness:
        context_completeness = ContextCompleteness.model_validate(
            bundle.context_completeness,
        )

    return DealContext(
        opportunity=OpportunitySnapshot(
            id=opportunity_id,
            name=str(opportunity.get("name") or "Unnamed opportunity"),
            stage=_stage_label(opportunity),
            amount=_parse_amount(opportunity),
            close_date=_parse_datetime(opportunity.get("closeDate")),
            company_id=(
                str(bundle.company.get("id"))
                if bundle.company and bundle.company.get("id")
                else None
            ),
            owner_id=str(owner_id) if owner_id else None,
            updated_at=_parse_datetime(opportunity.get("updatedAt")),
            stage_entered_at=_stage_entered_at(opportunity),
        ),
        company=company_snapshot,
        contacts=contacts,
        timeline=_build_timeline(bundle),
        tasks=_build_tasks(bundle),
        meetings=[],
        pipeline_meta=_build_pipeline_meta(bundle),
        context_provenance="crm_fallback",
        context_completeness=context_completeness,
    )
