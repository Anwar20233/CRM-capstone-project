import logging
from datetime import datetime, timezone

from followup.agents.risk.notifications import (
    LLMCopyGenerator,
    apply_notification_lifecycle,
    generate_notification_copy,
    should_notify,
)
from followup.agents.risk.rules import (
    compute_risk_score,
    detect_risk_signals,
    has_proposal_evidence,
    is_closed_stage,
    risk_level_for_score,
)
from followup.agents.risk.schemas import (
    Notification,
    ProfileFactUpdateSuggestion,
    RiskFactor,
    RiskNotificationAgentResult,
    RiskScore,
    RiskScoreBreakdown,
)
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent

logger = logging.getLogger(__name__)


def _evaluation_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def build_reasoning_summary(
    breakdown: RiskScoreBreakdown,
    context: DealContext,
) -> str:
    if not breakdown.factors:
        return "No risk factors detected; deal appears healthy."

    factor_summaries = [
        f"{factor.rule_id}: {factor.reason}" for factor in breakdown.factors
    ]
    top_factors = "; ".join(factor_summaries[:4])
    summary = (
        f"Risk score is {breakdown.total} ({breakdown.level.upper()}). "
        f"The main risks are {top_factors}."
    )

    if stage_at_or_past_proposal_without_evidence(breakdown, context):
        summary += " Proposal evidence was not found."
    elif has_proposal_evidence(context):
        summary += (
            " Proposal evidence was found in opportunity content, "
            "so the missing-proposal rule did not trigger."
        )

    if context.opportunity.stage_entered_at is None:
        summary += " Stage age is unavailable, so stalled-stage was not evaluated."

    completeness = context.context_completeness
    if completeness is not None:
        unavailable_sections: list[str] = []
        if completeness.meetings.status == "unavailable":
            unavailable_sections.append("meetings")
        if completeness.tasks.status == "unavailable":
            unavailable_sections.append("tasks")
        if unavailable_sections:
            joined = " and ".join(unavailable_sections)
            summary += (
                f" {joined.capitalize()} status could not be verified "
                "from the available reader tools."
            )

    if context.engagement.days_since_last_activity is None:
        summary += (
            " No trustworthy dated activity is available for recency scoring."
        )

    return summary


def stage_at_or_past_proposal_without_evidence(
    breakdown: RiskScoreBreakdown,
    context: DealContext,
) -> bool:
    return any(
        factor.rule_id == "missing_proposal" for factor in breakdown.factors
    )


def build_profile_fact_update_suggestions(
    context: DealContext,
    risk_score: RiskScore,
    *,
    score_delta: int | None = None,
) -> list[ProfileFactUpdateSuggestion]:
    suggestions = [
        ProfileFactUpdateSuggestion(
            fact_key="current_risk_score",
            value=str(risk_score.score),
        ),
        ProfileFactUpdateSuggestion(
            fact_key="current_risk_level",
            value=risk_score.level,
        ),
        ProfileFactUpdateSuggestion(
            fact_key="risk_factor_ids",
            value=",".join(factor.rule_id for factor in risk_score.factors),
        ),
        ProfileFactUpdateSuggestion(
            fact_key="last_risk_scored_at",
            value=risk_score.computed_at.isoformat(),
        ),
    ]
    if score_delta is not None:
        suggestions.append(
            ProfileFactUpdateSuggestion(
                fact_key="risk_score_delta",
                value=str(score_delta),
            ),
        )
    return suggestions


async def merge_deal_risk_report_signals(
    context: DealContext,
    factors: list[RiskFactor],
) -> list[RiskFactor]:
    try:
        from agent.tools.workflows import _deal_risk_report

        report = await _deal_risk_report(context.opportunity.id)
        if not report.get("ok"):
            return factors
        report_data = report.get("data") or {}
        for risk_item in report_data.get("risks") or []:
            risk_type = str(risk_item.get("type", "external"))
            detail = str(risk_item.get("detail", "External risk signal"))
            severity = str(risk_item.get("severity", "medium"))
            points = {"high": 15, "medium": 10, "low": 5}.get(severity, 10)
            rule_id = f"deal_risk_report_{risk_type}"
            if any(factor.rule_id == rule_id for factor in factors):
                continue
            factors.append(
                RiskFactor(
                    rule_id=rule_id,
                    points=points,
                    title=f"External risk: {risk_type}",
                    reason=detail,
                    severity=severity if severity in {"low", "medium", "high"} else "medium",  # type: ignore[arg-type]
                ),
            )
    except Exception as error:
        logger.debug("deal_risk_report enrichment skipped: %s", error)
    return factors


async def run_risk_notification_agent(
    context: DealContext,
    event: FollowUpEvent,
    existing_notifications: list[Notification],
    *,
    now: datetime | None = None,
    llm_generator: LLMCopyGenerator | None = None,
) -> RiskNotificationAgentResult:
    evaluation_time = _evaluation_now(now)

    breakdown = compute_risk_score(context, now=evaluation_time)
    signals = await merge_deal_risk_report_signals(
        context,
        breakdown.factors,
    )

    if signals != breakdown.factors:
        total = min(100, sum(factor.points for factor in signals))
        breakdown = RiskScoreBreakdown(
            total=total,
            level=risk_level_for_score(total),
            factors=signals,
        )

    reasoning_summary = build_reasoning_summary(breakdown, context)
    risk_score = RiskScore(
        score=breakdown.total,
        level=breakdown.level,
        factors=breakdown.factors,
        computed_at=evaluation_time,
        reasoning_summary=reasoning_summary,
    )

    drafts = (
        []
        if is_closed_stage(context.opportunity.stage)
        else should_notify(
            breakdown,
            event,
            existing_notifications,
            context=context,
            now=evaluation_time,
        )
    )
    notifications = await generate_notification_copy(
        drafts,
        context,
        risk_score=risk_score,
        llm_generator=llm_generator,
    )
    notifications = apply_notification_lifecycle(
        notifications,
        existing_notifications,
        now=evaluation_time,
    )

    return RiskNotificationAgentResult(
        risk_score=risk_score,
        notifications=notifications,
        reasoning_summary=reasoning_summary,
    )
