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
from typing import Any


def _default_next_step_agent() -> Any:
    from followup.contracts.next_step import MockNextStepAgent

    return MockNextStepAgent()


def _default_risk_agent() -> Any:
    from followup.contracts.risk import MockRiskAgent

    return MockRiskAgent()


def _default_drafting_agent() -> Any:
    from followup.contracts.drafting import MockDraftingAgent

    return MockDraftingAgent()


@dataclass
class AgentBundle:
    """Dependency bundle for the orchestrator, analogous to PipelineDeps.

    Each field defaults to the mock stand-in. Real agent implementations are
    swapped in by replacing the field — no orchestrator changes needed.
    """

    next_step: Any = field(default_factory=_default_next_step_agent)
    risk: Any = field(default_factory=_default_risk_agent)
    drafting: Any = field(default_factory=_default_drafting_agent)


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
    "RiskDealContext",
    "RiskAgent",
    "DatabaseRiskAgent",
    "MockRiskAgent",
    "RISK_FACTOR_TYPES",
    "RISK_LEVELS",
    "RISK_MODES",
    "SEVERITY_LEVELS",
    "build_risk_deal_context_from_db",
    "evaluate_deal_risk",
    "evaluate_risk_context",
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

_EXPORT_MODULES = {
    "EmailSignalEvent": "followup.contracts.events",
    "SIGNAL_TYPES": "followup.contracts.events",
    "NextStepPlan": "followup.contracts.next_step",
    "PlannedStep": "followup.contracts.next_step",
    "NextStepRequest": "followup.contracts.next_step",
    "NextStepAgent": "followup.contracts.next_step",
    "MockNextStepAgent": "followup.contracts.next_step",
    "NEXT_STEP_TYPES": "followup.contracts.next_step",
    "STEP_KINDS": "followup.contracts.next_step",
    "NEXT_STEP_MODES": "followup.contracts.next_step",
    "PRIORITY_LEVELS": "followup.contracts.next_step",
    "run_next_step": "followup.contracts.next_step",
    "mock_run_next_step": "followup.contracts.next_step",
    "RiskFactor": "followup.contracts.risk",
    "RiskAssessment": "followup.contracts.risk",
    "RiskAssessmentRequest": "followup.contracts.risk",
    "RiskDealContext": "followup.contracts.risk",
    "RiskAgent": "followup.contracts.risk",
    "DatabaseRiskAgent": "followup.contracts.risk",
    "MockRiskAgent": "followup.contracts.risk",
    "RISK_FACTOR_TYPES": "followup.contracts.risk",
    "RISK_LEVELS": "followup.contracts.risk",
    "RISK_MODES": "followup.contracts.risk",
    "SEVERITY_LEVELS": "followup.contracts.risk",
    "build_risk_deal_context_from_db": "followup.contracts.risk",
    "evaluate_deal_risk": "followup.contracts.risk",
    "evaluate_risk_context": "followup.contracts.risk",
    "run_risk_assessment": "followup.contracts.risk",
    "mock_run_risk_assessment": "followup.contracts.risk",
    "DraftRequest": "followup.contracts.drafting",
    "DraftResult": "followup.contracts.drafting",
    "DraftingAgent": "followup.contracts.drafting",
    "MockDraftingAgent": "followup.contracts.drafting",
    "DRAFT_TONE_TYPES": "followup.contracts.drafting",
    "DRAFT_MODES": "followup.contracts.drafting",
    "run_draft": "followup.contracts.drafting",
    "mock_run_draft": "followup.contracts.drafting",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(module_name)
    return getattr(module, name)
