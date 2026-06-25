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

from followup.knowledge import skill_store
from followup.next_step.context.schemas import DealContext

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
_PLAYBOOKS_DIR = _KNOWLEDGE_DIR / "playbooks"


# ---------------------------------------------------------------------------
# Planning-skill discovery — the agent lists what guidance exists at run time
# and loads the skills it deems relevant, instead of being told to read a fixed
# playbook for the deal's exact stage. DB skills (edited in the Skills tab) take
# precedence; the bundled markdown below is the fallback default catalog.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DefaultPlannerSkill:
    name: str
    label: str
    description: str
    path: Path


_DEFAULT_PLANNER_SKILLS: list[_DefaultPlannerSkill] = [
    _DefaultPlannerSkill(
        f"{skill_store.PLAYBOOK_PREFIX}discovery",
        "Discovery stage playbook",
        "Goals, exit criteria and next actions for early discovery deals.",
        _PLAYBOOKS_DIR / "Discovery.md",
    ),
    _DefaultPlannerSkill(
        f"{skill_store.PLAYBOOK_PREFIX}qualification",
        "Qualification stage playbook",
        "Confirming fit, BANT, and advancing qualified deals.",
        _PLAYBOOKS_DIR / "Qualification.md",
    ),
    _DefaultPlannerSkill(
        f"{skill_store.PLAYBOOK_PREFIX}proposal",
        "Proposal stage playbook",
        "Driving a sent proposal to a decision.",
        _PLAYBOOKS_DIR / "Proposal.md",
    ),
    _DefaultPlannerSkill(
        f"{skill_store.PLAYBOOK_PREFIX}negotiation",
        "Negotiation stage playbook",
        "Resolving objections (price, terms, legal) and getting to signature.",
        _PLAYBOOKS_DIR / "Negotiation.md",
    ),
    _DefaultPlannerSkill(
        f"{skill_store.PLAYBOOK_PREFIX}closed",
        "Closed stage playbook",
        "Handling won/lost deals and post-close follow-through.",
        _PLAYBOOKS_DIR / "Closed.md",
    ),
    _DefaultPlannerSkill(
        skill_store.BANT_SKILL_NAME,
        "BANT qualification framework",
        "How to read Budget/Authority/Need/Timeline gaps and act on each.",
        _KNOWLEDGE_DIR / "bant.md",
    ),
    _DefaultPlannerSkill(
        skill_store.BEST_PRACTICES_SKILL_NAME,
        "Sales best practices",
        "Engagement cadence, multi-threading, task hygiene, action specificity.",
        _KNOWLEDGE_DIR / "best_practices.md",
    ),
]

_DEFAULT_PLANNER_BY_NAME = {skill.name: skill for skill in _DEFAULT_PLANNER_SKILLS}


def _planner_catalog() -> list[tuple[str, str, str]]:
    """Available planning skills as (name, label, description).

    Union of the company's DB skills (which win) and the bundled defaults, so
    new skills are discovered the moment they are added in the Skills tab.
    """
    catalog: dict[str, tuple[str, str, str]] = {
        skill.name: (skill.name, skill.label, skill.description)
        for skill in _DEFAULT_PLANNER_SKILLS
    }
    for prefix in skill_store.PLANNER_DB_PREFIXES:
        for row in skill_store._run_sync(skill_store.fetch_skills_by_prefix(prefix)):
            description = row.description or _DEFAULT_PLANNER_BY_NAME.get(
                row.name,
                _DefaultPlannerSkill(row.name, row.label, "", Path()),
            ).description
            catalog[row.name] = (row.name, row.label, description)
    return sorted(catalog.values(), key=lambda item: item[1].lower())


def planner_catalog_text() -> str:
    """Human-readable catalog injected into the agent's context at run time."""
    entries = _planner_catalog()
    if not entries:
        return "No planning skills are currently available."
    return "\n".join(f"- {name} — {label}: {description}" for name, label, description in entries)


@tool
def list_planning_skills() -> str:
    """List the planning skills available to you (name, label, description).

    Call this first to discover what guidance exists for this workspace, then
    read the skills relevant to the current deal with read_planning_skill.
    """
    return planner_catalog_text()


@tool
def read_planning_skill(name: str) -> str:
    """Read the full content of a planning skill by its exact name.

    Args:
        name: A skill name from list_planning_skills, e.g.
              'followup-playbook-negotiation' or 'followup-bant'.
    """
    content = skill_store.get_skill_content(name)
    if content:
        return content
    default = _DEFAULT_PLANNER_BY_NAME.get(name)
    if default and default.path.exists():
        return default.path.read_text(encoding="utf-8")
    return f"No planning skill named '{name}'. Available:\n{planner_catalog_text()}"


# Ordered list of tools available to the planning agent. Discovery-first: the
# agent lists the available skills and loads the relevant ones itself.
PLANNER_TOOLS = [list_planning_skills, read_planning_skill]


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
    "list_planning_skills",
    "read_planning_skill",
    "planner_catalog_text",
    "PLANNER_TOOLS",
    "BANTGap",
    "BANTSignals",
    "EngagementSignals",
    "compute_bant_gaps",
    "compute_engagement_signals",
]
