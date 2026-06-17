"""P3 — Risk Assessment contracts: request/assessment types + agent interface.

The Risk agent owns the deal's risk score. After extraction runs on an inbound
email, the orchestrator hands it the freshly-extracted ``facts`` plus the
synthesized ``narrative`` and the ``previous_score``; the agent re-scores the
deal using its own internal model (outside the follow-up agent's scope) and
returns the updated ``RiskAssessment``. The follow-up agent only READS that
score — it never computes risk itself.

``MockRiskAgent`` is the stand-in: it derives a correctly-shaped score + factors
from the facts/narrative it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

RISK_FACTOR_TYPES: frozenset[str] = frozenset(
    {
        "engagement_gap",
        "unresolved_objection",
        "deal_velocity_drop",
        "sentiment_decline",
        "stakeholder_change",
        "budget_concern",
    }
)

RISK_MODES: frozenset[str] = frozenset({"single", "sweep"})

SEVERITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

# Fact types (from extraction) that move the score, and the factor they imply.
_FACT_TYPE_TO_FACTOR: dict[str, str] = {
    "concern": "unresolved_objection",
    "competitor": "deal_velocity_drop",
    "budget": "budget_concern",
    "sentiment": "sentiment_decline",
    "stakeholder": "stakeholder_change",
    "deadline": "deal_velocity_drop",
}


@dataclass
class RiskFactor:
    """A single contributing risk signal identified by the agent."""

    factor_type: str  # ∈ RISK_FACTOR_TYPES
    description: str
    severity: str  # ∈ SEVERITY_LEVELS


@dataclass
class RiskAssessment:
    """Output of the Risk Assessment agent.

    ``risk_score`` is in [0.0, 1.0] (1.0 = highest risk). All fields are
    str/float/list/dict/None so asdict(assessment) is JSON-safe for storage
    in PendingAction.risk_assessment.
    """

    opportunity_id: str
    risk_score: float  # 0.0–1.0
    factors: list[RiskFactor]  # asdict recurses into each RiskFactor
    previous_score: float | None
    assessed_at: str  # ISO-8601
    metadata: dict = field(default_factory=dict)


@dataclass
class RiskAssessmentRequest:
    """Input the orchestrator sends after extraction: the new facts + narrative.

    ``facts`` are the extraction outputs about the client (each a JSON-safe dict
    with at least ``fact_type``/``fact_value``); ``narrative`` is the synthesized
    deal story; ``previous_score`` is the last stored score the agent updates.
    """

    opportunity_id: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    narrative: str | None = None
    previous_score: float | None = None
    mode: str = "single"  # ∈ RISK_MODES


@runtime_checkable
class RiskAgent(Protocol):
    async def run(self, request: RiskAssessmentRequest) -> RiskAssessment: ...


class MockRiskAgent:
    """Stand-in risk agent; re-scores from the facts + narrative it is handed."""

    async def run(self, request: RiskAssessmentRequest) -> RiskAssessment:
        factors: list[RiskFactor] = []
        for fact in request.facts:
            fact_type = (fact.get("fact_type") or "").lower()
            factor_type = _FACT_TYPE_TO_FACTOR.get(fact_type)
            if factor_type is None:
                continue
            value = fact.get("fact_value") or fact.get("content") or ""
            sentiment = (fact.get("sentiment") or "").lower()
            factors.append(
                RiskFactor(
                    factor_type=factor_type,
                    description=f"{fact_type}: {value[:120]}" if value else fact_type,
                    severity="high" if sentiment == "negative" else "medium",
                )
            )

        # Start from the previously-stored score; each new risk-bearing fact
        # nudges it up. (The real agent replaces this with its own model.)
        base = request.previous_score if request.previous_score is not None else 0.2
        bump = min(len(factors) * 0.08, 0.4)
        score = round(min(base + bump, 1.0), 4)

        return RiskAssessment(
            opportunity_id=request.opportunity_id,
            risk_score=score,
            factors=factors or [
                RiskFactor(
                    factor_type="engagement_gap",
                    description="No new risk-bearing facts in this email.",
                    severity="low",
                )
            ],
            previous_score=request.previous_score,
            assessed_at=datetime.now(timezone.utc).isoformat(),
            metadata={"facts_considered": len(request.facts), "mode": request.mode},
        )


async def run_risk_assessment(
    request: RiskAssessmentRequest, *, agent: RiskAgent | None = None
) -> RiskAssessment:
    return await (agent or MockRiskAgent()).run(request)


async def mock_run_risk_assessment(request: RiskAssessmentRequest) -> RiskAssessment:
    return await MockRiskAgent().run(request)


__all__ = [
    "RiskFactor",
    "RiskAssessment",
    "RiskAssessmentRequest",
    "RiskAgent",
    "MockRiskAgent",
    "RISK_FACTOR_TYPES",
    "RISK_MODES",
    "SEVERITY_LEVELS",
    "run_risk_assessment",
    "mock_run_risk_assessment",
]
