from datetime import datetime, timezone
from typing import Awaitable, Callable

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.notifications import LLMCopyGenerator
from followup.agents.risk.schemas import (
    Notification,
    RiskSweepError,
    RiskSweepOpportunityResult,
    RiskSweepResult,
)
from followup.context.loader import load_deal_context
from followup.context.schemas import DealContext
from followup.context.stage_normalization import is_closed_stage
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline
from followup.notifications.repository import NotificationRepository
from followup.profile.protocols import ProfileServiceProtocol
from followup.store.risk_snapshot_store import RiskSnapshotStore
from followup.workflows.risk_sweep.compare import (
    compare_score_to_previous,
    needs_re_engagement_draft,
)

ActiveOpportunity = dict[str, str]
ListActiveOpportunities = Callable[[str], Awaitable[list[ActiveOpportunity]]]
LoadDealContext = Callable[
    [str, str, str],
    Awaitable[DealContext],
]
ListExistingNotifications = Callable[
    [str, str],
    Awaitable[list[Notification]],
]


def resolve_sweep_authenticated_user_id(opportunity: ActiveOpportunity) -> str:
    return opportunity.get("user_id") or opportunity.get("owner_id", "")


def build_sweep_opportunity_record(
    *,
    opportunity_id: str,
    owner_id: str,
    authenticated_user_id: str,
    stage: str,
    name: str,
) -> ActiveOpportunity:
    return {
        "id": opportunity_id,
        "owner_id": owner_id,
        "user_id": authenticated_user_id,
        "stage": stage,
        "name": name,
    }


async def _apply_risk_profile_facts(
    profile_service: ProfileServiceProtocol | None,
    context: DealContext,
    risk_score_value: int,
    risk_level: str,
    trigger_reference_id: str,
) -> None:
    if profile_service is None:
        return
    profile_id = getattr(context, "profile_id", None)
    if profile_id is None:
        return
    await profile_service.apply_fact_updates(
        profile_id=profile_id,
        candidates=[
            {
                "category": "risk",
                "fact_key": "risk_score",
                "value": str(risk_score_value),
                "confidence": 1.0,
            },
            {
                "category": "risk",
                "fact_key": "risk_level",
                "value": risk_level,
                "confidence": 1.0,
            },
        ],
        trigger="risk_score_update",
        trigger_reference_id=trigger_reference_id,
    )


async def run_daily_risk_sweep(
    workspace_id: str,
    *,
    list_active_opportunities: ListActiveOpportunities,
    load_deal_context_fn: LoadDealContext | None = None,
    snapshot_store: RiskSnapshotStore,
    list_existing_notifications: ListExistingNotifications | None = None,
    notification_repository: NotificationRepository | None = None,
    profile_service: ProfileServiceProtocol | None = None,
    llm_generator: LLMCopyGenerator | None = None,
    use_llm_context: bool = True,
    now: datetime | None = None,
    limit: int | None = None,
) -> RiskSweepResult:
    context_loader = load_deal_context_fn or (
        lambda opportunity_id, workspace, user_id: load_deal_context(
            opportunity_id,
            workspace,
            user_id,
            use_llm=use_llm_context,
        )
    )
    started_at = now or datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    opportunities = await list_active_opportunities(workspace_id)
    active_opportunities = [
        opportunity
        for opportunity in opportunities
        if not is_closed_stage(str(opportunity.get("stage", "")))
    ]
    if limit is not None:
        active_opportunities = active_opportunities[:limit]

    results: list[RiskSweepOpportunityResult] = []
    errors: list[RiskSweepError] = []
    notifications_created = 0
    snapshot_count = 0
    re_engagement_triggers = 0
    succeeded_count = 0
    failed_count = 0

    for opportunity in active_opportunities:
        opportunity_id = opportunity["id"]
        user_id = resolve_sweep_authenticated_user_id(opportunity)
        try:
            context = await context_loader(
                opportunity_id,
                workspace_id,
                user_id,
            )
        except Exception as error:
            failed_count += 1
            errors.append(
                RiskSweepError(
                    opportunity_id=opportunity_id,
                    message=str(error),
                ),
            )
            results.append(
                RiskSweepOpportunityResult(
                    opportunity_id=opportunity_id,
                    risk_score=_empty_risk_score(started_at),
                    snapshot=_empty_snapshot(opportunity_id, workspace_id),
                    skipped=True,
                    skip_reason=str(error),
                ),
            )
            continue

        sweep_event = FollowUpEvent(
            event_id=f"sweep-{opportunity_id}-{started_at.isoformat()}",
            idempotency_key=(
                f"{workspace_id}:daily_risk_sweep:{opportunity_id}:{started_at.date()}"
            ),
            event_type=FollowUpEventType.DAILY_RISK_SWEEP,
            opportunity_id=opportunity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            source="daily_sweep",
            occurred_at=started_at,
        )

        if list_existing_notifications is not None:
            loader = list_existing_notifications
        elif notification_repository is not None:
            async def loader(opportunity: str, owner: str) -> list[Notification]:
                return await notification_repository.list_for_opportunity(
                    opportunity_id=opportunity,
                    user_id=owner,
                )
        else:
            loader = _empty_notifications

        try:
            agent_result = await run_risk_agent_for_pipeline(
                sweep_event,
                loader,
                context=context,
                notification_repository=notification_repository,
                use_llm_context=False,
                llm_generator=llm_generator,
                now=started_at,
            )
            snapshot = await compare_score_to_previous(
                opportunity_id=opportunity_id,
                workspace_id=workspace_id,
                new_score=agent_result.risk_score.score,
                factors=agent_result.risk_score.factors,
                snapshot_store=snapshot_store,
                source="daily_sweep",
                now=started_at,
            )
        except Exception as error:
            failed_count += 1
            errors.append(
                RiskSweepError(
                    opportunity_id=opportunity_id,
                    message=str(error),
                ),
            )
            results.append(
                RiskSweepOpportunityResult(
                    opportunity_id=opportunity_id,
                    risk_score=_empty_risk_score(started_at),
                    snapshot=_empty_snapshot(opportunity_id, workspace_id),
                    skipped=True,
                    skip_reason=str(error),
                ),
            )
            continue

        re_engagement = needs_re_engagement_draft(snapshot)
        if re_engagement:
            re_engagement_triggers += 1
        notifications_created += len(agent_result.notifications)
        snapshot_count += 1
        succeeded_count += 1

        await _apply_risk_profile_facts(
            profile_service,
            context,
            agent_result.risk_score.score,
            agent_result.risk_score.level,
            str(snapshot.id),
        )

        results.append(
            RiskSweepOpportunityResult(
                opportunity_id=opportunity_id,
                risk_score=agent_result.risk_score,
                snapshot=snapshot,
                notifications=agent_result.notifications,
                needs_re_engagement_draft=re_engagement,
            ),
        )

    completed_at = datetime.now(timezone.utc)
    return RiskSweepResult(
        workspace_id=workspace_id,
        started_at=started_at,
        completed_at=completed_at,
        evaluated_count=len(active_opportunities),
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        notifications_created=notifications_created,
        snapshot_count=snapshot_count,
        re_engagement_triggers=re_engagement_triggers,
        opportunities_processed=succeeded_count,
        opportunities_skipped=failed_count,
        results=results,
        errors=errors,
    )


async def _empty_notifications(
    opportunity_id: str,
    user_id: str,
) -> list[Notification]:
    return []


def _empty_risk_score(computed_at: datetime):
    from followup.agents.risk.schemas import RiskScore

    return RiskScore(
        score=0,
        level="low",
        factors=[],
        computed_at=computed_at,
        reasoning_summary="",
    )


def _empty_snapshot(opportunity_id: str, workspace_id: str):
    from followup.agents.risk.schemas import RiskScoreSnapshot

    return RiskScoreSnapshot(
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        score=0,
        level="low",
    )
