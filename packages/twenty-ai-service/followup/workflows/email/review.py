"""Phase 2 — review queued emails and run the follow-up pipeline."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from followup.store.repositories import InboundEmail, InboundEmailRepository

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    claimed: int
    processed: int
    skipped: int
    failed: int


async def _run_email_pipeline(
    graph: Any,
    *,
    workspace_id: str,
    email: InboundEmail,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    initial_state = {
        "entry_point": "email",
        "trigger": {
            "id": email.message_id,
            "sender_email": email.sender_email,
            "subject": email.subject,
            "body": email.body,
        },
        "workspace_id": workspace_id,
        "run_id": run_id,
        "trace": [],
    }
    if email.opportunity_id:
        initial_state["opportunity_id"] = str(email.opportunity_id)

    return await graph.ainvoke(initial_state)


def _skip_reason(result: dict[str, Any]) -> str:
    error = result.get("error") or ""
    if "no deal resolved" in error.lower():
        return error
    if result.get("status") == "completed" and not result.get("pending_action"):
        return "pipeline completed without pending action"
    return error or "pipeline halted"


async def review_pending_emails(
    repo: InboundEmailRepository,
    graph: Any,
    *,
    workspace_id: str,
    batch_size: int = 10,
) -> ReviewResult:
    workspace_uuid = uuid.UUID(workspace_id)
    batch = await repo.claim_batch(workspace_uuid, batch_size=batch_size)

    processed = 0
    skipped = 0
    failed = 0

    for email in batch:
        try:
            result = await _run_email_pipeline(
                graph, workspace_id=workspace_id, email=email
            )
        except Exception as exc:  # noqa: BLE001
            await repo.mark_failed(email.id, error=str(exc))
            failed += 1
            continue

        status = result.get("status", "failed")
        run_id = result.get("run_id")
        pipeline_run_id = uuid.UUID(run_id) if run_id else None

        if status == "completed" and result.get("pending_action"):
            await repo.mark_processed(email.id, pipeline_run_id=pipeline_run_id)
            processed += 1
        elif status == "failed" and "no deal resolved" in (result.get("error") or "").lower():
            await repo.mark_skipped(email.id, reason=_skip_reason(result))
            skipped += 1
        elif status == "failed":
            await repo.mark_failed(email.id, error=result.get("error") or "pipeline failed")
            failed += 1
        else:
            await repo.mark_skipped(email.id, reason=_skip_reason(result))
            skipped += 1

    logger.info(
        "email review workspace=%s claimed=%s processed=%s skipped=%s failed=%s",
        workspace_id,
        len(batch),
        processed,
        skipped,
        failed,
    )
    return ReviewResult(
        claimed=len(batch),
        processed=processed,
        skipped=skipped,
        failed=failed,
    )
