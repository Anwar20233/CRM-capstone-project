from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.evaluation import RuleEvaluation, evaluate_all_rules
from followup.agents.risk.notifications import template_notification_copy
from followup.agents.risk.schemas import Notification, RiskFactor, RiskScore
from followup.context.errors import ContextLoadError
from followup.context.loader import load_deal_context
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline
from followup.notifications.in_memory_repository import InMemoryNotificationRepository

router = APIRouter(prefix="/followup", tags=["followup"])

_DEV_REPOSITORIES: dict[str, InMemoryNotificationRepository] = {}


class RiskEvaluateResponse(BaseModel):
    opportunity_id: str
    risk_score: RiskScore
    factors: list[RiskFactor] = Field(default_factory=list)
    rule_evaluations: list[RuleEvaluation] = Field(default_factory=list)
    notifications: list[Notification] = Field(default_factory=list)
    reasoning_summary: str
    persisted: bool = False


def _map_context_error(error: ContextLoadError) -> HTTPException:
    status_by_code = {
        "OPPORTUNITY_NOT_FOUND": 404,
        "RECORD_NOT_FOUND": 404,
        "BRIDGE_UNREACHABLE": 503,
        "BRIDGE_ERROR": 503,
        "BRIDGE_TOOL_ERROR": 503,
        "MAP_FAILED": 500,
        "INVALID_IDENTITY": 400,
    }
    status_code = status_by_code.get(error.code, 500)
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.message},
    )


def _build_event(
    opportunity_id: str,
    workspace_id: str,
    user_id: str,
) -> FollowUpEvent:
    return FollowUpEvent(
        event_id=f"api-risk-{opportunity_id}",
        idempotency_key=f"{workspace_id}:risk-evaluate:{opportunity_id}",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        occurred_at=datetime.now(timezone.utc),
    )


@router.post("/risk/{opportunity_id}/evaluate", response_model=RiskEvaluateResponse)
async def evaluate_opportunity_risk(
    opportunity_id: str,
    workspace_id: str = Query(...),
    user_id: str = Query(...),
    role_id: str | None = Query(default=None),
    use_llm_context: bool = Query(default=False),
    use_llm_copy: bool = Query(default=False),
    persist_notifications: bool = Query(default=False),
) -> RiskEvaluateResponse:
    try:
        context = await load_deal_context(
            opportunity_id,
            workspace_id,
            user_id,
            role_id=role_id,
            use_llm=use_llm_context,
        )
    except ContextLoadError as error:
        raise _map_context_error(error) from error

    evaluation_time = context.loaded_at or datetime.now(timezone.utc)
    event = _build_event(opportunity_id, workspace_id, user_id)
    repository: InMemoryNotificationRepository | None = None

    async def list_existing(opportunity: str, owner_id: str) -> list[Notification]:
        if repository is None:
            return []
        return await repository.list_for_opportunity(
            opportunity_id=opportunity,
            user_id=owner_id,
        )

    if persist_notifications:
        repository = _DEV_REPOSITORIES.setdefault(
            workspace_id,
            InMemoryNotificationRepository(),
        )

    llm_generator = None
    if not use_llm_copy:
        async def template_only(draft, deal_context, risk_score=None):
            return await template_notification_copy(
                draft,
                deal_context,
                risk_score=risk_score,
            )

        llm_generator = template_only

    try:
        result = await run_risk_agent_for_pipeline(
            event,
            list_existing,
            context=context,
            notification_repository=repository,
            use_llm_context=False,
            llm_generator=llm_generator,
            now=evaluation_time,
        )
    except ContextLoadError as error:
        raise _map_context_error(error) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail={"code": "RISK_EVALUATION_FAILED", "message": str(error)},
        ) from error

    evaluations = evaluate_all_rules(context, now=evaluation_time)
    return RiskEvaluateResponse(
        opportunity_id=opportunity_id,
        risk_score=result.risk_score,
        factors=result.risk_score.factors,
        rule_evaluations=evaluations,
        notifications=result.notifications,
        reasoning_summary=result.reasoning_summary,
        persisted=persist_notifications and bool(result.notifications),
    )
