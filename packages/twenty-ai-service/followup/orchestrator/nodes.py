"""Pipeline nodes for the Follow-Up orchestrator graph.

Each node is an async function ``FollowUpState -> partial-state dict``, built by
``build_nodes(deps)`` so it closes over the dependency bundle (mirrors
``followup/profile/graph.py``). Every node body is wrapped so a failure sets
``error`` + ``status="failed"`` and returns cleanly — a node never crashes the
run. Each node appends its name to ``trace`` for deterministic ordering + the
run log.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tracing import traceable

from followup.contracts.events import EmailSignalEvent
from followup.contracts.next_step import NextStepPlan, NextStepRequest, PlannedStep
from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.routing import prep_tasks_for_plan
from followup.orchestrator.state import FollowUpState
from followup.orchestrator.tasks import FOLLOWUP_SCOPE, TaskContext
from followup.profile.schemas import _row_to_dict

logger = logging.getLogger(__name__)

# entry_point -> the NextStepRequest trigger_type (∈ SIGNAL_TYPES).
_TRIGGER_TYPE = {"email": "email_signal", "orchestrator": "manual", "risk": "scheduled"}

# entry_point → followup_pending_actions.trigger_type (DB naming).
_PENDING_TRIGGER = {"email": "email_signal", "risk": "risk_alert", "orchestrator": "direct_request"}
# entry_point → followup_runs.entry_point (DB naming).
_RUNS_ENTRY = {"email": "email_signal", "risk": "risk_sweep", "orchestrator": "direct"}



# ===========================================================================
# Trace / failure helpers
# ===========================================================================


def _advance(state: FollowUpState, node: str, **updates: Any) -> dict[str, Any]:
    """Partial-state update that records this node ran (linear append to trace)."""
    return {**updates, "trace": list(state.get("trace") or []) + [node]}


def _fail(state: FollowUpState, node: str, error: str) -> dict[str, Any]:
    logger.warning("follow-up node %s failed: %s", node, error)
    return {
        "error": error,
        "status": "failed",
        "trace": list(state.get("trace") or []) + [node],
    }


# ===========================================================================
# Pure helpers (entry-point independent)
# ===========================================================================


def _email_source_text(trigger: dict[str, Any]) -> str:
    """Subject + body, the way the extractor expects an email source text."""
    subject = trigger.get("subject", "")
    body = trigger.get("body", "")
    return f"Subject: {subject}\n\n{body}" if subject else body


def _email_event(trigger: dict[str, Any]) -> EmailSignalEvent:
    return EmailSignalEvent(
        sender_email=trigger.get("sender_email", ""),
        subject=trigger.get("subject", ""),
        body=trigger.get("body", ""),
        received_at=trigger.get("received_at", ""),
        opportunity_id=trigger.get("opportunity_id"),
    )


def synthesize_plan(state: FollowUpState) -> NextStepPlan:
    """Build a plan for paths that skip the next-step agent (risk / orchestrator).

    The next-step agent only runs on the email path; risk sweeps and direct
    requests synthesize an equivalent plan from the score / the rep's intent.
    """
    deal = state["deal_context"]
    trigger = state.get("trigger") or {}
    classification = state.get("classification") or {}

    if state["entry_point"] == "risk":
        risk = state.get("risk_assessment")
        score = risk.risk_score if risk else (deal.risk_score or 0.0)
        if score >= 0.7:
            headline = "escalate"
            steps = [
                PlannedStep(kind="draft_email", intent=f"Re-engage {deal.opportunity_name} on the flagged risk.", priority="high"),
                PlannedStep(kind="write_note", intent=f"Log the risk escalation on {deal.opportunity_name}.", priority="high"),
            ]
        else:
            headline = "follow_up_call"
            steps = [PlannedStep(kind="draft_email", intent=f"Check in on {deal.opportunity_name}.", priority="medium")]
        return NextStepPlan(
            steps=steps,
            headline_action=headline,
            summary=f"Risk sweep flagged '{deal.opportunity_name}' (score {score:.2f}).",
            metadata={"opportunity_id": deal.opportunity_id, "source": "risk_sweep"},
        )

    # orchestrator (direct / manual request)
    instruction = trigger.get("instructions") or "Direct request from the rep."
    if classification.get("requires_calendar"):
        headline = "schedule_meeting"
        steps = [PlannedStep(kind="book_meeting", intent=instruction, priority=classification.get("urgency", "medium"))]
    else:
        headline = "send_proposal"
        steps = [PlannedStep(kind="draft_email", intent=instruction, priority=classification.get("urgency", "medium"))]
    metadata: dict[str, Any] = {"opportunity_id": deal.opportunity_id, "source": "direct_request"}
    if trigger.get("proposed_times"):
        metadata["proposed_times"] = trigger["proposed_times"]
    return NextStepPlan(steps=steps, headline_action=headline, summary=instruction, metadata=metadata)


def determine_action_type(state: FollowUpState) -> str:
    """The bundled pending action's headline action_type — the plan's headline."""
    plan = state.get("plan")
    if plan is not None:
        return plan.headline_action
    return (state.get("classification") or {}).get("type", "no_action")


def build_action_payload(state: FollowUpState) -> dict[str, Any]:
    """Bundle the whole plan + its prepared artifacts, JSON-safe.

    ``steps`` is what Step 7 iterates on accept; ``draft`` / ``calendar`` /
    ``task_results`` are the artifacts the prep tasks produced for those steps.
    """
    payload: dict[str, Any] = {}
    plan = state.get("plan")
    if plan is not None:
        payload["steps"] = [asdict(step) for step in plan.steps]
        payload["headline_action"] = plan.headline_action
        payload["plan_summary"] = plan.summary
    if state.get("draft") is not None:
        payload["draft"] = asdict(state["draft"])
    if state.get("calendar") is not None:
        payload["calendar"] = asdict(state["calendar"])
    if state.get("task_results"):
        payload["task_results"] = state["task_results"]
    # Carry the company id so the executor can link tasks/notes to BOTH the
    # opportunity and its company at accept time — the writer can't read it.
    deal = state.get("deal_context")
    if deal is not None and getattr(deal, "company_id", None):
        payload["company_id"] = deal.company_id
    # Snapshot the triggering email so the workflow card can show the rep WHY
    # this exists — even when no inbound-queue row was the source (a direct
    # /events call). Only the email entry point carries one.
    trigger = state.get("trigger") or {}
    if state.get("entry_point") == "email" and (
        trigger.get("subject") or trigger.get("body")
    ):
        payload["source_email"] = {
            "subject": trigger.get("subject"),
            "body": trigger.get("body"),
            "sender_email": trigger.get("sender_email"),
        }
    return payload


def build_reasoning(state: FollowUpState) -> str:
    parts: list[str] = []
    plan = state.get("plan")
    if plan is not None:
        parts.append(plan.summary)
    risk = state.get("risk_assessment")
    if risk is not None:
        top = risk.factors[0].description if risk.factors else None
        parts.append(f"Risk {risk.risk_score:.2f}" + (f": {top}" if top else ""))
    return " ".join(parts) if parts else "Follow-up recommended."


def get_invoked_agents(state: FollowUpState) -> list[str]:
    invoked: list[str] = []
    if state.get("plan") is not None:
        invoked.append("next_step")
    if state.get("risk_assessment") is not None:
        invoked.append("risk")
    if state.get("draft") is not None:
        invoked.append("drafting")
    return invoked


def resolve_urgency(state: FollowUpState) -> str:
    """The pending action's urgency, from the classification or the plan.

    Risk/orchestrator paths carry a classification with an explicit urgency. The
    email path has no classifier — its urgency is the next-step agent's own
    judgement, read off the highest-priority step in the plan it returned.
    """
    classification = state.get("classification")
    if classification and classification.get("urgency"):
        return classification["urgency"]
    plan = state.get("plan")
    if plan is not None and plan.steps:
        return plan.steps[0].priority
    return "medium"


def compute_expiry(urgency: str) -> datetime:
    hours = {"high": 24, "medium": 72, "low": 168}.get(urgency, 72)
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _coerce_uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid4()


# ===========================================================================
# Prep-task dispatch (shared by run_tasks) — one task → its artifacts
# ===========================================================================


async def run_prep_task(
    deps: OrchestratorDeps,
    task_name: str,
    state: FollowUpState,
    deal: Any,
    plan: Any,
    risk: Any,
    calendar: Any,
) -> dict[str, Any]:
    """Run one prep task and return its artifact dict ({} on skip/failure).

    A single task failing is isolated here so the other writers (which run
    concurrently) still produce their artifacts — the bundled action degrades
    gracefully instead of losing everything.
    """
    spec = deps.task_registry.get(task_name)
    if spec is None:  # not in scope / unknown — skip defensively
        return {}
    learned = deps.task_registry.learn([task_name], FOLLOWUP_SCOPE)
    instructions = learned[0]["instructions"] if learned else ""
    context = TaskContext(
        state=dict(state),
        deal_context=deal,
        plan=plan,
        classification=state.get("classification") or {},
        instructions=instructions,
        risk_assessment=risk,
        calendar=calendar,
    )
    runner = traceable(name=f"followup_task.{task_name}", run_type="tool")(spec.handler)
    try:
        return await runner(context)
    except Exception as error:  # noqa: BLE001 — one writer's failure is isolated
        logger.warning("prep task %s failed (isolated): %s", task_name, error)
        return {}


def merge_task_output(out: dict[str, Any], merged: dict[str, Any]) -> None:
    """Fold one task's artifact dict into the accumulating run artifacts."""
    if out.get("calendar") is not None:
        merged["calendar"] = out["calendar"]
    if out.get("draft") is not None:
        merged["draft"] = out["draft"]
    if out.get("task_results"):
        merged.setdefault("task_results", {}).update(out["task_results"])


# ===========================================================================
# Node factory
# ===========================================================================


def build_nodes(deps: OrchestratorDeps) -> dict[str, Callable]:
    """Build the seven graph nodes, each closed over ``deps``."""

    async def extract(state: FollowUpState) -> dict[str, Any]:
        # Email path only: run the reader+extractor (Step 2) so the just-arrived
        # email's facts are persisted before the profile loads. The email arrives
        # WITHOUT a known deal — extraction resolves it from the sender, so we
        # capture the resolved opportunity_id here for load_profile to read.
        try:
            from followup.profile.extraction import extract_from_email

            trigger = state.get("trigger") or {}
            outcome = await extract_from_email(
                workspace_id=state["workspace_id"],
                source_type="email",
                source_id=str(trigger.get("id", state["run_id"])),
                source_text=_email_source_text(trigger),
                sender_email=trigger.get("sender_email", ""),
                deps=deps.pipeline,
            )
            resolved = getattr(outcome, "opportunity_id", None)
            # A halt (unknown sender, ambiguous deal) leaves no opportunity to
            # work — end the run cleanly with the reason rather than crashing.
            if not resolved and not state.get("opportunity_id"):
                reason = (
                    getattr(outcome, "reason", None)
                    or getattr(outcome, "status", None)
                    or "no opportunity resolved"
                )
                return _fail(
                    state, "extract", f"extract: no deal resolved from sender ({reason})"
                )
            updates = {"opportunity_id": resolved} if resolved else {}
            return _advance(state, "extract", **updates)
        except Exception as error:  # noqa: BLE001
            return _fail(state, "extract", f"extract: {error}")

    async def load_profile(state: FollowUpState) -> dict[str, Any]:
        try:
            deal = await deps.profile_service.build_deal_context(
                state["opportunity_id"], include_shadows=False
            )
            return _advance(
                state,
                "load_profile",
                deal_context=deal,
                profile_narrative=deal.profile_narrative,
            )
        except Exception as error:  # noqa: BLE001
            return _fail(state, "load_profile", f"load_profile: {error}")

    async def classify(state: FollowUpState) -> dict[str, Any]:
        # Deterministic labelling for the risk and orchestrator entry points only.
        # The email path skips this node entirely (the graph routes it straight to
        # plan): the next-step agent reads its own stage playbook + BANT framework
        # and decides from the raw trigger, so no LLM email triage runs here.
        try:
            entry = state["entry_point"]
            if entry == "orchestrator":
                trigger = state.get("trigger") or {}
                instructions = (trigger.get("instructions") or "").lower()
                classification = {
                    "type": "direct_send",
                    "urgency": trigger.get("urgency", "medium"),
                    "requires_next_step": False,
                    "requires_risk": False,
                    "requires_calendar": bool(
                        trigger.get("wants_meeting") or "meeting" in instructions
                    ),
                }
            else:  # risk
                classification = {
                    "type": "risk_alert",
                    "urgency": "high",
                    "requires_next_step": False,
                    "requires_risk": True,
                    "requires_calendar": False,
                }
            return _advance(state, "classify", classification=classification)
        except Exception as error:  # noqa: BLE001
            return _fail(state, "classify", f"classify: {error}")

    async def assess_risk(state: FollowUpState) -> dict[str, Any]:
        # The risk agent owns its own DB-backed context load. The orchestrator
        # passes only identifiers and trigger metadata; it does not assemble
        # DealContext, facts, narratives, or previous risk snapshots for scoring.
        try:
            assessment = await deps.agents.risk.evaluate_deal_risk(
                opportunity_id=str(state["opportunity_id"]),
                workspace_id=state.get("workspace_id"),
                trigger_type=(state.get("trigger") or {}).get("trigger_type")
                or state["entry_point"],
            )
            deal = state.get("deal_context")
            if deal is not None:
                deal.risk_score = assessment.risk_score
            return _advance(
                state, "assess_risk", risk_assessment=assessment, deal_context=deal
            )
        except Exception as error:  # noqa: BLE001
            return _fail(state, "assess_risk", f"assess_risk: {error}")

    async def plan(state: FollowUpState) -> dict[str, Any]:
        # The next-step agent plans only on the email path; risk/orchestrator
        # entries synthesize an equivalent plan from the score / rep intent.
        try:
            entry = state["entry_point"]
            if entry == "email":
                request = NextStepRequest(
                    deal_context=state["deal_context"],
                    trigger_type=_TRIGGER_TYPE.get(entry, "manual"),
                    trigger=_email_event(state["trigger"]),
                    narrative=state.get("profile_narrative"),
                    classification=state.get("classification"),
                    mode="single",
                )
                result = await deps.agents.next_step.run(request)
            else:
                result = synthesize_plan(state)
            return _advance(state, "plan", plan=result)
        except Exception as error:  # noqa: BLE001
            return _fail(state, "plan", f"plan: {error}")

    async def run_tasks(state: FollowUpState) -> dict[str, Any]:
        # Follow the plan: expand its steps into prep tasks that produce the
        # artifacts each step needs. The only ordering constraint is
        # check_calendar → draft_email (the email offers the slots the calendar
        # found); everything else (the small writers — note, task, draft) runs
        # CONCURRENTLY, each with the context it needs. One writer failing is
        # isolated so the rest still produce their artifacts.
        try:
            current_plan = state.get("plan") or synthesize_plan(state)
            deal = state["deal_context"]
            risk = state.get("risk_assessment")
            task_names = prep_tasks_for_plan(current_plan.steps)
            merged: dict[str, Any] = {}

            # Phase A — calendar first (the draft depends on its slots).
            calendar_result = None
            if "check_calendar" in task_names:
                cal_out = await run_prep_task(
                    deps, "check_calendar", state, deal, current_plan, risk, None
                )
                merge_task_output(cal_out, merged)
                calendar_result = merged.get("calendar")

            # Phase B — the independent writers, concurrently.
            rest = [name for name in task_names if name != "check_calendar"]
            outs = await asyncio.gather(
                *(
                    run_prep_task(
                        deps, name, state, deal, current_plan, risk, calendar_result
                    )
                    for name in rest
                )
            )
            for out in outs:
                merge_task_output(out, merged)

            updates: dict[str, Any] = {"plan": current_plan, **merged}
            return _advance(state, "run_tasks", **updates)
        except Exception as error:  # noqa: BLE001
            return _fail(state, "run_tasks", f"run_tasks: {error}")

    async def create_pending(state: FollowUpState) -> dict[str, Any]:
        try:
            opportunity_uuid = _coerce_uuid(state["opportunity_id"])
            workspace_uuid = _coerce_uuid(state["workspace_id"])
            trigger = state.get("trigger") or {}
            urgency = resolve_urgency(state)

            action_data = {
                "id": uuid.uuid4(),
                "opportunity_id": opportunity_uuid,
                "workspace_id": workspace_uuid,
                "trigger_type": _PENDING_TRIGGER.get(state["entry_point"], state["entry_point"]),
                "trigger_id": trigger.get("id"),
                "action_type": determine_action_type(state),
                "action_payload": build_action_payload(state),
                "reasoning": build_reasoning(state),
                "urgency": urgency,
                "next_step_result": _asdict_or_none(state.get("plan")),
                "risk_assessment": _asdict_or_none(state.get("risk_assessment")),
                "draft_result": _asdict_or_none(state.get("draft")),
                "profile_narrative": state.get("profile_narrative"),
                "status": "pending",
                "expires_at": compute_expiry(urgency),
            }
            # Expire any existing pending actions for this opportunity to prevent duplicates
            await deps.pipeline.pending_actions.expire_existing_for_opportunity(opportunity_uuid)
            action = await deps.pipeline.pending_actions.create(action_data)

            run_data = {
                "id": _coerce_uuid(state.get("run_id")),
                "opportunity_id": opportunity_uuid,
                "workspace_id": workspace_uuid,
                "entry_point": _RUNS_ENTRY.get(state["entry_point"], state["entry_point"]),
                "trigger_payload": trigger,
                "pending_action_id": action.id,
                "agents_invoked": get_invoked_agents(state),
                "profile_loaded": state.get("deal_context") is not None,
                "status": "completed",
            }
            await deps.pipeline.runs.create(run_data)

            return _advance(
                state,
                "create_pending",
                pending_action=_row_to_dict(action),
                status="completed",
            )
        except Exception as error:  # noqa: BLE001
            return _fail(state, "create_pending", f"create_pending: {error}")

    # Once a node has failed, later nodes pass through untouched — the run ends
    # with status="failed" instead of crashing on missing state downstream.
    def _guarded(fn: Callable) -> Callable:
        async def wrapper(state: FollowUpState) -> dict[str, Any]:
            if state.get("status") == "failed":
                return {}
            return await fn(state)

        return wrapper

    raw = {
        "extract": extract,
        "load_profile": load_profile,
        "assess_risk": assess_risk,
        "classify": classify,
        "plan": plan,
        "run_tasks": run_tasks,
        "create_pending": create_pending,
    }
    return {name: _guarded(fn) for name, fn in raw.items()}


def _asdict_or_none(value: Any) -> dict[str, Any] | None:
    return asdict(value) if value is not None else None


__all__ = [
    "build_nodes",
    "run_prep_task",
    "merge_task_output",
    "synthesize_plan",
    "determine_action_type",
    "build_action_payload",
    "build_reasoning",
    "get_invoked_agents",
    "resolve_urgency",
    "compute_expiry",
]
