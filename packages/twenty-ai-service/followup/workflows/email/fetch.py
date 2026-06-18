"""Phase 1 — fetch inbound emails into the queue (no LLM)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from followup.store.repositories import InboundEmailRepository
from followup.workflows.email.bridge_reads import fetch_inbound_messages

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    fetched: int
    enqueued: int
    skipped_duplicate: int


async def fetch_inbound_emails(
    repo: InboundEmailRepository,
    *,
    workspace_id: str,
    since: Optional[datetime] = None,
) -> FetchResult:
    workspace_uuid = uuid.UUID(workspace_id)
    cursor = since
    if cursor is None:
        cursor = await repo.get_latest_received_at(workspace_uuid)

    messages = await fetch_inbound_messages(since=cursor)
    enqueued = 0
    skipped = 0

    for message in messages:
        row = await repo.enqueue(
            {
                "workspace_id": workspace_uuid,
                "message_id": message["message_id"],
                "sender_email": message["sender_email"],
                "subject": message["subject"],
                "body": message["body"],
                "received_at": message["received_at"],
            }
        )
        if row is None:
            skipped += 1
        else:
            enqueued += 1

    logger.info(
        "email fetch workspace=%s fetched=%s enqueued=%s duplicates=%s",
        workspace_id,
        len(messages),
        enqueued,
        skipped,
    )
    return FetchResult(
        fetched=len(messages),
        enqueued=enqueued,
        skipped_duplicate=skipped,
    )
