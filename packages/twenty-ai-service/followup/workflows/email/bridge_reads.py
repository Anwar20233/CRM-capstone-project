"""Bridge-backed reads for email monitoring workflows."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from followup.profile.dependencies import _records_from_bridge_data

logger = logging.getLogger(__name__)

_DEFAULT_FETCH_LIMIT = 100


async def bridge_find(tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    from agent.tool_scope import READER_SCOPE
    from agent.tools.composite_reads import _exec, _identity
    from bridge_client import forward

    result = await forward("execute", _exec(tool, args, _identity(READER_SCOPE)))
    if not result.get("ok"):
        logger.warning("bridge %s failed: %s", tool, result.get("error"))
        return []
    return _records_from_bridge_data(result.get("data"))


def _parse_received_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def fetch_inbound_messages(
    *,
    since: Optional[datetime] = None,
    limit: int = _DEFAULT_FETCH_LIMIT,
) -> list[dict[str, Any]]:
    """Read recent CRM messages and return inbound-only rows with sender resolved."""
    args: dict[str, Any] = {
        "limit": limit,
        "offset": 0,
        "orderBy": [{"receivedAt": "DescNullsLast"}],
    }
    if since is not None:
        args["receivedAt"] = {"gte": since.isoformat()}

    messages = await bridge_find("find_messages", args)
    if not messages:
        return []

    message_ids = [str(message["id"]) for message in messages if message.get("id")]
    if not message_ids:
        return []

    associations = await bridge_find(
        "find_message_channel_message_associations",
        {
            "limit": len(message_ids),
            "offset": 0,
            "direction": {"eq": "INCOMING"},
            "messageId": {"in": message_ids},
        },
    )
    incoming_ids = {
        str(association["messageId"])
        for association in associations
        if association.get("messageId")
    }

    participants = await bridge_find(
        "find_message_participants",
        {
            "limit": len(message_ids) * 2,
            "offset": 0,
            "role": {"eq": "FROM"},
            "messageId": {"in": message_ids},
        },
    )
    sender_by_message = {
        str(participant["messageId"]): participant.get("handle") or ""
        for participant in participants
        if participant.get("messageId")
    }

    inbound: list[dict[str, Any]] = []
    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id or message_id not in incoming_ids:
            continue
        sender_email = sender_by_message.get(message_id, "").strip()
        if not sender_email:
            continue
        inbound.append(
            {
                "message_id": message_id,
                "sender_email": sender_email,
                "subject": message.get("subject") or "",
                "body": message.get("text") or message.get("body") or "",
                "received_at": _parse_received_at(message.get("receivedAt")),
            }
        )
    return inbound
