import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from followup.agents.risk.objections import detect_customer_objections
from followup.agents.risk.rules import (
    NOTIFICATION_PRIORITY,
    factor_severity_rank,
    get_rule_definition,
    is_closed_stage,
)
from followup.agents.risk.schemas import (
    Notification,
    NotificationDraft,
    NotificationSeverity,
    RiskScore,
    RiskScoreBreakdown,
)
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent

logger = logging.getLogger(__name__)

MAX_NOTIFICATIONS_PER_RUN = 2
DISMISS_SUPPRESS_DAYS = 7

NOTIFICATION_TEMPLATES: dict[str, str] = {
    "no_activity": (
        "Deal {deal_name} has no trustworthy dated activity on record. "
        "Review the opportunity and confirm the next customer action."
    ),
    "no_meeting": (
        "Deal {deal_name} has no upcoming meeting scheduled. "
        "Book time with the buyer to maintain momentum."
    ),
    "stalled_stage": (
        "Deal {deal_name} has been in {stage} longer than expected. "
        "Review blockers and define a clear next step."
    ),
    "missing_decision_maker": (
        "Deal {deal_name} is past early qualification but lacks a decision maker. "
        "Identify and engage the economic buyer."
    ),
    "missing_proposal": (
        "Deal {deal_name} is at PROPOSAL stage without proposal evidence. "
        "Send or document the proposal to keep the deal moving."
    ),
    "overdue_tasks": (
        "Deal {deal_name} has overdue tasks. "
        "Complete or reschedule them to avoid stalling the deal."
    ),
    "engagement_drop": (
        "Engagement on {deal_name} has dropped significantly. "
        "Re-engage contacts with a targeted follow-up."
    ),
    "past_expected_close_date": (
        "Deal {deal_name} is past its expected close date. "
        "Confirm whether the timeline should be updated or the deal advanced."
    ),
}

LLMCopyGenerator = Callable[
    [NotificationDraft, DealContext, RiskScore | None],
    Awaitable[tuple[str, str]],
]


def _evaluation_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _notification_sort_key(factor) -> tuple[int, int, int]:
    try:
        priority = NOTIFICATION_PRIORITY.index(factor.rule_id)
    except ValueError:
        priority = len(NOTIFICATION_PRIORITY)
    return (
        -factor_severity_rank(factor.severity),
        priority,
        -factor.points,
    )


def should_notify(
    breakdown: RiskScoreBreakdown,
    event: FollowUpEvent,
    existing_notifications: list[Notification],
    *,
    context: DealContext,
    now: datetime | None = None,
) -> list[NotificationDraft]:
    if is_closed_stage(context.opportunity.stage):
        return []

    opportunity_stage = event.payload.get("new_stage") or event.payload.get("stage")
    if opportunity_stage and is_closed_stage(str(opportunity_stage)):
        return []

    if breakdown.total < 40 and not any(
        factor.severity == "high" for factor in breakdown.factors
    ):
        return []

    suppressed_rule_ids = _recently_dismissed_rule_ids(
        existing_notifications,
        now=now,
    )
    eligible_factors = [
        factor
        for factor in breakdown.factors
        if factor.rule_id not in suppressed_rule_ids
    ]
    if not eligible_factors:
        return []

    eligible_factors.sort(key=_notification_sort_key)

    drafts: list[NotificationDraft] = []
    for factor in eligible_factors[:MAX_NOTIFICATIONS_PER_RUN]:
        rule_definition = get_rule_definition(factor.rule_id)
        if rule_definition is None:
            continue
        drafts.append(
            NotificationDraft(
                rule_id=factor.rule_id,
                title=rule_definition.title,
                severity=rule_definition.severity,
                template_key=rule_definition.template_key,
                opportunity_id=event.opportunity_id,
                user_id=event.user_id,
            ),
        )
    return drafts


def _recently_dismissed_rule_ids(
    existing_notifications: list[Notification],
    *,
    now: datetime | None = None,
) -> set[str]:
    current = _evaluation_now(now)
    cutoff = current - timedelta(days=DISMISS_SUPPRESS_DAYS)
    suppressed: set[str] = set()
    for notification in existing_notifications:
        if notification.status != "dismissed":
            continue
        dismissed_at = notification.dismissed_at
        if dismissed_at is None:
            continue
        if dismissed_at.tzinfo is None:
            dismissed_at = dismissed_at.replace(tzinfo=timezone.utc)
        if dismissed_at >= cutoff and notification.rule_id:
            suppressed.add(notification.rule_id)
    return suppressed


def _undated_timeline_excerpt(context: DealContext) -> str | None:
    for timeline_item in context.timeline:
        if timeline_item.occurred_at is not None:
            continue
        excerpt = timeline_item.summary or timeline_item.title
        if excerpt:
            return excerpt[:240]
    return None


def _render_template(template_key: str, context: DealContext) -> str:
    template = NOTIFICATION_TEMPLATES.get(
        template_key,
        "Deal {deal_name} needs attention based on recent risk signals.",
    )
    return template.format(
        deal_name=context.opportunity.name,
        days=context.engagement.days_since_last_activity,
        stage=context.opportunity.stage,
    )


def render_notification_template(template_key: str, context: DealContext) -> str:
    return _render_template(template_key, context)


def build_notification_reasoning(
    draft: NotificationDraft,
    context: DealContext,
    *,
    risk_score: RiskScore | None = None,
) -> str:
    matching_factor = None
    if risk_score is not None:
        matching_factor = next(
            (
                factor
                for factor in risk_score.factors
                if factor.rule_id == draft.rule_id
            ),
            None,
        )
    if matching_factor is not None:
        return matching_factor.reason
    return (
        f"Risk rule '{draft.rule_id}' triggered for "
        f"{context.opportunity.name} at stage {context.opportunity.stage}."
    )


async def template_notification_copy(
    draft: NotificationDraft,
    context: DealContext,
    *,
    risk_score: RiskScore | None = None,
) -> tuple[str, str]:
    return (
        render_notification_template(draft.template_key, context),
        build_notification_reasoning(draft, context, risk_score=risk_score),
    )


async def _default_llm_copy_generator(
    draft: NotificationDraft,
    context: DealContext,
    risk_score: RiskScore | None = None,
) -> tuple[str, str]:
    from agent.llm_client import LLMClient

    factor = None
    if risk_score is not None:
        factor = next(
            (
                item
                for item in risk_score.factors
                if item.rule_id == draft.rule_id
            ),
            None,
        )

    undated_excerpt = _undated_timeline_excerpt(context)
    objections = detect_customer_objections(context)
    objection_text = (
        "; ".join(
            f"{objection.category}: {objection.excerpt[:120]}"
            for objection in objections[:2]
        )
        if objections
        else "None detected"
    )

    prompt = (
        "Write a concise sales notification for a CRM deal at risk.\n"
        f"Title: {draft.title}\n"
        f"Severity: {draft.severity}\n"
        f"Deal: {context.opportunity.name}\n"
        f"Company: {context.company.name if context.company else 'Unknown'}\n"
        f"Stage: {context.opportunity.stage}\n"
        f"Amount: {context.opportunity.amount}\n"
        f"Expected close date: {context.opportunity.close_date}\n"
        f"Triggered factor: {factor.reason if factor else draft.rule_id}\n"
        f"Days since last trustworthy dated activity: "
        f"{context.engagement.days_since_last_activity}\n"
        f"Undated opportunity text excerpt: {undated_excerpt or 'None'}\n"
        f"Detected objections: {objection_text}\n"
        "Rules:\n"
        "- Do not claim an email or note happened on a specific date when occurred_at is null.\n"
        "- Use phrasing like 'The opportunity contains customer text expressing ...' for undated content.\n"
        "- Do not change severity or title.\n"
        "Return JSON with keys: body (2-3 sentences), reasoning_summary (1 sentence)."
    )
    client = LLMClient()
    response = client.get_openai_client().chat.completions.create(
        model=client.model,
        messages=[
            {
                "role": "system",
                "content": "You write actionable CRM risk notifications. JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    body = str(parsed.get("body", "")).strip()
    reasoning_summary = str(parsed.get("reasoning_summary", "")).strip()
    if not body:
        raise ValueError("LLM returned empty notification body")
    if not reasoning_summary:
        reasoning_summary = build_notification_reasoning(
            draft,
            context,
            risk_score=risk_score,
        )
    return body, reasoning_summary


async def generate_notification_copy(
    drafts: list[NotificationDraft],
    context: DealContext,
    *,
    risk_score: RiskScore | None = None,
    llm_generator: LLMCopyGenerator | None = None,
) -> list[Notification]:
    generator = llm_generator or _default_llm_copy_generator
    notifications: list[Notification] = []
    for draft in drafts:
        body = _render_template(draft.template_key, context)
        reasoning_summary = build_notification_reasoning(
            draft,
            context,
            risk_score=risk_score,
        )
        try:
            body, reasoning_summary = await generator(draft, context, risk_score)
        except Exception as error:
            logger.warning(
                "LLM notification copy failed for rule %s: %s",
                draft.rule_id,
                error,
            )
        notifications.append(
            Notification(
                opportunity_id=draft.opportunity_id,
                user_id=draft.user_id,
                title=draft.title,
                body=body,
                severity=draft.severity,
                status="unread",
                rule_id=draft.rule_id,
                reasoning_summary=reasoning_summary,
            ),
        )
    return notifications


def apply_notification_lifecycle(
    new_notifications: list[Notification],
    existing_notifications: list[Notification],
    *,
    now: datetime | None = None,
) -> list[Notification]:
    evaluation_time = _evaluation_now(now)
    suppressed_rule_ids = _recently_dismissed_rule_ids(
        existing_notifications,
        now=evaluation_time,
    )
    existing_keys: set[tuple[str, str]] = set()
    for notification in existing_notifications:
        if not notification.rule_id:
            continue
        key = (notification.opportunity_id, notification.rule_id)
        if notification.status == "dismissed":
            if notification.rule_id in suppressed_rule_ids:
                existing_keys.add(key)
            continue
        existing_keys.add(key)

    deduped: list[Notification] = []
    for notification in new_notifications:
        key = (notification.opportunity_id, notification.rule_id)
        if key in existing_keys:
            continue
        notification.status = "unread"
        deduped.append(notification)
        existing_keys.add(key)
    return deduped
