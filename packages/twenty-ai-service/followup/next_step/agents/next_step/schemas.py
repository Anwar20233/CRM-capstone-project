"""Schemas for the Next Step Intelligence Agent (Person 2).

All types are Pydantic v2.

Public types (cross the agent boundary to the Orchestrator):
    OrchestratorAction, RecommendedAction, NextStepAgentResult

Internal types (consumed only inside the agent module):
    NextStepLLMActionItem, NextStepLLMOutput
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

_VALID_ORCHESTRATOR_TOOLS: frozenset[str] = frozenset(
    {
        "create_task",
        "schedule_meeting",
        "send_email",
        "update_opportunity",
        "log_activity",
        "create_reminder",
    }
)

# When the model fills action_type but omits orchestrator_tool, map common labels.
_ACTION_TYPE_TO_TOOL: dict[str, str] = {
    "send_email": "send_email",
    "send_proposal": "send_email",
    "follow_up_call": "send_email",
    "check_in": "send_email",
    "close_deal": "update_opportunity",
    "escalate": "create_task",
    "schedule_meeting": "schedule_meeting",
    "book_meeting": "schedule_meeting",
    "schedule_demo": "schedule_meeting",
    "create_task": "create_task",
    "log_activity": "log_activity",
    "create_reminder": "create_reminder",
    "update_opportunity": "update_opportunity",
    "update_stage": "update_opportunity",
}


# ---------------------------------------------------------------------------
# Public types — returned to the Orchestrator
# ---------------------------------------------------------------------------


class OrchestratorAction(BaseModel):
    """A concrete action the Orchestrator may choose to execute."""

    tool: str = Field(
        description=(
            "Orchestrator tool name. One of: create_task, schedule_meeting, "
            "send_email, update_opportunity, log_activity, create_reminder."
        )
    )
    instruction: str = Field(
        description="Natural-language instruction for the Orchestrator, referencing the opportunity id."
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Structured key-value parameters passed to the tool by the Orchestrator.",
    )


class RecommendedAction(BaseModel):
    """A single recommended next step, ready for Orchestrator consumption."""

    action_type: str = Field(description="Short machine-readable action category")
    title: str = Field(description="Human-readable title shown to the sales rep")
    description: str = Field(description="What to do and why, in plain language")
    priority: int = Field(ge=1, le=5, description="1 = highest urgency, 5 = lowest")
    reasoning: str = Field(description="Why this action matters now, grounded in deal facts")
    evidence: list[str] = Field(
        default_factory=list,
        description="Specific deal facts (timeline items, metrics, contacts) supporting this action",
    )
    profile_fact_refs: list[str] = Field(
        default_factory=list,
        description="IDs of active profile facts referenced by this action",
    )
    orchestrator_action: OrchestratorAction = Field(
        description="Executable action the Orchestrator may perform on behalf of this recommendation"
    )


class NextStepAgentResult(BaseModel):
    """Complete output returned by run_next_step_agent to the Orchestrator."""

    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    summary_reasoning: str = Field(
        default="",
        description="Overall reasoning narrative across all recommendations",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    skipped: bool = Field(default=False)
    skip_reason: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Internal LLM output types — never exposed outside the agent module
# ---------------------------------------------------------------------------


class NextStepLLMActionItem(BaseModel):
    """Single action item as returned by the structured LLM call."""

    action_type: str
    title: str
    description: str
    priority: int = Field(ge=1, le=5)
    reasoning: str
    evidence: list[str] = Field(default_factory=list)
    profile_fact_refs: list[str] = Field(default_factory=list)
    # Optional in the LLM JSON schema; normalized before field validation.
    orchestrator_tool: str | None = Field(
        default=None,
        description="Orchestrator tool to invoke (e.g. 'create_task', 'schedule_meeting')",
    )
    orchestrator_instruction: str | None = Field(
        default=None,
        description="Instruction for the Orchestrator tool, must reference the opportunity id",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_partial_action(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if not data.get("evidence"):
            data["evidence"] = ["No specific evidence cited"]
        action_key = str(data.get("action_type") or "").lower().strip()
        tool = str(data.get("orchestrator_tool") or "").strip().lower()
        if not tool or tool not in _VALID_ORCHESTRATOR_TOOLS:
            tool = _ACTION_TYPE_TO_TOOL.get(action_key, action_key)
        if tool not in _VALID_ORCHESTRATOR_TOOLS:
            tool = "send_email"
        data["orchestrator_tool"] = tool
        instruction = str(data.get("orchestrator_instruction") or "").strip()
        if not instruction:
            instruction = str(
                data.get("description") or data.get("title") or "Execute recommended action"
            ).strip()
        data["orchestrator_instruction"] = instruction
        return data


class NextStepLLMOutput(BaseModel):
    """Structured output returned by the LLM — validated before scoring."""

    actions: list[NextStepLLMActionItem] = Field(min_length=1, max_length=5)
    summary_reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)
