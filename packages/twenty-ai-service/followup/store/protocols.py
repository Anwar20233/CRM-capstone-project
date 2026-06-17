from typing import Protocol

from followup.agents.risk.schemas import (
    Notification,
    NotificationStatus,
    RiskScore,
    RiskScoreSnapshot,
)


class FollowUpStoreProtocol(Protocol):
    async def list_notifications_for_opportunity(
        self,
        opportunity_id: str,
        user_id: str,
    ) -> list[Notification]:
        ...

    async def save_risk_score(
        self,
        run_id: str,
        opportunity_id: str,
        risk_score: RiskScore,
    ) -> str:
        ...

    async def save_notification(
        self,
        run_id: str,
        notification: Notification,
    ) -> str:
        ...

    async def save_risk_score_snapshot(
        self,
        snapshot: RiskScoreSnapshot,
    ) -> str:
        ...

    async def get_latest_risk_score_snapshot(
        self,
        opportunity_id: str,
        workspace_id: str,
    ) -> RiskScoreSnapshot | None:
        ...

    async def update_notification_status(
        self,
        notification_id: str,
        status: NotificationStatus,
    ) -> Notification:
        ...
