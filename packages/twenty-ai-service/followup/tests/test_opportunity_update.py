"""Tests for opportunity-update grounding (stage + close date).

These cover the two bugs this module fixes: a close-date push must NOT be
silently treated as a stage edit, and a proposed stage must be grounded against
the real pipeline (never invented). Every failure path must carry a reason.
"""

from __future__ import annotations

import pytest

from followup.crm.opportunity_update import (
    build_update_args,
    canonical_field,
    kind_for_field,
    normalize_close_date,
    normalize_stage,
    resolve_change,
)

STAGES = [
    {"value": "NEW", "label": "New", "position": 0},
    {"value": "SCREENING", "label": "Screening", "position": 1},
    {"value": "MEETING", "label": "Meeting", "position": 2},
    {"value": "PROPOSAL", "label": "Proposal", "position": 3},
    {"value": "CLOSED_WON", "label": "Closed Won", "position": 4},
]


async def _stages():
    return STAGES


async def _no_stages():
    return []


# ---------------------------------------------------------------------------
# field / kind routing
# ---------------------------------------------------------------------------


def test_canonical_field_aliases() -> None:
    assert canonical_field("stage") == "stage"
    assert canonical_field("Deal_Stage") == "stage"
    assert canonical_field("close_date") == "closeDate"
    assert canonical_field("closeDate") == "closeDate"
    assert canonical_field("amount") is None  # unsupported -> None


def test_kind_for_field_splits_stage_from_other() -> None:
    assert kind_for_field("stage") == "update_stage"
    assert kind_for_field("closeDate") == "update_opportunity"
    assert kind_for_field(None) == "update_opportunity"


# ---------------------------------------------------------------------------
# stage normalization
# ---------------------------------------------------------------------------


def test_stage_exact_and_label_and_punctuation() -> None:
    assert normalize_stage("PROPOSAL", STAGES) == ("PROPOSAL", None)
    assert normalize_stage("Closed Won", STAGES) == ("CLOSED_WON", None)
    assert normalize_stage("closed_won", STAGES) == ("CLOSED_WON", None)


def test_stage_invalid_names_the_options() -> None:
    value, reason = normalize_stage("Negotiation", STAGES)
    assert value is None
    assert "not a valid stage" in reason
    assert "Proposal (PROPOSAL)" in reason


def test_stage_advance_picks_next_by_position() -> None:
    assert normalize_stage("advance", STAGES, current_value="SCREENING", intent="move forward") == (
        "MEETING",
        None,
    )


def test_stage_advance_at_final_stage_is_invalid() -> None:
    value, reason = normalize_stage("advance to next stage", STAGES, current_value="CLOSED_WON")
    assert value is None
    assert "final stage" in reason


def test_stage_without_metadata_returns_reason() -> None:
    value, reason = normalize_stage("", STAGES)
    assert value is None
    assert "no target stage" in reason


# ---------------------------------------------------------------------------
# close date normalization
# ---------------------------------------------------------------------------


def test_close_date_parses_common_formats() -> None:
    assert normalize_close_date("2026-07-01") == ("2026-07-01", None)
    assert normalize_close_date("07/01/2026") == ("2026-07-01", None)
    assert normalize_close_date("July 1 2026") == ("2026-07-01", None)


def test_close_date_unparseable_has_reason() -> None:
    value, reason = normalize_close_date("sometime soon")
    assert value is None
    assert "not a recognizable date" in reason


# ---------------------------------------------------------------------------
# resolve_change — the end-to-end grounding used by plan + accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_stage_change_is_grounded() -> None:
    change = await resolve_change(
        field="stage", value="Closed Won", current_stage="PROPOSAL", stages_provider=_stages
    )
    assert change["valid"] is True
    assert change["field"] == "stage"
    assert change["value"] == "CLOSED_WON"
    assert "Closed Won" in change["display"]


@pytest.mark.asyncio
async def test_resolve_close_date_is_not_treated_as_stage() -> None:
    # The original bug: a date push collapsed into a stage edit and did nothing.
    change = await resolve_change(
        field="closeDate", value="2026-07-01", stages_provider=_no_stages
    )
    assert change["valid"] is True
    assert change["field"] == "closeDate"
    assert change["value"] == "2026-07-01"


@pytest.mark.asyncio
async def test_resolve_invented_stage_is_invalid_with_reason() -> None:
    change = await resolve_change(
        field="stage", value="Negotiation", current_stage="MEETING", stages_provider=_stages
    )
    assert change["valid"] is False
    assert change["reason"]
    assert "Negotiation" in change["reason"]


@pytest.mark.asyncio
async def test_resolve_unsupported_field_is_invalid() -> None:
    change = await resolve_change(field="amount", value="5000", stages_provider=_stages)
    assert change["valid"] is False
    assert "not supported" in change["reason"]


@pytest.mark.asyncio
async def test_resolve_stage_without_metadata_fails_when_no_pipeline() -> None:
    change = await resolve_change(field="stage", value="PROPOSAL", stages_provider=_no_stages)
    assert change["valid"] is False
    assert "pipeline" in change["reason"]


def test_build_update_args_shapes_bridge_call() -> None:
    assert build_update_args("opp-1", {"field": "stage", "value": "PROPOSAL"}) == {
        "id": "opp-1",
        "stage": "PROPOSAL",
    }
    assert build_update_args("opp-1", {"field": "closeDate", "value": "2026-07-01"}) == {
        "id": "opp-1",
        "closeDate": "2026-07-01",
    }


# ---------------------------------------------------------------------------
# Adapter mapping — a date push must NOT collapse into a stage edit
# ---------------------------------------------------------------------------


def _recommendation(field: str, value: str):
    from followup.next_step.agents.next_step.schemas import (
        OrchestratorAction,
        RecommendedAction,
    )

    return RecommendedAction(
        action_type="update_opportunity",
        title="Update the deal",
        description="Update the opportunity",
        priority=2,
        reasoning="because",
        evidence=["a fact"],
        orchestrator_action=OrchestratorAction(
            tool="update_opportunity",
            instruction="update opp",
            params={"opportunity_id": "opp-1", "field": field, "value": value},
        ),
    )


def test_adapter_routes_stage_to_update_stage_kind() -> None:
    from followup.agents.next_step_adapter import _step_from_recommendation

    step = _step_from_recommendation(_recommendation("stage", "PROPOSAL"))
    assert step.kind == "update_stage"
    assert step.metadata["change"] == {"field": "stage", "value": "PROPOSAL"}


def test_adapter_routes_close_date_to_update_opportunity_kind() -> None:
    from followup.agents.next_step_adapter import _step_from_recommendation

    step = _step_from_recommendation(_recommendation("closeDate", "2026-07-01"))
    # The original bug: this used to become update_stage and do nothing.
    assert step.kind == "update_opportunity"
    assert step.metadata["change"] == {"field": "closeDate", "value": "2026-07-01"}


# ---------------------------------------------------------------------------
# Prep task — grounds the plan's update steps against the real pipeline
# ---------------------------------------------------------------------------


def _deal_context():
    from followup.profile.schemas import DealContext

    return DealContext(
        opportunity_id="opp-1",
        opportunity_name="Acme",
        deal_stage="MEETING",
        deal_value=1000.0,
        company_name="Acme",
        profile_narrative="",
        contacts=[],
        recent_activities=[],
        key_relationships=[],
        open_concerns=[],
        risk_score=None,
        close_date="2026-01-01",
    )


@pytest.mark.asyncio
async def test_prep_task_grounds_stage_and_date_steps(monkeypatch) -> None:
    from types import SimpleNamespace

    from followup.contracts.next_step import NextStepPlan, PlannedStep
    from followup.orchestrator.tasks import build_default_task_registry
    from followup.orchestrator.tasks import TaskContext
    import followup.crm.opportunity_update as opp_mod

    monkeypatch.setattr(opp_mod, "fetch_pipeline_stages", _stages)

    plan = NextStepPlan(
        steps=[
            PlannedStep(kind="update_stage", intent="advance", metadata={"change": {"field": "stage", "value": "Proposal"}}),
            PlannedStep(kind="update_opportunity", intent="push date", metadata={"change": {"field": "closeDate", "value": "2026-09-01"}}),
        ],
        headline_action="close_deal",
        summary="",
    )
    registry = build_default_task_registry(SimpleNamespace())
    spec = registry.get("validate_opportunity_change")
    ctx = TaskContext(state={}, deal_context=_deal_context(), plan=plan, instructions="")

    out = await spec.handler(ctx)
    results = out["task_results"]
    assert results["update_stage"] == {
        "field": "stage",
        "value": "PROPOSAL",
        "valid": True,
        "reason": None,
        "display": "Move stage to Proposal",
        "current_value": "MEETING",
    }
    assert results["update_opportunity"]["valid"] is True
    assert results["update_opportunity"]["field"] == "closeDate"
    assert results["update_opportunity"]["value"] == "2026-09-01"


@pytest.mark.asyncio
async def test_prep_task_flags_invented_stage_with_reason(monkeypatch) -> None:
    from types import SimpleNamespace

    from followup.contracts.next_step import NextStepPlan, PlannedStep
    from followup.orchestrator.tasks import build_default_task_registry, TaskContext
    import followup.crm.opportunity_update as opp_mod

    monkeypatch.setattr(opp_mod, "fetch_pipeline_stages", _stages)

    plan = NextStepPlan(
        steps=[PlannedStep(kind="update_stage", intent="negotiate", metadata={"change": {"field": "stage", "value": "Negotiation"}})],
        headline_action="close_deal",
        summary="",
    )
    registry = build_default_task_registry(SimpleNamespace())
    spec = registry.get("validate_opportunity_change")
    ctx = TaskContext(state={}, deal_context=_deal_context(), plan=plan, instructions="")

    out = await spec.handler(ctx)
    change = out["task_results"]["update_stage"]
    assert change["valid"] is False
    assert "Negotiation" in change["reason"]


# ---------------------------------------------------------------------------
# Executor — a grounded close-date push actually writes closeDate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_writes_close_date_push() -> None:
    from types import SimpleNamespace

    from followup.api.execution import FollowupActionExecutor

    executor = FollowupActionExecutor()
    writes: list[tuple[str, dict]] = []

    async def _fake_direct_write(tool: str, args: dict) -> dict:
        writes.append((tool, args))
        return {"id": "opp-1"}

    executor._direct_write = _fake_direct_write  # type: ignore[assignment]
    action = SimpleNamespace(
        opportunity_id="opp-1",
        action_payload={
            "task_results": {
                "update_opportunity": {
                    "field": "closeDate",
                    "value": "2026-09-01",
                    "valid": True,
                }
            }
        },
        reasoning="",
    )

    status, error = await executor._execute_opportunity_update(
        "update_opportunity", {"kind": "update_opportunity", "intent": "push date"}, action
    )
    assert status == "completed"
    assert error is None
    assert writes == [("update_opportunity", {"id": "opp-1", "closeDate": "2026-09-01"})]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
