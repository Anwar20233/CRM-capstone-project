"""Orchestrator — routes rep messages to the correct CRM agent worker."""

from __future__ import annotations

import json

from agent.workers.reader_worker import ReaderWorker
from agent.workers.writer_worker import WriterWorker


async def _session_get_topic(session_id: str) -> dict:
    return {"topic": None}  # STUB — replace when Person 2 delivers session tools


async def _session_set_topic(session_id: str, topic: dict) -> dict:
    return {"ok": True}  # STUB — replace when Person 2 delivers session tools


_FOLLOWUP_KEYWORDS = ("follow up", "follow-up", "schedule", "remind")
_WRITE_KEYWORDS = (
    "create",
    "update",
    "add",
    "change",
    "move",
    "delete",
    "remove",
    "advance",
)
_READ_KEYWORDS = (
    "find",
    "get",
    "search",
    "show",
    "list",
    "who",
    "what",
    "how many",
)


def _classify_intent(message: str) -> tuple[str, bool]:
    normalised = message.lower()

    if any(keyword in normalised for keyword in _FOLLOWUP_KEYWORDS):
        return "followup", False

    if any(keyword in normalised for keyword in _WRITE_KEYWORDS):
        return "write", False

    if any(keyword in normalised for keyword in _READ_KEYWORDS):
        return "read", False

    return "read", True


def _parse_reader_response(response: str) -> dict | None:
    try:
        parsed = json.loads(response.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _build_topic(intent: str, parsed: dict | None) -> dict:
    record_id = None
    entity_type = None

    if parsed is not None:
        entity_type = parsed.get("entity_type")
        if parsed.get("resolution") == "single":
            record = parsed.get("record")
            if isinstance(record, dict):
                record_id = record.get("id")

    return {
        "entity_type": entity_type,
        "intent": intent,
        "id": record_id,
    }


class Orchestrator:
    def __init__(
        self,
        session_id: str = "default",
        model: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.reader = ReaderWorker(session_id=session_id, model=model)
        self.writer = WriterWorker(session_id=session_id, model=model)
        # Future workers added here:
        # self.followup = FollowUpWorker(...)  # not built yet
        # self.research = ResearchWorker(...)  # not built yet

    async def handle(self, user_message: str) -> dict:
        await _session_get_topic(self.session_id)

        intent, is_default_route = _classify_intent(user_message)
        parsed: dict | None = None
        result: dict

        if intent == "read":
            result = await self.reader.run(user_message)
            parsed = _parse_reader_response(result["response"])
            if is_default_route:
                result = {**result, "routed_by": "default"}

        elif intent == "write":
            reader_result = await self.reader.run(user_message)
            parsed = _parse_reader_response(reader_result["response"])

            if parsed is None:
                result = {
                    "status": "unresolved",
                    "message": "Could not parse reader response",
                }
            elif parsed.get("resolution") == "none":
                result = {
                    "status": "unresolved",
                    "message": "Could not find the record",
                }
            elif parsed.get("resolution") == "multiple":
                result = {
                    "status": "ambiguous",
                    "candidates": parsed.get("candidates", []),
                }
            else:
                writer_message = (
                    f"Resolved record:\n{json.dumps(parsed['record'])}\n\n"
                    f"Entity type: {parsed['entity_type']}\n\n"
                    f"Instruction: {user_message}"
                )
                result = await self.writer.run(writer_message)

        elif intent == "followup":
            result = {
                "status": "stub",
                "agent": "followup",
                "message": "Follow-up agent not yet implemented",
                "original_request": user_message,
            }

        elif intent == "research":
            result = {
                "status": "stub",
                "agent": "research",
                "message": "Research agent not yet implemented",
                "original_request": user_message,
            }

        else:
            result = await self.reader.run(user_message)
            parsed = _parse_reader_response(result["response"])
            if is_default_route:
                result = {**result, "routed_by": "default"}

        await _session_set_topic(
            self.session_id,
            _build_topic(intent, parsed),
        )
        return result


orchestrator = Orchestrator()
