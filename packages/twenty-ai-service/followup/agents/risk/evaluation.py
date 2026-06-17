from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from followup.agents.risk.rules import (
    RULE_DEFINITIONS,
    days_in_stage,
    evaluate_engagement_drop,
    evaluate_missing_decision_maker,
    evaluate_missing_proposal,
    evaluate_no_activity_7d,
    evaluate_no_future_meeting,
    evaluate_overdue_tasks,
    evaluate_past_expected_close_date,
    evaluate_stalled_stage,
    has_proposal_evidence,
    stage_at_or_past_decision_maker_threshold,
    stage_at_or_past_proposal,
    stage_sla_days_for,
)
from followup.agents.risk.schemas import RiskFactor
from followup.context.completeness import section_status
from followup.context.schemas import DealContext
from followup.context.stage_normalization import is_closed_stage, normalize_stage

RuleEvaluationStatus = Literal["triggered", "not_triggered", "skipped"]


class RuleEvaluation(BaseModel):
    rule_id: str
    status: RuleEvaluationStatus
    reason: str
    factor: RiskFactor | None = None


def _explain_no_activity_7d(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    timeline_status = section_status(context.context_completeness, "timeline")
    if timeline_status == "unavailable":
        return (
            "skipped",
            "Skipped because timeline data could not be loaded.",
        )
    days = context.engagement.days_since_last_activity
    if days is not None and days < 7:
        return (
            "not_triggered",
            f"Not triggered because dated activity occurred {days} day(s) ago.",
        )
    return (
        "not_triggered",
        "Not triggered because recent dated activity thresholds were not met.",
    )


def _explain_no_future_meeting(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    if is_closed_stage(context.opportunity.stage):
        return ("skipped", "Skipped because the opportunity is in a closed stage.")
    meetings_status = section_status(context.context_completeness, "meetings")
    if meetings_status == "unavailable":
        return (
            "skipped",
            "Skipped because meeting data could not be verified from the available reader tools.",
        )
    if context.engagement.has_future_meeting:
        return (
            "not_triggered",
            "Not triggered because a future meeting is recorded.",
        )
    return (
        "not_triggered",
        "Not triggered because meeting status did not meet the rule threshold.",
    )


def _explain_stalled_stage(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> tuple[RuleEvaluationStatus, str]:
    if is_closed_stage(context.opportunity.stage):
        return ("skipped", "Skipped because the opportunity is in a closed stage.")
    if context.opportunity.stage_entered_at is None:
        return (
            "skipped",
            "Skipped because stage_entered_at is unavailable.",
        )
    sla_days = stage_sla_days_for(context)
    if sla_days is None:
        return (
            "skipped",
            "Skipped because no SLA is configured for the current stage.",
        )
    days = days_in_stage(context, now=now)
    if days is None:
        return ("skipped", "Skipped because stage age could not be calculated.")
    stage = normalize_stage(context.opportunity.stage)
    if days <= sla_days:
        return (
            "not_triggered",
            f"Not triggered because {stage} age ({days} days) is within the SLA ({sla_days} days).",
        )
    return (
        "not_triggered",
        "Not triggered because stage-age thresholds were not met.",
    )


def _explain_missing_decision_maker(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    if is_closed_stage(context.opportunity.stage):
        return ("skipped", "Skipped because the opportunity is in a closed stage.")
    if not stage_at_or_past_decision_maker_threshold(context):
        return (
            "skipped",
            "Skipped because the deal has not reached the decision-maker qualification stage.",
        )
    if any(contact.is_decision_maker for contact in context.contacts):
        return (
            "not_triggered",
            "Not triggered because a decision-maker contact is identified.",
        )
    contacts_status = section_status(context.context_completeness, "contacts")
    if contacts_status == "unavailable":
        return (
            "skipped",
            "Skipped because contact data could not be verified.",
        )
    return (
        "not_triggered",
        "Not triggered because contact coverage did not meet the rule threshold.",
    )


def _explain_missing_proposal(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    if is_closed_stage(context.opportunity.stage):
        return ("skipped", "Skipped because the opportunity is in a closed stage.")
    if not stage_at_or_past_proposal(context):
        return (
            "skipped",
            "Skipped because the deal is not yet at or past PROPOSAL.",
        )
    if has_proposal_evidence(context):
        return (
            "not_triggered",
            "Not triggered because proposal evidence was found in opportunity content.",
        )
    return (
        "not_triggered",
        "Not triggered because proposal evidence checks did not meet the threshold.",
    )


def _explain_overdue_tasks(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    tasks_status = section_status(context.context_completeness, "tasks")
    if tasks_status == "unavailable":
        return (
            "skipped",
            "Skipped because task data could not be verified from the available reader tools.",
        )
    overdue_count = sum(1 for task in context.tasks if task.is_overdue)
    if overdue_count == 0:
        return (
            "not_triggered",
            "Not triggered because no overdue tasks were found.",
        )
    return (
        "not_triggered",
        "Not triggered because overdue-task thresholds were not met.",
    )


def _explain_engagement_drop(context: DealContext) -> tuple[RuleEvaluationStatus, str]:
    prior = context.engagement.activity_count_prior_14d
    recent = context.engagement.activity_count_14d
    if prior <= 0:
        return (
            "not_triggered",
            "Not triggered because no prior activity baseline exists.",
        )
    if recent >= prior * 0.5:
        return (
            "not_triggered",
            f"Not triggered because recent activity ({recent}) did not drop below half of the prior period ({prior}).",
        )
    return (
        "not_triggered",
        "Not triggered because engagement-drop thresholds were not met.",
    )


def _explain_past_expected_close_date(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> tuple[RuleEvaluationStatus, str]:
    if is_closed_stage(context.opportunity.stage):
        return ("skipped", "Skipped because the opportunity is in a closed stage.")
    if context.opportunity.close_date is None:
        return (
            "skipped",
            "Skipped because no expected close date is available.",
        )
    factor = evaluate_past_expected_close_date(context, now=now)
    if factor is None:
        return (
            "not_triggered",
            "Not triggered because the expected close date has not passed.",
        )
    return (
        "not_triggered",
        "Not triggered because close-date thresholds were not met.",
    )


_EXPLAINERS = {
    "no_activity_7d": _explain_no_activity_7d,
    "no_future_meeting": _explain_no_future_meeting,
    "stalled_stage": _explain_stalled_stage,
    "missing_decision_maker": _explain_missing_decision_maker,
    "missing_proposal": _explain_missing_proposal,
    "overdue_tasks": _explain_overdue_tasks,
    "engagement_drop": _explain_engagement_drop,
    "past_expected_close_date": _explain_past_expected_close_date,
}

_EVALUATORS = {
    "no_activity_7d": lambda context, now=None: evaluate_no_activity_7d(context),
    "no_future_meeting": lambda context, now=None: evaluate_no_future_meeting(context),
    "stalled_stage": evaluate_stalled_stage,
    "missing_decision_maker": lambda context, now=None: evaluate_missing_decision_maker(context),
    "missing_proposal": lambda context, now=None: evaluate_missing_proposal(context),
    "overdue_tasks": lambda context, now=None: evaluate_overdue_tasks(context),
    "engagement_drop": lambda context, now=None: evaluate_engagement_drop(context),
    "past_expected_close_date": evaluate_past_expected_close_date,
}


def evaluate_all_rules(
    context: DealContext,
    *,
    now: datetime | None = None,
) -> list[RuleEvaluation]:
    evaluations: list[RuleEvaluation] = []
    for rule in RULE_DEFINITIONS:
        evaluator = _EVALUATORS[rule.id]
        if rule.id in {"stalled_stage", "past_expected_close_date"}:
            factor = evaluator(context, now=now)
        else:
            factor = evaluator(context, now=now)
        if factor is not None:
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule.id,
                    status="triggered",
                    reason=factor.reason,
                    factor=factor,
                ),
            )
            continue

        explainer = _EXPLAINERS[rule.id]
        if rule.id in {"stalled_stage", "past_expected_close_date"}:
            status, reason = explainer(context, now=now)
        else:
            status, reason = explainer(context)
        evaluations.append(
            RuleEvaluation(
                rule_id=rule.id,
                status=status,
                reason=reason,
            ),
        )
    return evaluations
