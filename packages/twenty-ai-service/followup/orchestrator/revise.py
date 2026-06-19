"""In-place revision of a single step in a pending follow-up action (Strategy A).

The chat agent's full re-plan path (Strategy B) re-runs the whole pipeline and
replaces the bundled pending action — collapsing a multi-step workflow down to
whatever the orchestrator synthesizes. That is the right behaviour only when the
rep's request changes *which* actions exist.

For a targeted refinement ("make the email friendlier", "move the meeting to
Tuesday 2pm", "tighten the note") we instead edit ONE step's artifact in place:

* We never re-enter the LangGraph pipeline, so ``expire_existing_for_opportunity``
  is never reached and the other steps' artifacts survive untouched.
* The follow-up agent's OWN authoring (the drafting agent + the note/task LLM
  authors) regenerates only the targeted artifact — same code paths the original
  plan used, via ``run_prep_task`` / ``merge_task_output``.
* **Minimal change:** a meeting edit keeps the existing slot as the baseline and
  applies ONLY the requested delta. "Expand to 1 hour" keeps the start and moves
  the end; it never silently relocates the meeting to a different day. We never
  re-search the calendar on a revise (that is what caused a duration change to
  drift the date).
* **Atomic cascade gate:** changing a shared fact (the meeting time/duration)
  forces every dependent artifact (the email that offers the slot, any note/task
  that references it) to be regenerated. If ANY dependent fails to regenerate, the
  WHOLE revise is rejected and nothing is persisted — the bundle is never left in
  a half-updated, inconsistent state (one thing changed, the others stale).
* **Availability gate:** moving a meeting to a time that is already booked is
  refused — we return ``status="unavailable"`` with alternatives instead of
  booking over the conflict.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from followup.calendar.availability import (
    CalendarResult,
    TimeSlot,
    _parse_iso,
    check_availability,
)
from followup.contracts.next_step import NextStepPlan, PlannedStep
from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.nodes import merge_task_output, run_prep_task
from followup.orchestrator.routing import prep_tasks_for_plan
from followup.store.repositories import PendingAction

logger = logging.getLogger(__name__)

# Content steps whose artifacts can reference a meeting date — recomputed
# alongside a calendar change so nothing is left mentioning the old time.
_DATE_DEPENDENT_KINDS = ("draft_email", "write_note", "create_task")


def _rebuild_plan(action: PendingAction) -> NextStepPlan:
    """Reconstruct the next-step plan from the persisted action.

    Prefers ``next_step_result`` (the planner's own output); falls back to the
    bundled ``action_payload['steps']`` so legacy/synthesized actions still work.
    """
    raw = action.next_step_result or {}
    steps_raw = raw.get("steps") or (action.action_payload or {}).get("steps") or []
    steps = [
        PlannedStep(
            kind=step.get("kind", ""),
            intent=step.get("intent", ""),
            priority=step.get("priority", "medium"),
            metadata=step.get("metadata") or {},
        )
        for step in steps_raw
        if isinstance(step, dict)
    ]
    return NextStepPlan(
        steps=steps,
        headline_action=raw.get("headline_action") or action.action_type,
        summary=raw.get("summary") or action.reasoning or "",
        metadata=raw.get("metadata") or {},
    )


def _find_step(plan: NextStepPlan, target: Any) -> tuple[Optional[int], Optional[PlannedStep]]:
    """Resolve ``target`` (a step kind or an index) to a (index, step) pair."""
    # By index first (accept "1" or 1).
    try:
        index = int(target)
    except (TypeError, ValueError):
        index = None
    if index is not None and 0 <= index < len(plan.steps):
        return index, plan.steps[index]
    # By kind (case-insensitive).
    target_kind = str(target).lower()
    for current_index, step in enumerate(plan.steps):
        if step.kind.lower() == target_kind:
            return current_index, step
    return None, None


def _jsonify(value: Any) -> Any:
    """A dataclass artifact → its JSON-safe dict; anything else passes through."""
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def _current_slot(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The meeting's currently-chosen slot from the persisted calendar artifact."""
    calendar = payload.get("calendar") or {}
    return next(
        (slot for slot in (calendar.get("available_slots") or []) if slot.get("available")),
        None,
    )


def _revised_slot(
    *,
    baseline: Optional[dict[str, Any]],
    requested_time: Optional[str],
    requested_duration_minutes: Optional[int],
) -> tuple[Optional[datetime], Optional[timedelta], Optional[str]]:
    """Apply ONLY the requested delta on top of the existing slot.

    Returns ``(start, duration, error)``. The start defaults to the baseline's
    start (so a duration-only edit never moves the day); the duration defaults to
    the baseline's span (so a time-only edit keeps the length). If neither a
    baseline nor a requested time exists there is nothing to anchor to.
    """
    base_start = _parse_iso(requested_time) if requested_time else None
    base_end = None
    if base_start is None and baseline:
        base_start = _parse_iso(baseline.get("start"))
    if baseline:
        base_end = _parse_iso(baseline.get("end"))
    if base_start is None:
        return None, None, "No existing meeting time to revise — specify a date and time."

    if requested_duration_minutes:
        duration = timedelta(minutes=int(requested_duration_minutes))
    elif baseline and base_end is not None:
        base_baseline_start = _parse_iso(baseline.get("start"))
        duration = (
            base_end - base_baseline_start
            if base_baseline_start is not None and base_end > base_baseline_start
            else timedelta(minutes=30)
        )
    else:
        duration = timedelta(minutes=30)
    return base_start, duration, None


async def _run_recompute(
    deps: OrchestratorDeps,
    task_names: list[str],
    state: dict[str, Any],
    deal: Any,
    plan: NextStepPlan,
    calendar: Any,
) -> dict[str, Any]:
    """Regenerate the dependent content artifacts against a FIXED calendar.

    A revise never re-searches the calendar (that drifts the date); the meeting
    slot is decided deterministically by the caller and threaded in here as
    ``calendar`` so the email/note/task regenerate against the confirmed time.
    The independent writers run concurrently; one failing is isolated (the caller's
    gate then rejects the whole revise).
    """
    merged: dict[str, Any] = {}
    if calendar is not None:
        merged["calendar"] = calendar
    rest = [name for name in task_names if name != "check_calendar"]
    outs = await asyncio.gather(
        *(run_prep_task(deps, name, state, deal, plan, None, calendar) for name in rest)
    )
    for out in outs:
        merge_task_output(out, merged)
    return merged


async def revise_step_in_place(
    deps: OrchestratorDeps,
    *,
    action: PendingAction,
    target: Any,
    instructions: str,
    requested_time: Optional[str] = None,
    requested_duration_minutes: Optional[int] = None,
    user_id: str,
    tz_name: Optional[str] = None,
) -> dict[str, Any]:
    """Edit one step of a pending action in place (Strategy A).

    Returns one of:
    * ``{"status": "updated", "action": <PendingAction>}``
    * ``{"status": "unavailable", "requested_time": str, "alternatives": [dict]}``
    * ``{"status": "error", "error": str}``

    Nothing is persisted unless the whole revise — the targeted edit AND every
    dependent artifact it invalidates — succeeds (the cascade gate).
    """
    if action.status != "pending":
        return {"status": "error", "error": f"Action is '{action.status}', not 'pending'"}

    plan = _rebuild_plan(action)
    _index, step = _find_step(plan, target)
    if step is None:
        return {"status": "error", "error": f"No step matching target '{target}'"}

    is_calendar = step.kind == "book_meeting"
    opportunity_id = str(action.opportunity_id)

    try:
        deal = await deps.profile_service.build_deal_context(
            opportunity_id, include_shadows=False
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("revise: load deal context failed for %s", opportunity_id)
        return {"status": "error", "error": f"load_profile: {error}"}

    payload = dict(action.action_payload or {})

    # ------------------------------------------------------------------
    # Calendar edit: keep the existing slot as the baseline, apply ONLY the
    # requested delta, then verify the resulting interval is actually free. We
    # never re-search the calendar — that is what previously drifted the date on
    # a duration-only change.
    # ------------------------------------------------------------------
    new_calendar = None
    if is_calendar:
        baseline = _current_slot(payload)
        start, duration, error = _revised_slot(
            baseline=baseline,
            requested_time=requested_time,
            requested_duration_minutes=requested_duration_minutes,
        )
        if error:
            return {"status": "error", "error": error}
        end = start + duration
        duration_minutes = max(1, int(duration.total_seconds() // 60))
        try:
            availability = await check_availability(
                calendar_reader=deps.pipeline.calendar_reader,
                owner_user_id=user_id,
                workspace_id=str(action.workspace_id),
                proposed_times=[start.isoformat()],
                duration_minutes=duration_minutes,
                find_slots_when_empty=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("revise: availability check failed")
            return {"status": "error", "error": f"check_calendar: {exc}"}
        if availability.all_busy:
            return {
                "status": "unavailable",
                "requested_time": start.isoformat(),
                "alternatives": [asdict(slot) for slot in availability.suggested_alternatives],
            }
        # Deterministic slot — exactly the confirmed start + requested duration.
        new_calendar = CalendarResult(
            available_slots=[
                TimeSlot(start=start.isoformat(), end=end.isoformat(), available=True)
            ],
            all_busy=False,
            suggested_alternatives=[],
        )

    # Thread the rep's instruction into the targeted step so the agent's authors
    # (which read the matching step's intent) act on it.
    step.intent = f"{step.intent} — Revision requested: {instructions}".strip(" —")

    trigger: dict[str, Any] = {"owner_user_id": user_id, "timezone": tz_name}
    prior_draft = payload.get("draft") or action.draft_result
    if prior_draft:
        trigger["prior_draft"] = prior_draft

    state: dict[str, Any] = {
        "entry_point": "orchestrator",
        "opportunity_id": opportunity_id,
        "workspace_id": str(action.workspace_id),
        "trigger": trigger,
        "classification": {},
    }

    # The calendar threaded into every regenerated artifact: the freshly-confirmed
    # slot on a calendar edit, otherwise the existing slot (so a tone-only email
    # edit keeps offering the same time).
    effective_calendar = new_calendar
    if effective_calendar is None and payload.get("calendar"):
        existing = payload["calendar"]
        effective_calendar = CalendarResult(
            available_slots=[TimeSlot(**slot) for slot in (existing.get("available_slots") or [])],
            all_busy=bool(existing.get("all_busy")),
            suggested_alternatives=[
                TimeSlot(**slot) for slot in (existing.get("suggested_alternatives") or [])
            ],
        )

    # Recompute set:
    # * non-calendar edit → just the targeted content step.
    # * calendar edit → every date-dependent content step in the plan, because the
    #   shared meeting time just changed and any artifact mentioning it is now
    #   stale (the email offering the slot, a note/task referencing the date).
    if is_calendar:
        recompute_steps = [
            other for other in plan.steps if other.kind in _DATE_DEPENDENT_KINDS
        ]
    else:
        recompute_steps = [step]

    task_names = prep_tasks_for_plan(recompute_steps)
    try:
        merged = await _run_recompute(
            deps, task_names, state, deal, plan, effective_calendar
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("revise: recompute failed for action %s", action.id)
        return {"status": "error", "error": f"recompute: {error}"}

    # ------------------------------------------------------------------
    # CASCADE GATE — all-or-nothing. Every step we set out to recompute MUST have
    # produced a fresh artifact; if any did not, we persist NOTHING so the bundle
    # is never left half-updated (the meeting moved but the email still stale).
    # ------------------------------------------------------------------
    produced: set[str] = set()
    if merged.get("draft") is not None:
        produced.add("draft_email")
    produced.update((merged.get("task_results") or {}).keys())
    required = {s.kind for s in recompute_steps}
    missing = required - produced
    if missing:
        logger.warning(
            "revise: cascade incomplete for action %s; missing %s — no changes applied",
            action.id,
            sorted(missing),
        )
        return {
            "status": "error",
            "error": (
                "Could not consistently update dependent step(s) "
                f"{sorted(missing)} — no changes were applied."
            ),
        }

    # Gate passed — apply every change atomically. Untouched steps and their
    # artifacts (and the full steps list) stay exactly as they were.
    if new_calendar is not None:
        payload["calendar"] = _jsonify(new_calendar)
    if merged.get("draft") is not None:
        payload["draft"] = _jsonify(merged["draft"])
        action.draft_result = _jsonify(merged["draft"])
    if merged.get("task_results"):
        payload.setdefault("task_results", {}).update(merged["task_results"])

    action.action_payload = payload
    action.acted_on_at = datetime.now(timezone.utc)
    try:
        action.acted_on_by = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        pass

    saved = await deps.pipeline.pending_actions.save(action)
    return {"status": "updated", "action": saved}


__all__ = ["revise_step_in_place"]
