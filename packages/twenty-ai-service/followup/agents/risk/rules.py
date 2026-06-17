from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from followup.agents.risk.schemas import (
    NotificationSeverity,
    RiskFactor,
    RiskLevel,
    RiskScoreBreakdown,
)
from followup.context.completeness import section_status
from followup.context.schemas import DealContext
from followup.context.stage_normalization import (
    CANONICAL_CLOSED_STAGES,
    is_closed_stage,
    normalize_stage,
    stage_at_or_after,
)

CLOSED_STAGES = CANONICAL_CLOSED_STAGES

ENGAGEMENT_DROP_RATIO = 0.5
DECISION_MAKER_STAGE_THRESHOLD = "MEETING"

PROPOSAL_EVIDENCE_PHRASES: tuple[str, ...] = (
    "proposal",
    "quote",
    "quotation",
    "pricing",
    "commercial offer",
    "contract",
    "statement of work",
    "order form",
    "scope of work",
)
PROPOSAL_EVIDENCE_TOKENS = frozenset({"proposal", "quote", "quotation", "pricing", "contract", "sow"})


@dataclass(frozen=True)
class RiskRuleDefinition:
    id: str
    points: int
    severity: NotificationSeverity
    title: str
    description: str
    template_key: str


RULE_DEFINITIONS: tuple[RiskRuleDefinition, ...] = (
    RiskRuleDefinition(
        id="no_activity_7d",
        points=25,
        severity="high",
        title="No recent activity",
        description="No trustworthy dated activity in the last 7 days",
        template_key="no_activity",
    ),
    RiskRuleDefinition(
        id="no_future_meeting",
        points=20,
        severity="high",
        title="No upcoming meeting",
        description="No future meeting recorded for an open deal",
        template_key="no_meeting",
    ),
    RiskRuleDefinition(
        id="stalled_stage",
        points=20,
        severity="high",
        title="Deal stalled in stage",
        description="Deal has remained in stage longer than the configured SLA",
        template_key="stalled_stage",
    ),
    RiskRuleDefinition(
        id="missing_decision_maker",
        points=15,
        severity="medium",
        title="Missing decision maker",
        description="No decision-maker contact identified past qualification",
        template_key="missing_decision_maker",
    ),
    RiskRuleDefinition(
        id="missing_proposal",
        points=20,
        severity="high",
        title="Missing proposal evidence",
        description="Deal is at or past proposal stage without proposal evidence",
        template_key="missing_proposal",
    ),
    RiskRuleDefinition(
        id="overdue_tasks",
        points=15,
        severity="medium",
        title="Overdue tasks",
        description="One or more open tasks are overdue",
        template_key="overdue_tasks",
    ),
    RiskRuleDefinition(
        id="engagement_drop",
        points=10,
        severity="medium",
        title="Engagement declining",
        description="Recent dated activity dropped versus the prior 14-day window",
        template_key="engagement_drop",
    ),
    RiskRuleDefinition(
        id="past_expected_close_date",
        points=20,
        severity="high",
        title="Past expected close date",
        description="Expected close date has passed for an open deal",
        template_key="past_expected_close_date",
    ),
)

_RULES_BY_ID = {rule.id: rule for rule in RULE_DEFINITIONS}


def _evaluation_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def days_in_stage(context: DealContext, *, now: datetime | None = None) -> int | None:
    reference = context.opportunity.stage_entered_at
    if reference is None:
        return None
    current = _evaluation_now(now)
    return max(0, (current - _ensure_utc(reference)).days)


def stage_sla_days_for(context: DealContext) -> int | None:
    stage = normalize_stage(context.opportunity.stage)
    sla_days = context.pipeline_meta.stage_sla_days.get(stage)
    return sla_days


def _default_pipeline_stages() -> list[str]:
    return ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"]


def stage_at_or_past_proposal(context: DealContext) -> bool:
    pipeline_stages = context.pipeline_meta.stages or _default_pipeline_stages()
    return stage_at_or_after(
        context.opportunity.stage,
        "PROPOSAL",
        pipeline_stages,
    )


def stage_at_or_past_decision_maker_threshold(context: DealContext) -> bool:
    pipeline_stages = context.pipeline_meta.stages or _default_pipeline_stages()
    return stage_at_or_after(
        context.opportunity.stage,
        DECISION_MAKER_STAGE_THRESHOLD,
        pipeline_stages,
    )


def has_proposal_evidence(context: DealContext) -> bool:
    for timeline_item in context.timeline:
        haystack = " ".join(
            filter(
                None,
                [
                    timeline_item.title,
                    timeline_item.summary or "",
                ],
            ),
        ).lower()
        if not haystack:
            continue
        for phrase in PROPOSAL_EVIDENCE_PHRASES:
            if phrase in haystack:
                return True
        tokens = {
            token.strip(".,;:!?()[]")
            for token in haystack.split()
        }
        if tokens & PROPOSAL_EVIDENCE_TOKENS:
            return True
    return False


def _build_factor(
    rule: RiskRuleDefinition,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> RiskFactor:
    return RiskFactor(
        rule_id=rule.id,
        points=rule.points,
        severity=rule.severity,
        title=rule.title,
        reason=reason,
        metadata=metadata or {},
    )


def evaluate_no_activity_7d(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["no_activity_7d"]
    timeline_status = section_status(context.context_completeness, "timeline")
    if timeline_status == "unavailable":
        return None
    days = context.engagement.days_since_last_activity
    if days is not None and days < 7:
        return None
    if days is None:
        reason = "No trustworthy dated activity is available."
    else:
        reason = f"No activity has been recorded for {days} days."
    return _build_factor(rule, reason, {"days_since_last_activity": days})


def evaluate_no_future_meeting(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["no_future_meeting"]
    if is_closed_stage(context.opportunity.stage):
        return None
    meetings_status = section_status(context.context_completeness, "meetings")
    if meetings_status == "unavailable":
        return None
    if context.engagement.has_future_meeting:
        return None
    return _build_factor(
        rule,
        "No future meeting is currently recorded.",
        {"has_future_meeting": False},
    )


def evaluate_stalled_stage(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> RiskFactor | None:
    rule = _RULES_BY_ID["stalled_stage"]
    if is_closed_stage(context.opportunity.stage):
        return None
    if context.opportunity.stage_entered_at is None:
        return None

    stage = normalize_stage(context.opportunity.stage)
    sla_days = stage_sla_days_for(context)
    if sla_days is None:
        return None

    days = days_in_stage(context, now=now)
    if days is None or days <= sla_days:
        return None

    return _build_factor(
        rule,
        f"In {stage} for {days} days (SLA: {sla_days} days).",
        {
            "days_in_stage": days,
            "stage_sla_days": sla_days,
            "pipeline_meta_source": context.pipeline_meta.source,
            "stage": stage,
        },
    )


def evaluate_missing_decision_maker(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["missing_decision_maker"]
    if is_closed_stage(context.opportunity.stage):
        return None
    if not stage_at_or_past_decision_maker_threshold(context):
        return None
    if any(contact.is_decision_maker for contact in context.contacts):
        return None
    stage = normalize_stage(context.opportunity.stage)
    return _build_factor(
        rule,
        f"No decision-maker contact is identified at the {stage} stage.",
        {"stage": stage, "contact_count": len(context.contacts)},
    )


def evaluate_missing_proposal(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["missing_proposal"]
    if is_closed_stage(context.opportunity.stage):
        return None
    if not stage_at_or_past_proposal(context):
        return None
    if has_proposal_evidence(context):
        return None
    return _build_factor(
        rule,
        "Stage is at or past PROPOSAL but no proposal evidence was found.",
        {"stage": normalize_stage(context.opportunity.stage)},
    )


def evaluate_overdue_tasks(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["overdue_tasks"]
    tasks_status = section_status(context.context_completeness, "tasks")
    if tasks_status == "unavailable":
        return None
    overdue_count = sum(1 for task in context.tasks if task.is_overdue)
    if overdue_count == 0:
        return None
    return _build_factor(
        rule,
        f"{overdue_count} overdue task(s) on this deal.",
        {"overdue_task_count": overdue_count},
    )


def evaluate_engagement_drop(context: DealContext) -> RiskFactor | None:
    rule = _RULES_BY_ID["engagement_drop"]
    prior = context.engagement.activity_count_prior_14d
    recent = context.engagement.activity_count_14d
    if prior <= 0:
        return None
    if recent >= prior * ENGAGEMENT_DROP_RATIO:
        return None
    return _build_factor(
        rule,
        (
            f"Dated activity dropped from {prior} to {recent} "
            "in the last 14 days."
        ),
        {
            "activity_count_prior_14d": prior,
            "activity_count_14d": recent,
            "drop_ratio_threshold": ENGAGEMENT_DROP_RATIO,
        },
    )


def evaluate_past_expected_close_date(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> RiskFactor | None:
    rule = _RULES_BY_ID["past_expected_close_date"]
    if is_closed_stage(context.opportunity.stage):
        return None
    close_date = context.opportunity.close_date
    if close_date is None:
        return None

    current = _evaluation_now(now)
    close_at = _ensure_utc(close_date)
    if close_at >= current:
        return None

    days_overdue = max(0, (current.date() - close_at.date()).days)
    return _build_factor(
        rule,
        f"The expected close date passed {days_overdue} days ago.",
        {
            "days_overdue": days_overdue,
            "close_date": close_at.isoformat(),
        },
    )


_RULE_EVALUATORS = (
    evaluate_no_activity_7d,
    evaluate_no_future_meeting,
    evaluate_stalled_stage,
    evaluate_missing_decision_maker,
    evaluate_missing_proposal,
    evaluate_overdue_tasks,
    evaluate_engagement_drop,
    evaluate_past_expected_close_date,
)


def risk_level_for_score(score: int) -> RiskLevel:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


score_to_level = risk_level_for_score


def detect_risk_signals(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> list[RiskFactor]:
    factors: list[RiskFactor] = []
    for evaluator in _RULE_EVALUATORS:
        if evaluator is evaluate_stalled_stage or evaluator is evaluate_past_expected_close_date:
            factor = evaluator(context, now=now)
        else:
            factor = evaluator(context)
        if factor is not None:
            factors.append(factor)
    return factors


def compute_risk_score(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> RiskScoreBreakdown:
    factors = detect_risk_signals(context, now=now)
    total = min(100, sum(factor.points for factor in factors))
    return RiskScoreBreakdown(
        total=total,
        level=risk_level_for_score(total),
        factors=factors,
    )


def get_rule_definition(rule_id: str) -> RiskRuleDefinition | None:
    return _RULES_BY_ID.get(rule_id)


def get_rule_metadata(rule_id: str) -> dict[str, Any] | None:
    rule = get_rule_definition(rule_id)
    if rule is None:
        return None
    return {
        "id": rule.id,
        "points": rule.points,
        "title": rule.title,
        "severity": rule.severity,
        "template_key": rule.template_key,
        "description": rule.description,
    }


def factor_severity_rank(severity: NotificationSeverity) -> int:
    return {"high": 3, "medium": 2, "low": 1}[severity]


NOTIFICATION_PRIORITY: list[str] = [
    "past_expected_close_date",
    "no_activity_7d",
    "no_future_meeting",
    "missing_decision_maker",
    "missing_proposal",
    "overdue_tasks",
    "stalled_stage",
    "engagement_drop",
]
