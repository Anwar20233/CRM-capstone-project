from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from followup.context.schemas import DealContext, EngagementMetrics, TaskSnapshot


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _recompute_task_overdue(tasks: list[TaskSnapshot], now: datetime) -> list[TaskSnapshot]:
    updated_tasks: list[TaskSnapshot] = []
    for task in tasks:
        is_overdue = False
        if task.due_at is not None and task.status.upper() != "DONE":
            is_overdue = _ensure_utc(task.due_at) < now
        updated_tasks.append(task.model_copy(update={"is_overdue": is_overdue}))
    return updated_tasks


def _has_trustworthy_timeline_timestamp(item) -> bool:
    if item.occurred_at is None:
        return False
    if item.timestamp_source == "unavailable":
        return False
    return True


def compute_engagement_metrics(context: DealContext, now: datetime) -> EngagementMetrics:
    activity_dates: list[datetime] = [
        _ensure_utc(item.occurred_at)
        for item in context.timeline
        if _has_trustworthy_timeline_timestamp(item)
    ]

    if activity_dates:
        latest_activity = max(activity_dates)
        days_since_last_activity = max(
            0,
            (now.date() - latest_activity.date()).days,
        )
    else:
        days_since_last_activity = None

    recent_start = now - timedelta(days=14)
    prior_start = now - timedelta(days=28)

    activity_count_14d = sum(
        1 for occurred_at in activity_dates if occurred_at >= recent_start
    )
    activity_count_prior_14d = sum(
        1
        for occurred_at in activity_dates
        if prior_start <= occurred_at < recent_start
    )

    has_future_meeting = any(
        meeting.starts_at is not None and _ensure_utc(meeting.starts_at) > now
        for meeting in context.meetings
    )

    return EngagementMetrics(
        days_since_last_activity=days_since_last_activity,
        activity_count_14d=activity_count_14d,
        activity_count_prior_14d=activity_count_prior_14d,
        has_future_meeting=has_future_meeting,
    )


def enrich_context(
    context: DealContext,
    *,
    provenance: Literal["profile_primary", "crm_fallback", "hybrid"] | None = None,
    now: datetime | None = None,
) -> DealContext:
    current_time = now or datetime.now(timezone.utc)
    tasks = _recompute_task_overdue(context.tasks, current_time)
    engagement = compute_engagement_metrics(
        context.model_copy(update={"tasks": tasks}),
        current_time,
    )
    return context.model_copy(
        update={
            "tasks": tasks,
            "engagement": engagement,
            "loaded_at": current_time,
            "context_provenance": provenance or context.context_provenance,
        },
    )
