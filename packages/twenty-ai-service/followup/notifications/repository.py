from __future__ import annotations

from typing import Protocol

from followup.agents.risk.schemas import Notification, NotificationStatus


class NotificationRepository(Protocol):
    async def list_for_opportunity(
        self,
        *,
        opportunity_id: str,
        user_id: str,
    ) -> list[Notification]:
        ...

    async def save_many(
        self,
        notifications: list[Notification],
    ) -> list[Notification]:
        ...

    async def update_status(
        self,
        *,
        notification_id: str,
        status: NotificationStatus,
    ) -> Notification:
        ...
