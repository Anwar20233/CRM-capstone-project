"""Unit tests for email monitoring workflows (fetch / review / outbox)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from followup.store.repositories import InboundEmail
from followup.workflows.email.fetch import fetch_inbound_emails
from followup.workflows.email.review import review_pending_emails
from followup.workflows.email.send_outbox import send_outbox_batch


class FakeInboundRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.latest_received: datetime | None = None

    async def get_latest_received_at(self, workspace_id: uuid.UUID):
        return self.latest_received

    async def enqueue(self, email_data: dict):
        message_id = email_data["message_id"]
        if message_id in self.rows:
            return None
        row = InboundEmail(
            id=uuid.uuid4(),
            workspace_id=email_data["workspace_id"],
            message_id=message_id,
            sender_email=email_data["sender_email"],
            subject=email_data.get("subject", ""),
            body=email_data.get("body", ""),
            received_at=email_data["received_at"],
        )
        self.rows[message_id] = {"row": row, "status": "pending"}
        return row

    async def claim_batch(self, workspace_id: uuid.UUID, batch_size: int = 10):
        pending = [
            entry["row"]
            for entry in self.rows.values()
            if entry["status"] == "pending"
        ][:batch_size]
        for entry in pending:
            self.rows[entry.message_id]["status"] = "processing"
        return pending

    async def mark_processed(self, email_id: uuid.UUID, *, pipeline_run_id=None):
        for entry in self.rows.values():
            if entry["row"].id == email_id:
                entry["status"] = "processed"

    async def mark_skipped(self, email_id: uuid.UUID, *, reason: str):
        for entry in self.rows.values():
            if entry["row"].id == email_id:
                entry["status"] = "skipped"
                entry["reason"] = reason

    async def mark_failed(self, email_id: uuid.UUID, *, error: str):
        for entry in self.rows.values():
            if entry["row"].id == email_id:
                entry["status"] = "failed"
                entry["error"] = error


def test_fetch_dedupes_message_ids(monkeypatch):
    async def _run():
        repo = FakeInboundRepo()
        now = datetime.now(timezone.utc)
        messages = [
            {
                "message_id": "msg-1",
                "sender_email": "buyer@acme.com",
                "subject": "Hello",
                "body": "Hi",
                "received_at": now,
            }
        ]

        async def fake_fetch(**kwargs):
            return messages

        monkeypatch.setattr(
            "followup.workflows.email.fetch.fetch_inbound_messages",
            fake_fetch,
        )

        workspace_id = str(uuid.uuid4())
        first = await fetch_inbound_emails(repo, workspace_id=workspace_id)
        second = await fetch_inbound_emails(repo, workspace_id=workspace_id)

        assert first.enqueued == 1
        assert second.skipped_duplicate == 1
        assert len(repo.rows) == 1

    asyncio.run(_run())


def test_review_marks_processed_on_pending_action():
    async def _run():
        repo = FakeInboundRepo()
        workspace_id = uuid.uuid4()
        row = await repo.enqueue(
            {
                "workspace_id": workspace_id,
                "message_id": "msg-2",
                "sender_email": "buyer@acme.com",
                "subject": "Pricing",
                "body": "Question",
                "received_at": datetime.now(timezone.utc),
            }
        )
        assert row is not None

        graph = AsyncMock()
        graph.ainvoke.return_value = {
            "status": "completed",
            "run_id": str(uuid.uuid4()),
            "pending_action": {"id": str(uuid.uuid4())},
        }

        result = await review_pending_emails(
            repo, graph, workspace_id=str(workspace_id), batch_size=5
        )

        assert result.processed == 1
        assert repo.rows["msg-2"]["status"] == "processed"

    asyncio.run(_run())


def test_review_marks_skipped_when_no_deal_resolved():
    async def _run():
        repo = FakeInboundRepo()
        workspace_id = uuid.uuid4()
        await repo.enqueue(
            {
                "workspace_id": workspace_id,
                "message_id": "msg-3",
                "sender_email": "unknown@example.com",
                "subject": "Hi",
                "body": "Hello",
                "received_at": datetime.now(timezone.utc),
            }
        )

        graph = AsyncMock()
        graph.ainvoke.return_value = {
            "status": "failed",
            "error": "extract: no deal resolved from sender (unknown sender)",
        }

        result = await review_pending_emails(
            repo, graph, workspace_id=str(workspace_id), batch_size=5
        )

        assert result.skipped == 1
        assert repo.rows["msg-3"]["status"] == "skipped"

    asyncio.run(_run())


def test_send_outbox_marks_sent(monkeypatch):
    async def _run():
        action_id = uuid.uuid4()
        action = SimpleNamespace(
            id=action_id,
            action_payload={
                "draft": {
                    "recipient_email": "buyer@acme.com",
                    "subject": "Follow up",
                    "body": "Hi there",
                }
            },
            draft_result=None,
            execution_status=None,
            execution_error=None,
            acted_on_at=None,
            executed_at=None,
        )

        class FakePendingRepo:
            async def list_accepted_for_outbox(self, workspace_id, batch_size=20):
                return [action]

            async def save(self, saved_action):
                self.saved = saved_action

        repo = FakePendingRepo()

        async def fake_send(action, *, force=False):
            return {"status": "sent"}

        monkeypatch.setattr(
            "followup.workflows.email.send_outbox.send_drafted_email",
            fake_send,
        )

        result = await send_outbox_batch(repo, workspace_id=str(uuid.uuid4()))

        assert result.sent == 1
        assert action.execution_status == "completed"
        assert action.action_payload.get("email_sent_at")

    asyncio.run(_run())
