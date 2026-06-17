"""Agent contracts for the Follow-Up Intelligence pipeline (Steps 2-4 agent layer).

Public surface:

* ``EmailSignalEvent`` / ``SIGNAL_TYPES`` — inbound trigger (events.py).
* ``NextStepRequest`` / ``NextStepPlan`` / ``PlannedStep`` / ``NextStepAgent`` /
  ``MockNextStepAgent`` — P2 next-step planner (next_step.py).
* ``RiskAssessmentRequest`` / ``RiskFactor`` / ``RiskAssessment`` /
  ``RiskAgent`` / ``MockRiskAgent`` — P3 risk scoring (risk.py).
* ``DraftRequest`` / ``DraftResult`` / ``DraftingAgent`` / ``MockDraftingAgent``
  — P4 email drafting (drafting.py).
* ``AgentBundle`` — dependency bundle analogous to PipelineDeps; swap in real
  agents by replacing the default mock fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from followup.contracts.drafting import (
    DRAFT_MODES,
    DRAFT_TONE_TYPES,
    DraftingAgent,
    DraftRequest,
    DraftResult,
    MockDraftingAgent,
    mock_run_draft,
    run_draft,
)
from followup.contracts.events import EmailSignalEvent, SIGNAL_TYPES
from followup.contracts.next_step import (
    MockNextStepAgent,
    NEXT_STEP_MODES,
    NEXT_STEP_TYPES,
    NextStepAgent,
    NextStepPlan,
    NextStepRequest,
    PlannedStep,
    PRIORITY_LEVELS,
    STEP_KINDS,
    mock_run_next_step,
    run_next_step,
)
from followup.contracts.risk import (
    MockRiskAgent,
    RISK_FACTOR_TYPES,
    RISK_MODES,
    RiskAgent,
    RiskAssessment,
    RiskAssessmentRequest,
    RiskFactor,
    SEVERITY_LEVELS,
    mock_run_risk_assessment,
    run_risk_assessment,
)


@dataclass
class AgentBundle:
    """Dependency bundle for the orchestrator, analogous to PipelineDeps.

    Each field defaults to the mock stand-in. Real agent implementations are
    swapped in by replacing the field — no orchestrator changes needed.
    """

    next_step: NextStepAgent = field(default_factory=MockNextStepAgent)
    risk: RiskAgent = field(default_factory=MockRiskAgent)
    drafting: DraftingAgent = field(default_factory=MockDraftingAgent)


__all__ = [
    # events
    "EmailSignalEvent",
    "SIGNAL_TYPES",
    # next_step
    "NextStepPlan",
    "PlannedStep",
    "NextStepRequest",
    "NextStepAgent",
    "MockNextStepAgent",
    "NEXT_STEP_TYPES",
    "STEP_KINDS",
    "NEXT_STEP_MODES",
    "PRIORITY_LEVELS",
    "run_next_step",
    "mock_run_next_step",
    # risk
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
    # drafting
    "DraftRequest",
    "DraftResult",
    "DraftingAgent",
    "MockDraftingAgent",
    "DRAFT_TONE_TYPES",
    "DRAFT_MODES",
    "run_draft",
    "mock_run_draft",
    # bundle
    "AgentBundle",
]
