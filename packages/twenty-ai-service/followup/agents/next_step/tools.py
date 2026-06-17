"""Internal analysis tools for the Next Step Intelligence Agent.

Replaces RAG retrieval with deterministic, structured knowledge functions.
All tools are pure Python — no I/O, no network calls, no external dependencies.
Results are injected into the LLM prompt by prompts.py.

These functions are internal to the agent and must not be called by any
component outside followup/agents/next_step/.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from followup.context.schemas import DealContext


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


@dataclass
class StageGuidance:
    stage: str
    objective: str
    key_activities: list[str]
    exit_criteria: list[str]
    common_pitfalls: list[str]


@dataclass
class BANTGap:
    dimension: str   # Budget | Authority | Need | Timeline
    status: str      # confirmed | partial | missing
    detail: str


@dataclass
class BANTAssessment:
    gaps: list[BANTGap]
    is_fully_qualified: bool
    qualification_score: int  # 0-4


@dataclass
class EngagementHealth:
    status: str              # healthy | declining | stalled | cold
    days_since_last: int
    trend: str               # improving | stable | declining
    has_future_meeting: bool
    risk_flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Playbook knowledge base (replaces SALES_PLAYBOOKS RAG collection)
# ---------------------------------------------------------------------------

_STAGE_PLAYBOOKS: dict[str, StageGuidance] = {
    "Discovery": StageGuidance(
        stage="Discovery",
        objective="Understand the buyer's pain, stakeholders, and initial budget expectations.",
        key_activities=[
            "Conduct discovery call to document pain points",
            "Identify and map all stakeholders",
            "Confirm a decision maker is engaged",
            "Probe for initial budget range",
            "Confirm the buyer's decision timeline",
        ],
        exit_criteria=[
            "Pain point documented in the CRM",
            "Decision maker identified and engaged",
            "Initial budget range discussed",
            "Next meeting scheduled",
        ],
        common_pitfalls=[
            "Moving to Proposal without a confirmed decision maker",
            "Skipping the budget conversation to avoid awkwardness",
            "Leaving the call without a scheduled follow-up",
        ],
    ),
    "Qualification": StageGuidance(
        stage="Qualification",
        objective="Confirm all four BANT dimensions before investing in a proposal.",
        key_activities=[
            "Confirm budget range is within deal size",
            "Confirm the decision maker is actively engaged",
            "Validate the need aligns with your solution",
            "Get a committed decision timeline from the buyer",
        ],
        exit_criteria=[
            "All four BANT dimensions confirmed",
            "Executive sponsor or economic buyer engaged",
            "Mutual action plan agreed and documented",
        ],
        common_pitfalls=[
            "Advancing without confirmed budget",
            "Relying on a champion who lacks authority",
            "Treating vague timelines as firm commitments",
        ],
    ),
    "Proposal": StageGuidance(
        stage="Proposal",
        objective="Present a tailored proposal and secure verbal commitment.",
        key_activities=[
            "Send proposal to the decision maker",
            "Schedule a proposal walkthrough meeting",
            "Address objections and competitor concerns",
            "Confirm the evaluation criteria",
        ],
        exit_criteria=[
            "Proposal reviewed by the decision maker",
            "Key objections documented and addressed",
            "Verbal commitment or shortlist confirmation received",
        ],
        common_pitfalls=[
            "Sending the proposal without booking a walkthrough meeting first",
            "Not tailoring the proposal to documented pain points",
            "Letting the deal go dark after sending the proposal",
        ],
    ),
    "Negotiation": StageGuidance(
        stage="Negotiation",
        objective="Resolve all commercial and legal issues and move to verbal close.",
        key_activities=[
            "Document all open commercial and legal issues",
            "Engage legal and procurement contacts on both sides",
            "Offer concessions only within approved thresholds",
            "Set a firm mutual close date with the buyer",
        ],
        exit_criteria=[
            "All commercial issues resolved",
            "Verbal commitment received",
            "Contract sent for signature",
        ],
        common_pitfalls=[
            "Giving concessions without receiving something in return",
            "Letting legal reviews drag without a hard deadline",
            "Not involving your own legal team early enough",
        ],
    ),
}

_DEFAULT_PLAYBOOK = StageGuidance(
    stage="Unknown",
    objective="Advance the deal to the next stage.",
    key_activities=["Review deal status", "Engage stakeholders", "Schedule next touchpoint"],
    exit_criteria=["Stage-specific criteria defined with the buyer"],
    common_pitfalls=["Lack of defined next steps leading to deal stall"],
)


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def lookup_stage_playbook(stage: str) -> StageGuidance:
    """Return structured playbook guidance for the given pipeline stage."""
    return _STAGE_PLAYBOOKS.get(stage, _DEFAULT_PLAYBOOK)


def evaluate_bant_gaps(context: DealContext) -> BANTAssessment:
    """Deterministically assess BANT qualification gaps from the deal context."""
    gaps: list[BANTGap] = []

    # Budget — derived from active profile facts
    budget_fact = next(
        (f for f in context.active_facts if "bant_budget" in f.fact_key.lower()),
        None,
    )
    if budget_fact and "confirmed" in budget_fact.value.lower():
        gaps.append(BANTGap("Budget", "confirmed", budget_fact.value))
    elif budget_fact:
        gaps.append(BANTGap("Budget", "partial", budget_fact.value))
    else:
        gaps.append(BANTGap("Budget", "missing", "No budget fact recorded on this opportunity"))

    # Authority — derived from contact flags
    decision_makers = [c for c in context.contacts if c.is_decision_maker]
    if decision_makers:
        names = ", ".join(c.name for c in decision_makers)
        gaps.append(BANTGap("Authority", "confirmed", f"{names} flagged as decision maker(s)"))
    else:
        gaps.append(BANTGap("Authority", "missing", "No contact is flagged as decision maker"))

    # Need — inferred from timeline notes
    pain_terms = ("pain", "need", "problem", "challenge", "issue", "inefficiency")
    has_pain = any(
        any(term in (item.summary or "").lower() for term in pain_terms)
        for item in context.timeline
    )
    if has_pain:
        gaps.append(BANTGap("Need", "confirmed", "Pain point or business need documented in timeline"))
    else:
        gaps.append(BANTGap("Need", "missing", "No pain point or business need documented in timeline"))

    # Timeline — derived from close date
    if context.opportunity.close_date:
        gaps.append(BANTGap("Timeline", "confirmed", f"Close date set: {context.opportunity.close_date.date()}"))
    else:
        gaps.append(BANTGap("Timeline", "missing", "No close date set on the opportunity"))

    confirmed = sum(1 for g in gaps if g.status == "confirmed")
    return BANTAssessment(
        gaps=gaps,
        is_fully_qualified=confirmed == 4,
        qualification_score=confirmed,
    )


def assess_engagement_health(context: DealContext) -> EngagementHealth:
    """Assess engagement health from engagement metrics and open task state."""
    eng = context.engagement
    days = eng.days_since_last_activity
    current = eng.activity_count_14d
    prior = eng.activity_count_prior_14d

    if days > 14:
        status = "cold"
    elif days > 7:
        status = "stalled"
    elif prior > 0 and current < prior * 0.6:
        status = "declining"
    else:
        status = "healthy"

    if current > prior:
        trend = "improving"
    elif current < prior:
        trend = "declining"
    else:
        trend = "stable"

    risk_flags: list[str] = []
    if days > 7:
        risk_flags.append(f"No activity for {days} days")
    if not eng.has_future_meeting:
        risk_flags.append("No future meeting scheduled")
    overdue = [t for t in context.tasks if t.is_overdue]
    if overdue:
        risk_flags.append(f"{len(overdue)} overdue task(s) unresolved")
    if prior > 0 and current < prior * 0.5:
        risk_flags.append("Activity dropped >50% vs prior 14 days")

    return EngagementHealth(
        status=status,
        days_since_last=days,
        trend=trend,
        has_future_meeting=eng.has_future_meeting,
        risk_flags=risk_flags,
    )
