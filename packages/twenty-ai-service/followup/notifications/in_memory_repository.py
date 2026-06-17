from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from followup.agents.risk.schemas import Notification, NotificationStatus
from followup.notifications.repository import NotificationRepository


class InMemoryNotificationRepository:
    # Development-only in-memory notification store.

    def __init__(self) -> None:
        self._notifications: dict[str, Notification] = {}

    async def list_for_opportunity(
        self,
        *,
        opportunity_id: str,
        user_id: str,
    ) -> list[Notification]:
        return [
            notification.model_copy()
            for notification in self._notifications.values()
            if notification.opportunity_id == opportunity_id
            and notification.user_id == user_id
        ]

    async def save_many(
        self,
        notifications: list[Notification],
    ) -> list[Notification]:
        saved: list[Notification] = []
        for notification in notifications:
            stored = notification.model_copy()
            if stored.id is None:
                stored.id = str(uuid4())
            self._notifications[stored.id] = stored
            saved.append(stored.model_copy())
        return saved

    async def update_status(
        self,
        *,
        notification_id: str,
        status: NotificationStatus,
        now: datetime | None = None,
    ) -> Notification:
        notification = self._notifications[notification_id]
        updated = notification.model_copy(update={"status": status})
        if status == "dismissed":
            dismissed_at = now or datetime.now(timezone.utc)
            if dismissed_at.tzinfo is None:
                dismissed_at = dismissed_at.replace(tzinfo=timezone.utc)
            updated = updated.model_copy(update={"dismissed_at": dismissed_at})
        self._notifications[notification_id] = updated
        return updated.model_copy()

    def count(self) -> int:
        return len(self._notifications)
