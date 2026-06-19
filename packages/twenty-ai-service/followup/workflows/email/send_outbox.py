"""Hourly outbox poller — send accepted draft emails idempotently."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from followup.api.execution import email_already_sent, send_drafted_email
from followup.store.repositories import PendingActionRepository

logger = logging.getLogger(__name__)


@dataclass
class SendOutboxResult:
    claimed: int
    sent: int
    skipped: int
    failed: int


async def send_outbox_batch(
    repo: PendingActionRepository,
    *,
    workspace_id: str,
    batch_size: int = 20,
) -> SendOutboxResult:
    workspace_uuid = uuid.UUID(workspace_id)
    actions = await repo.list_accepted_for_outbox(workspace_uuid, batch_size=batch_size)

    sent = 0
    skipped = 0
    failed = 0

    for action in actions:
        if email_already_sent(action):
            skipped += 1
            continue

        result = await send_drafted_email(action)
        status = result.get("status")

        if status == "sent":
            payload = dict(action.action_payload or {})
            payload["email_sent_at"] = datetime.now(timezone.utc).isoformat()
            action.action_payload = payload
            action.execution_status = "completed"
            action.executed_at = datetime.now(timezone.utc)
            await repo.save(action)
            sent += 1
        elif status == "skipped":
            skipped += 1
        else:
            action.execution_status = "failed"
            action.execution_error = result.get("error") or "send failed"
            await repo.save(action)
            failed += 1

    logger.info(
        "email outbox workspace=%s claimed=%s sent=%s skipped=%s failed=%s",
        workspace_id,
        len(actions),
        sent,
        skipped,
        failed,
    )
    return SendOutboxResult(
        claimed=len(actions),
        sent=sent,
        skipped=skipped,
        failed=failed,
    )
