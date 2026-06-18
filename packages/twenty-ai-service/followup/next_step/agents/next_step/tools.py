"""Knowledge-base tools and deal-signal functions for the Next Step Intelligence Agent.

LangChain @tool-decorated functions are the agent's read-only knowledge interface:
the LLM calls them during the gather phase to retrieve the business rules it needs
before producing a plan. These are pure file reads — no network, no LLM.

Signal functions (compute_bant_gaps, compute_engagement_signals) are deterministic
computations from deal data. They produce factual observations that go into the
context message the planner reasons from — the LLM does not call these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.tools import tool

from followup.next_step.context.schemas import DealContext

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
_PLAYBOOKS_DIR = _KNOWLEDGE_DIR / "playbooks"


# ---------------------------------------------------------------------------
# LangChain tools — the LLM calls these to read knowledge files
# ---------------------------------------------------------------------------


@tool
def read_stage_playbook(stage: str) -> str:
    """Read the full sales playbook for a pipeline stage.

    Args:
        stage: Pipeline stage name, e.g. Discovery, Qualification, Proposal,
               Negotiation. Case-insensitive fallback is applied.

    Returns:
        Markdown playbook: stage goal, exit criteria, recommended next actions
        by signal, and common mistakes to avoid.
    """
    path = _PLAYBOOKS_DIR / f"{stage}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    for p in sorted(_PLAYBOOKS_DIR.glob("*.md")):
        if p.stem.lower() == stage.lower():
            return p.read_text(encoding="utf-8")
    available = ", ".join(p.stem for p in sorted(_PLAYBOOKS_DIR.glob("*.md")))
    return f"No playbook found for stage '{stage}'. Available stages: {available}."


@tool
def read_bant_framework() -> str:
    """Read the BANT qualification framework.

    Returns the full BANT framework: what each dimension (Budget, Authority,
    Need, Timeline) means, the gap signals to look for in deal context, and
    the recommended action for each missing or partial dimension.
    """
    path = _KNOWLEDGE_DIR / "bant.md"
    return path.read_text(encoding="utf-8") if path.exists() else "BANT framework not available."


@tool
def read_best_practices() -> str:
    """Read general B2B sales best practices.

    Returns guidance on engagement cadence, multi-threading, task hygiene,
    evidence-based recommendations, and action specificity.
    """
    path = _KNOWLEDGE_DIR / "best_practices.md"
    return path.read_text(encoding="utf-8") if path.exists() else "Best practices not available."


# Ordered list of tools available to the planning agent.
PLANNER_TOOLS = [read_stage_playbook, read_bant_framework, read_best_practices]


# ---------------------------------------------------------------------------
# Deal signal functions — deterministic, not LLM tools
# ---------------------------------------------------------------------------


@dataclass
class BANTGap:
    dimension: str  # Budget | Authority | Need | Timeline
    status: str     # confirmed | partial | missing
    detail: str


@dataclass
class BANTSignals:
    gaps: list[BANTGap]
    qualification_score: int  # 0–4
    is_fully_qualified: bool


@dataclass
class EngagementSignals:
    status: str              # healthy | declining | stalled | cold | unknown
    days_since_last: int | None  # None when no activity is on record
    trend: str               # improving | stable | declining
    has_future_meeting: bool
    risk_flags: list[str] = field(default_factory=list)


def compute_bant_gaps(context: DealContext) -> BANTSignals:
    """Compute BANT qualification gaps from the deal context."""
    gaps: list[BANTGap] = []

    budget_fact = next(
        (f for f in context.active_facts if "bant_budget" in f.fact_key.lower()), None
    )
    amount = context.opportunity.amount
    if budget_fact and "confirmed" in budget_fact.value.lower():
        gaps.append(BANTGap("Budget", "confirmed", budget_fact.value))
    elif budget_fact:
        gaps.append(BANTGap("Budget", "partial", budget_fact.value))
    elif amount and amount > 0:
        # A quantified deal amount is a budget signal — the deal is sized, even
        # though the customer's budget has not been explicitly confirmed.
        gaps.append(
            BANTGap(
                "Budget",
                "partial",
                f"Deal amount ${amount:,.0f} on record (budget not explicitly confirmed)",
            )
        )
    else:
        gaps.append(BANTGap("Budget", "missing", "No budget fact recorded and no deal amount set"))

    decision_makers = [c for c in context.contacts if c.is_decision_maker]
    if decision_makers:
        names = ", ".join(c.name for c in decision_makers)
        gaps.append(BANTGap("Authority", "confirmed", f"{names} flagged as decision maker(s)"))
    else:
        gaps.append(BANTGap("Authority", "missing", "No contact flagged as decision maker"))

    pain_terms = ("pain", "need", "problem", "challenge", "issue", "inefficiency")
    has_pain = any(
        any(term in (item.summary or "").lower() for term in pain_terms)
        for item in context.timeline
    )
    if has_pain:
        gaps.append(BANTGap("Need", "confirmed", "Pain point documented in timeline"))
    else:
        gaps.append(BANTGap("Need", "missing", "No pain point documented in timeline"))

    if context.opportunity.close_date:
        gaps.append(BANTGap("Timeline", "confirmed", f"Close date: {context.opportunity.close_date.date()}"))
    else:
        gaps.append(BANTGap("Timeline", "missing", "No close date set"))

    confirmed = sum(1 for g in gaps if g.status == "confirmed")
    return BANTSignals(gaps=gaps, qualification_score=confirmed, is_fully_qualified=confirmed == 4)


def compute_engagement_signals(context: DealContext) -> EngagementSignals:
    """Compute engagement health from deal activity metrics."""
    eng = context.engagement
    days = eng.days_since_last_activity
    current = eng.activity_count_14d
    prior = eng.activity_count_prior_14d

    # days is None when the deal has no activity on record — we genuinely don't
    # know its recency, so we report "unknown" rather than inventing a number.
    if days is None:
        status = "unknown"
    elif days > 14:
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
    if days is not None and days > 7:
        risk_flags.append(f"No activity for {days} days")
    if not eng.has_future_meeting:
        risk_flags.append("No future meeting scheduled")
    overdue = [t for t in context.tasks if t.is_overdue]
    if overdue:
        risk_flags.append(f"{len(overdue)} overdue task(s)")
    if prior > 0 and current < prior * 0.5:
        risk_flags.append("Activity dropped >50% vs prior 14 days")

    return EngagementSignals(
        status=status,
        days_since_last=days,
        trend=trend,
        has_future_meeting=eng.has_future_meeting,
        risk_flags=risk_flags,
    )


__all__ = [
    "read_stage_playbook",
    "read_bant_framework",
    "read_best_practices",
    "PLANNER_TOOLS",
    "BANTGap",
    "BANTSignals",
    "EngagementSignals",
    "compute_bant_gaps",
    "compute_engagement_signals",
]
