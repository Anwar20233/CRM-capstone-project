"""FollowupActionExecutor — executes an accepted plan, step by step.

A pending action bundles the next-step agent's whole PLAN: an ordered list of
``steps`` (each ``{kind, intent, ...}``) plus the artifacts the follow-up agent's
prep tasks produced (a finished ``draft`` email, ``calendar`` slots, a note body).
On accept this executor walks the steps IN ORDER. There are two execution paths,
split by what the follow-up agent already produced:

* **Finished artifacts → the final writer just uses the responsible tool.**
  ``draft_email`` and ``book_meeting`` carry complete content (the drafter wrote
  the whole email; the calendar prep chose the slot). The executor calls the
  responsible bridge tool DIRECTLY (``send_email`` / ``create_calendar_event``)
  with the prepared arguments under ``WRITER_SCOPE`` — there is nothing to
  compose. (``send_email`` is an ACTION tool the record-writer agent can't reach
  through its object/operation CRUD discovery anyway.)

* **Record writes → the CRM Write agent, through the orchestrator seam.**
  ``write_note`` / ``create_task`` / ``update_stage`` (and any future record
  write) are delegated via ``orchestrator.delegate_write`` — the follow-up agent
  never calls the writer directly. Each write's free-text content is HIDDEN
  behind a handle (``_hide`` → a privacy handle the writer carries verbatim); the
  real target id stays real (a UUID, not PII). The writer unmasks tool args at
  its execute step, so the writer LLM only ever sees handles, never the content.
  This is general — every write type the writer handles is managed the same way.

The follow-up agent never writes directly; every write runs under ``WRITER_SCOPE``.
The action completes only if every step does.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Steps whose content the follow-up agent already finished — sent directly via
# the responsible tool, no LLM re-composition.
_DIRECT_KINDS = frozenset({"draft_email", "book_meeting"})

# Legacy single-action_type → step kind, so pending actions persisted before the
# plan model still execute.
_ACTION_TYPE_TO_KIND: dict[str, str] = {
    "send_email": "draft_email",
    "send_proposal": "draft_email",
    "follow_up_call": "draft_email",
    "check_in": "draft_email",
    "close_deal": "draft_email",
    "schedule_meeting": "book_meeting",
    "add_note": "write_note",
    "write_note": "write_note",
    "create_task": "create_task",
    "escalate": "create_task",
    "update_deal_stage": "update_stage",
}


class FollowupActionExecutor:
    """Walks an accepted plan's steps: direct sends for finished artifacts (email/
    calendar), writer-agent delegation for intent-driven record writes."""

    def __init__(self, session_id: Optional[str] = None, model: Optional[str] = None) -> None:
        self._session_id = session_id or f"followup-write-{uuid.uuid4()}"
        self._model = model

    async def execute(self, action: Any) -> dict[str, Any]:
        """Execute every step of the action's plan in order. Returns ``{status, error?}``."""
        steps = self._steps_for(action)
        if not steps:
            return {
                "status": "failed",
                "error": f"No executable steps for action '{action.action_type}'",
            }

        errors: list[str] = []
        completed = 0
        for index, step in enumerate(steps):
            kind = step.get("kind")
            if kind in _DIRECT_KINDS:
                status, error = await self._execute_direct(kind, step, action)
            else:
                status, error = await self._execute_via_writer(kind, step, action, index)
            if status == "completed":
                completed += 1
            else:
                errors.append(f"step {index} ({kind}): {error or 'failed'}")

        if completed == len(steps):
            return {"status": "completed"}
        if completed == 0:
            return {"status": "failed", "error": "; ".join(errors)}
        return {
            "status": "failed",
            "error": f"{completed}/{len(steps)} steps completed; " + "; ".join(errors),
        }

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    @staticmethod
    def _steps_for(action: Any) -> list[dict[str, Any]]:
        """The plan's steps, or a one-step fallback from a legacy action_type."""
        steps = (action.action_payload or {}).get("steps")
        if isinstance(steps, list) and steps:
            return steps
        kind = _ACTION_TYPE_TO_KIND.get(action.action_type)
        if kind is None:
            return []
        return [{"kind": kind, "intent": action.reasoning or ""}]

    # ------------------------------------------------------------------
    # Path 1 — finished artifact → call the responsible tool directly
    # ------------------------------------------------------------------

    async def _execute_direct(
        self, kind: str, step: dict[str, Any], action: Any
    ) -> tuple[str, Optional[str]]:
        builder = self._DIRECT_BUILDERS.get(kind)
        if builder is None:
            return "failed", f"no direct sender for step '{kind}'"
        built = builder(self, step, action)
        if built is None:
            return "failed", f"no finished content for step '{kind}'"
        tool, args = built
        try:
            await self._direct_write(tool, args)
            return "completed", None
        except Exception as exc:  # noqa: BLE001
            logger.exception("FollowupActionExecutor direct %s (%s) failed", kind, tool)
            return "failed", str(exc)

    async def _direct_write(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a bridge tool directly under WRITER_SCOPE (the final-write path)."""
        return await _direct_bridge_write(tool, args)

    def _direct_draft_email(self, step: dict[str, Any], action: Any) -> Optional[tuple[str, dict]]:
        args = build_send_email_args(action, fallback_body=step.get("intent"))
        return ("send_email", args) if args else None

    def _direct_book_meeting(self, step: dict[str, Any], action: Any) -> Optional[tuple[str, dict]]:
        # The follow-up agent chose the slot; just create the event.
        payload = action.action_payload or {}
        calendar = payload.get("calendar") or {}
        chosen = next(
            (s for s in (calendar.get("available_slots") or []) if s.get("available")),
            None,
        )
        if chosen is None:
            return None
        # Title is the agent-authored meeting brief (names + reason), with the
        # next-step step.intent only as a legacy fallback.
        meeting = (payload.get("task_results") or {}).get("book_meeting") or {}
        title = meeting.get("title") or step.get("intent") or "Follow-up Meeting"
        args = {
            "title": title[:120],
            "startsAt": chosen.get("start"),
            "endsAt": chosen.get("end"),
        }
        return "create_calendar_event", args

    _DIRECT_BUILDERS: dict[str, Any] = {
        "draft_email": _direct_draft_email,
        "book_meeting": _direct_book_meeting,
    }

    # ------------------------------------------------------------------
    # Path 2 — intent → the CRM Write agent composes the record
    # ------------------------------------------------------------------

    async def _execute_via_writer(
        self, kind: str, step: dict[str, Any], action: Any, index: int
    ) -> tuple[str, Optional[str]]:
        # Every write delegated to the writer carries a fresh handle map: the
        # instruction builder hides each piece of free-text content behind a
        # handle (real ids stay real for targeting); the writer unmasks tool args
        # at its execute step, so the writer LLM never sees the real content. This
        # is general — any write type the writer handles is managed the same way.
        from agent.masking import EntityHandleMap

        pii_map = EntityHandleMap()
        instruction = self._build_instruction(kind, step, action, pii_map)
        if instruction is None:
            return "failed", f"no execution mapping for step '{kind}'"
        try:
            # Reach the writer through the orchestrator seam — never directly.
            from agent.orchestrator import delegate_write

            result = await delegate_write(
                instruction,
                pii_map=pii_map,
                session_id=f"{self._session_id}-{index}",
                model=self._model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("FollowupActionExecutor writer step %s (%s) failed", index, kind)
            return "failed", str(exc)

        if result.get("type") == "interrupt":
            return "failed", "write requires manual approval (tier-3 gated)"

        outcome = self._status_from_writes(result.get("tool_calls") or [], kind)
        return outcome["status"], outcome.get("error")

    @staticmethod
    def _status_from_writes(tool_calls: list[dict[str, Any]], kind: str) -> dict[str, Any]:
        """Read the writer's executed ``execute_tool`` calls to decide ok/failed."""
        from agent.tool_scope import is_write_tool

        write_results: list[Any] = []
        for call in tool_calls:
            if call.get("name") != "execute_tool":
                continue
            tool = (call.get("args") or {}).get("tool")
            if tool and is_write_tool(tool):
                write_results.append(call.get("result"))

        if not write_results:
            return {"status": "failed", "error": f"writer executed no CRM write for step '{kind}'"}

        last = write_results[-1]
        if isinstance(last, dict) and last.get("ok"):
            return {"status": "completed"}
        error = ""
        if isinstance(last, dict):
            err = last.get("error")
            error = err.get("message") if isinstance(err, dict) else str(err)
        return {"status": "failed", "error": error or "writer reported a failed write"}

    def _build_instruction(
        self, kind: str, step: dict[str, Any], action: Any, pii_map: Any
    ) -> Optional[str]:
        builder = self._BUILDERS.get(kind)
        if builder is None:
            return None
        return builder(self, step, action, pii_map)

    @staticmethod
    def _opportunity_id(action: Any) -> str:
        return str(action.opportunity_id)

    @staticmethod
    def _company_id(action: Any) -> Optional[str]:
        # Threaded through the action payload at planning time (the writer cannot
        # read it). Absent for deals with no linked company → opp-only linking.
        company_id = (action.action_payload or {}).get("company_id")
        return str(company_id) if company_id else None

    @classmethod
    def _link_clause(cls, action: Any, target_tool: str) -> str:
        """Tell the writer exactly how to link the new record to its targets.

        Twenty links tasks/notes through a join record (``create_task_target`` /
        ``create_note_target``) that takes the new record's id plus
        ``targetOpportunity`` (and ``targetCompany`` when known) — NOT a field on
        the create call. Spelling it out stops the writer guessing fields like
        ``relatedOpportunityId`` (which don't exist) and flailing through
        composite catalogs.
        """
        opp = cls._opportunity_id(action)
        company = cls._company_id(action)
        targets = f"targetOpportunity={opp}"
        if company:
            targets += f", targetCompany={company}"
        return (
            f" After creating it, link it by calling `{target_tool}` with the new "
            f"record's id plus {targets}. Do not invent fields on the create call."
        )

    @staticmethod
    def _hide(pii_map: Any, value: str, *, entity_type: str = "content") -> str:
        """Hide a free-text value behind a handle the writer carries verbatim.

        Returns the handle name (e.g. ``content001``); the real value is restored
        at the writer's execute step. General-purpose — every write type routes
        its PII-bearing content through this, so the writer LLM never sees it.
        """
        if not value:
            return ""
        handle = pii_map.register_privacy(entity_type, value)
        return handle.name if handle is not None else value

    def _instruction_write_note(self, step: dict[str, Any], action: Any, pii_map: Any) -> Optional[str]:
        payload = action.action_payload or {}
        note = (payload.get("task_results") or {}).get("write_note") or {}
        body = note.get("body") or step.get("intent") or action.reasoning or ""
        body_ref = self._hide(pii_map, body, entity_type="note")
        return (
            f"Create a note. "
            f"Use this text verbatim and unaltered as the note body: {body_ref}."
            + self._link_clause(action, "create_note_target")
        )

    def _instruction_create_task(self, step: dict[str, Any], action: Any, pii_map: Any) -> Optional[str]:
        payload = action.action_payload or {}
        task = (payload.get("task_results") or {}).get("create_task") or {}
        title = task.get("title") or step.get("intent") or "Follow-up task"
        title_ref = self._hide(pii_map, title, entity_type="task")
        return (
            f"Create a task. "
            f"Use this text verbatim and unaltered as the task title: {title_ref}."
            + self._link_clause(action, "create_task_target")
        )

    def _instruction_update_stage(self, step: dict[str, Any], action: Any, pii_map: Any) -> Optional[str]:
        # The stage value is an enum (not PII) and the writer must read it to pick
        # the target — so it stays visible; only any free-text detail is hidden.
        intent = step.get("intent") or ""
        detail_ref = self._hide(pii_map, intent, entity_type="note")
        return (
            f"Advance the deal stage on opportunity {self._opportunity_id(action)}. "
            f"Context (verbatim): {detail_ref}"
        )

    _BUILDERS: dict[str, Any] = {
        "write_note": _instruction_write_note,
        "create_task": _instruction_create_task,
        "update_stage": _instruction_update_stage,
    }


# ===========================================================================
# Email send seam — shared by the accept executor AND the future hourly emailer
# ===========================================================================
#
# The "emailer" will be a separate entity that polls accepted actions on a
# schedule and sends their drafted email to the follow-up agent's send tool. To
# accommodate that without a rewrite, the actual send lives in standalone,
# idempotent functions here: ``build_send_email_args`` shapes the payload from a
# pending action's finished draft, ``_direct_bridge_write`` performs the write,
# and ``send_drafted_email`` is the idempotent consumer the cron will call.


def build_send_email_args(
    action: Any, *, fallback_body: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Shape ``send_email`` args from a pending action's finished draft.

    Returns ``None`` when there is nothing to send (no recipient) — the drafter
    already wrote the whole email, so we only ship it. ``send_email`` expects
    ``recipients={to:...}``, a subject, and an HTML body.
    """
    draft = (action.action_payload or {}).get("draft") or action.draft_result or {}
    recipient = draft.get("recipient_email")
    if not recipient:
        return None
    body = draft.get("body") or fallback_body or ""
    return {
        "recipients": {"to": recipient},
        "subject": draft.get("subject") or "Follow-up",
        "body": body.replace("\n", "<br>"),  # the tool expects HTML
    }


async def _direct_bridge_write(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a bridge tool directly under WRITER_SCOPE (the final-write path)."""
    from agent.progress import emit_progress
    from agent.tool_scope import WRITER_SCOPE
    from agent.tools.composite_reads import _exec, _identity
    from bridge_client import forward

    # Surface the direct send the same way the writer agent surfaces its calls,
    # so live traces (and any progress sink) see the final write.
    emit_progress(
        {"type": "tool_call", "name": "execute_tool", "args": {"tool": tool, "tool_args": args}}
    )
    result = await forward("execute", _exec(tool, args, _identity(WRITER_SCOPE)))
    if not result.get("ok"):
        error = result.get("error", {})
        raise RuntimeError(error.get("message", "unknown error"))
    return result.get("data", {})


def email_already_sent(action: Any) -> bool:
    """Idempotency guard: has this action's email already been sent?

    The hourly emailer must never double-send on a re-poll. A completed
    ``execution_status`` means the accept executor already sent it; a future
    dedicated ``email_sent_at`` marker (if added) also counts.
    """
    if getattr(action, "execution_status", None) == "completed":
        return True
    return bool((action.action_payload or {}).get("email_sent_at"))


async def send_drafted_email(action: Any, *, force: bool = False) -> dict[str, Any]:
    """Idempotently send a pending action's drafted email (the outbox consumer).

    Standalone so the future hourly emailer can call it directly: it polls
    accepted actions, and for each unsent draft calls this. Returns
    ``{status: sent|skipped|failed, error?}``. Persisting the sent marker /
    ``execution_status`` is the caller's responsibility (it owns the repo).
    """
    if not force and email_already_sent(action):
        return {"status": "skipped", "reason": "already sent"}
    args = build_send_email_args(action)
    if args is None:
        return {"status": "skipped", "reason": "no drafted email to send"}
    try:
        await _direct_bridge_write("send_email", args)
        return {"status": "sent"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_drafted_email failed for action %s", getattr(action, "id", "?"))
        return {"status": "failed", "error": str(exc)}


def _action_to_fact(action: Any) -> dict[str, Any]:
    """Convert a completed plan into a commitment fact for the profile.

    Returns a dict suitable for ``ProfileFactRepository.create()``.
    """
    now = datetime.now(timezone.utc)
    steps = (action.action_payload or {}).get("steps") or []
    kinds = [s.get("kind") for s in steps if isinstance(s, dict)]
    return {
        "id": uuid.uuid4(),
        "opportunity_id": action.opportunity_id,
        "entity_type": "opportunity",
        # The fact is ABOUT the opportunity, so it references the opportunity as
        # its CRM entity — required by the profile_facts_entity_ref CHECK
        # (entity_crm_id OR shadow_entity_id must be set), else the insert is
        # rejected and the commitment fact silently dropped.
        "entity_crm_id": action.opportunity_id,
        "fact_type": "commitment",
        "fact_value": _fact_value_for(action, kinds),
        # 'agent_action' is not an allowed source_type (the profile_facts CHECK
        # permits email|note|crm_record|meeting|risk_score); a committed action
        # is a CRM write, so it is logged as a crm_record-sourced fact.
        "source_type": "crm_record",
        "confidence": 1.0,
        "extracted_at": now,
        "valid_from": now,
    }


def _fact_value_for(action: Any, kinds: list[str]) -> str:
    """Human-readable summary of the executed plan."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    label = {
        "draft_email": "sent a follow-up email",
        "book_meeting": "booked a meeting",
        "write_note": "logged a note",
        "create_task": "created a task",
        "update_stage": "advanced the deal stage",
    }
    if not kinds:
        return f"Follow-up '{action.action_type}' executed on {now_str}"
    done = ", ".join(label.get(k, k) for k in kinds)
    return f"Follow-up on {now_str}: {done}."


__all__ = [
    "FollowupActionExecutor",
    "_action_to_fact",
    "build_send_email_args",
    "send_drafted_email",
    "email_already_sent",
]
