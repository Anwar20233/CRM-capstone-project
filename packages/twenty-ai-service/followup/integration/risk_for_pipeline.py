"""P1 orchestrator helpers for the Risk Notification Agent."""

from datetime import datetime, timezone
from typing import Awaitable, Callable

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.notifications import LLMCopyGenerator
from followup.agents.risk.schemas import Notification, RiskNotificationAgentResult
from followup.context.loader import load_deal_context
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.notifications.repository import NotificationRepository

# Event types where EVENT_AGENT_ROUTING includes "risk" (see spec §5.3).
EVENT_TYPES_WITH_RISK_AGENT: frozenset[FollowUpEventType] = frozenset(
    {
        FollowUpEventType.OPPORTUNITY_CREATED,
        FollowUpEventType.OPPORTUNITY_UPDATED,
        FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
        FollowUpEventType.EMAIL_SENT,
        FollowUpEventType.PROPOSAL_SENT,
        FollowUpEventType.TASK_COMPLETED,
        FollowUpEventType.ACTIVITY_LOGGED,
        FollowUpEventType.DAILY_RISK_SWEEP,
    },
)

ListExistingNotifications = Callable[
    [str, str],
    Awaitable[list[Notification]],
]

SaveNotifications = Callable[
    [list[Notification]],
    Awaitable[list[Notification]],
]


def _evaluation_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


async def run_risk_agent_for_pipeline(
    event: FollowUpEvent,
    list_existing_notifications: ListExistingNotifications,
    *,
    context: DealContext | None = None,
    save_notifications: SaveNotifications | None = None,
    notification_repository: NotificationRepository | None = None,
    use_llm_context: bool = True,
    llm_generator: LLMCopyGenerator | None = None,
    now: datetime | None = None,
) -> RiskNotificationAgentResult:
    deal_context = context
    if deal_context is None:
        deal_context = await load_deal_context(
            event.opportunity_id,
            event.workspace_id,
            event.user_id,
            use_llm=use_llm_context,
        )

    existing_notifications = await list_existing_notifications(
        event.opportunity_id,
        event.user_id,
    )

    result = await run_risk_notification_agent(
        deal_context,
        event,
        existing_notifications,
        now=_evaluation_now(now),
        llm_generator=llm_generator,
    )

    if result.notifications:
        if save_notifications is not None:
            await save_notifications(result.notifications)
        elif notification_repository is not None:
            await notification_repository.save_many(result.notifications)

    return result
