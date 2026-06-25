"""Conditional-edge logic and the per-step prep map for the orchestrator graph.

Routing is deterministic and entry-point driven for the first hop. The PLAN the
next-step agent returns (a list of ``PlannedStep`` intents) then drives which
prep tasks run, via ``STEP_PREP`` — the mechanical "how to prepare each kind of
step" map. The planning intelligence itself lives in the next-step agent; this
file only expands each step.kind into the prep task(s) the orchestrator runs
before persisting the bundled pending action.
"""

from __future__ import annotations

from typing import Iterable

from followup.orchestrator.state import FollowUpState

# step.kind -> ordered prep tasks that produce the artifacts the step needs.
# draft_email -> a draft; book_meeting -> calendar slots + a draft offering them;
# write_note -> the note body; create_task -> the task title. Both note and task
# content are authored at plan time (by the follow-up agent's own LLMs) so they
# are in the pending action the rep reviews. update_stage / update_opportunity
# run validate_opportunity_change, which grounds the proposed field+value against
# the real pipeline so the rep reviews (and the executor writes) a concrete,
# valid change instead of a free-text intent the writer has to guess at.
STEP_PREP: dict[str, list[str]] = {
    "draft_email": ["draft_email"],
    "book_meeting": ["check_calendar", "draft_email"],
    "write_note": ["write_note"],
    "create_task": ["create_task"],
    "update_stage": ["validate_opportunity_change"],
    "update_opportunity": ["validate_opportunity_change"],
}

# Fallback when a plan step has an unknown kind: draft a plain follow-up email
# (the rep reviews it before anything is sent).
_DEFAULT_PREP: list[str] = ["draft_email"]


def prep_tasks_for_plan(steps: Iterable) -> list[str]:
    """The ordered, de-duplicated prep tasks for a whole plan.

    Tasks are shared across the plan (one draft, one calendar check). Order is
    preserved with one guarantee: ``check_calendar`` precedes ``draft_email`` so
    the draft can offer the free slots the calendar found.
    """
    ordered: list[str] = []
    for step in steps:
        kind = getattr(step, "kind", None)
        prep = STEP_PREP.get(kind, _DEFAULT_PREP) if kind else _DEFAULT_PREP
        for task in prep:
            if task not in ordered:
                ordered.append(task)
    if "check_calendar" in ordered and "draft_email" in ordered:
        ordered = ["check_calendar"] + [t for t in ordered if t != "check_calendar"]
    return ordered


def route_entry(state: FollowUpState) -> str:
    """First hop: email triggers extract first; everything else loads the profile."""
    return "extract" if state["entry_point"] == "email" else "load_profile"


__all__ = ["STEP_PREP", "prep_tasks_for_plan", "route_entry"]
