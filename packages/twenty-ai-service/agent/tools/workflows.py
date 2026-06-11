"""Workflow tools — high-level compound CRM operations in a single tool call.

The core idea: instead of an agent making 4–8 sequential tool calls to complete
a business operation, one workflow tool does the whole job internally — reads,
writes, linking, notes, and follow-up tasks — and returns a structured summary
of everything that was done.

Workflow list
~~~~~~~~~~~~~

Read/Analysis workflows (READ capability):
  ``account_health_check``    — full account health report: overdue tasks, stale
                                deals, last activity date, open risks
  ``deal_risk_report``        — deep risk analysis on a single opportunity

Write workflows (WRITE capability):
  ``change_company_budget``         — find → read current → update → log note
  ``onboard_new_client``            — company + person + opportunity + note + tasks
  ``qualify_and_create_deal``       — find/create person + company → opportunity + note
  ``close_deal``                    — advance to closed stage + summary note + follow-up task
  ``pipeline_push``                 — advance one stage + reason note + next-stage task
  ``schedule_account_review``       — task + note + update company's next-review date
  ``create_meeting_summary``        — note + one task per action item + update last-contact
  ``upsert_contact_at_company``     — find-or-create person + company + link
  ``bulk_update_deal_stage``        — move ALL open opps for a company + log note on each
  ``reassign_account``              — update company + all open opps + handoff note
  ``send_proposal_followup``        — advance to Proposal stage + note + follow-up task
  ``convert_lead_to_opportunity``   — person → company → opportunity → task
  ``deal_lost_recovery``            — restore + update approach + follow-up task
  ``emergency_account_escalation``  — urgent task + escalation note + flag all open opps

Each workflow internally calls ``bridge_client.forward()`` directly rather than
going through the agent's ``execute_tool`` meta-tool.  Writes use the bridge's
write role (``TWENTY_WRITER_ROLE_ID``).  Dangerous workflows (bulk operations,
closures) require a ``confirmation_token`` that must be obtained from the first
call before the destructive part executes.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any

from langchain_core.tools import StructuredTool

from bridge_client import forward
from agent.tools.bridge_args import find_tool_args
from agent.tool_scope import ToolScope


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _identity(scope: ToolScope) -> dict[str, str]:
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID")
    user_id = os.environ.get("TWENTY_USER_ID")
    try:
        role_id = scope.role_id
    except RuntimeError:
        role_id = None
    missing = [k for k, v in {"TWENTY_WORKSPACE_ID": workspace_id, scope.role_env_var: role_id, "TWENTY_USER_ID": user_id}.items() if not v]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
    return {"workspace_id": workspace_id, "role_id": role_id, "user_id": user_id}


async def _run(tool: str, args: dict, ident: dict) -> dict:
    """Execute a single bridge tool and return the envelope."""
    return await forward("execute", {
        "tool": tool,
        "args": args,
        "workspaceId": ident["workspace_id"],
        "roleId": ident["role_id"],
        "userId": ident["user_id"],
    })


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _pull(result: dict) -> tuple[bool, Any]:
    if not result.get("ok"):
        return False, result.get("error", {}).get("message", "unknown error")
    return True, result.get("data")


def _edges(result: dict) -> list[dict]:
    """Extract node list from a paginated bridge result."""
    ok, data = _pull(result)
    if not ok or not data:
        return []
    if isinstance(data, dict):
        return [e.get("node", e) for e in data.get("edges", [])]
    return []


# Lightweight in-process confirmation token store (same pattern as write_policy).
# Maps token → {"action": str, "payload": dict, "expires": float}
_WORKFLOW_TOKENS: dict[str, dict] = {}
_TOKEN_TTL = 600  # 10 minutes


def _issue_token(action: str, payload: dict) -> str:
    token = secrets.token_urlsafe(24)
    _WORKFLOW_TOKENS[token] = {
        "action": action,
        "payload": payload,
        "expires": time.time() + _TOKEN_TTL,
    }
    return token


def _consume_token(token: str, expected_action: str) -> tuple[bool, str, dict]:
    """Validate and consume a confirmation token.  Returns (ok, error_msg, payload)."""
    entry = _WORKFLOW_TOKENS.get(token)
    if not entry:
        return False, "Invalid or already-used confirmation token.", {}
    if time.time() > entry["expires"]:
        _WORKFLOW_TOKENS.pop(token, None)
        return False, "Confirmation token has expired (10-minute window).", {}
    if entry["action"] != expected_action:
        return False, f"Token is for '{entry['action']}', not '{expected_action}'.", {}
    _WORKFLOW_TOKENS.pop(token)
    return True, "", entry["payload"]


# ---------------------------------------------------------------------------
# READ workflows — account_health_check, deal_risk_report
# ---------------------------------------------------------------------------

async def _account_health_check(company_id: str) -> dict:
    """Full account health report in one call.

    Checks: overdue tasks, stale deals (not touched in 30 days), last activity
    date, contact count, total open pipeline value, and open risks.
    Returns a ``health_score`` (0–100) and a ``risk_flags`` list.
    """
    ident = _identity(_account_health_check._scope)  # type: ignore[attr-defined]
    now_ts = time.time()
    stale_cutoff = int(now_ts - 30 * 86400)
    import datetime
    stale_iso = datetime.datetime.fromtimestamp(stale_cutoff, tz=datetime.timezone.utc).isoformat()

    company_r, people_r, opps_r, overdue_tasks_r, notes_r = await asyncio.gather(
        _run("find_one_company", {"id": company_id}, ident),
        _run("find_people", find_tool_args({"companyId": {"eq": company_id}}), ident),
        _run(
            "find_opportunities",
            find_tool_args(
                {"companyId": {"eq": company_id}, "stage": {"notIn": ["CLOSED_WON", "CLOSED_LOST"]}},
            ),
            ident,
        ),
        _run(
            "find_tasks",
            find_tool_args({
                "taskTargets": {"some": {"companyId": {"eq": company_id}}},
                "status": {"neq": "DONE"},
                "dueAt": {"lt": datetime.datetime.now(datetime.timezone.utc).isoformat()},
            }),
            ident,
        ),
        _run(
            "find_notes",
            find_tool_args(
                {"noteTargets": {"some": {"companyId": {"eq": company_id}}}},
                limit=1,
                order_by={"updatedAt": "DescNullsLast"},
            ),
            ident,
        ),
    )

    ok_co, company = _pull(company_r)
    if not ok_co:
        return _err("COMPANY_NOT_FOUND", f"Cannot find company {company_id}")

    people = _edges(people_r)
    opps = _edges(opps_r)
    overdue_tasks = _edges(overdue_tasks_r)
    notes = _edges(notes_r)

    last_activity = notes[0].get("updatedAt") if notes else None
    total_pipeline = sum((o.get("amount") or {}).get("amountMicros", 0) or 0 for o in opps)

    stale_deals = [
        o for o in opps
        if (o.get("updatedAt") or "9999") < stale_iso
    ]

    risk_flags: list[str] = []
    if overdue_tasks:
        risk_flags.append(f"{len(overdue_tasks)} overdue task(s)")
    if stale_deals:
        risk_flags.append(f"{len(stale_deals)} deal(s) not updated in 30+ days")
    if not people:
        risk_flags.append("No contacts linked to this company")
    if not last_activity:
        risk_flags.append("No notes/activity on record")
    if not opps:
        risk_flags.append("No open opportunities")

    # Simple health score: start at 100, subtract penalties
    score = 100
    score -= len(overdue_tasks) * 10
    score -= len(stale_deals) * 15
    score -= 20 if not people else 0
    score -= 15 if not last_activity else 0
    score -= 10 if not opps else 0
    score = max(0, min(100, score))

    return _ok({
        "company": company,
        "health_score": score,
        "risk_flags": risk_flags,
        "contact_count": len(people),
        "open_deal_count": len(opps),
        "total_pipeline_micros": total_pipeline,
        "overdue_tasks": overdue_tasks,
        "stale_deals": stale_deals,
        "last_activity_date": last_activity,
    })


async def _deal_risk_report(opportunity_id: str) -> dict:
    """Deep risk analysis for a single opportunity.

    Checks: days since last update, missing required fields (close date, amount,
    point of contact), overdue tasks, no recent notes, stage regression risk.
    Returns ``risk_level`` (low/medium/high) and actionable ``recommendations``.
    """
    ident = _identity(_deal_risk_report._scope)  # type: ignore[attr-defined]
    import datetime

    opp_r, notes_r, tasks_r = await asyncio.gather(
        _run("find_one_opportunity", {"id": opportunity_id}, ident),
        _run(
            "find_notes",
            find_tool_args(
                {"noteTargets": {"some": {"opportunityId": {"eq": opportunity_id}}}},
                limit=3,
                order_by={"updatedAt": "DescNullsLast"},
            ),
            ident,
        ),
        _run(
            "find_tasks",
            find_tool_args({
                "taskTargets": {"some": {"opportunityId": {"eq": opportunity_id}}},
                "status": {"neq": "DONE"},
            }),
            ident,
        ),
    )

    ok_opp, opp = _pull(opp_r)
    if not ok_opp:
        return _err("OPP_NOT_FOUND", f"Cannot find opportunity {opportunity_id}")

    notes = _edges(notes_r)
    tasks = _edges(tasks_r)
    overdue_tasks = [t for t in tasks if (t.get("dueAt") or "9999") < datetime.datetime.now(datetime.timezone.utc).isoformat()]

    risks: list[dict] = []
    now = datetime.datetime.now(datetime.timezone.utc)

    # Stale update
    updated_at = opp.get("updatedAt")
    if updated_at:
        try:
            delta = now - datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if delta.days > 14:
                risks.append({"type": "stale", "severity": "high", "detail": f"Last updated {delta.days} days ago"})
            elif delta.days > 7:
                risks.append({"type": "stale", "severity": "medium", "detail": f"Last updated {delta.days} days ago"})
        except ValueError:
            pass

    # Missing close date
    if not opp.get("closeDate"):
        risks.append({"type": "missing_close_date", "severity": "medium", "detail": "No close date set"})

    # Missing amount
    amount = (opp.get("amount") or {}).get("amountMicros")
    if not amount:
        risks.append({"type": "missing_amount", "severity": "medium", "detail": "No deal amount set"})

    # No point of contact
    if not opp.get("pointOfContactId"):
        risks.append({"type": "no_poc", "severity": "high", "detail": "No point of contact assigned"})

    # Overdue tasks
    if overdue_tasks:
        risks.append({"type": "overdue_tasks", "severity": "high", "detail": f"{len(overdue_tasks)} overdue task(s)"})

    # No recent notes
    if not notes:
        risks.append({"type": "no_notes", "severity": "medium", "detail": "No notes on this deal"})

    risk_level = "low"
    high_count = sum(1 for r in risks if r["severity"] == "high")
    med_count = sum(1 for r in risks if r["severity"] == "medium")
    if high_count >= 2 or (high_count >= 1 and med_count >= 2):
        risk_level = "high"
    elif high_count >= 1 or med_count >= 2:
        risk_level = "medium"

    recommendations = []
    for r in risks:
        if r["type"] == "stale":
            recommendations.append("Update the deal record or add a progress note")
        elif r["type"] == "missing_close_date":
            recommendations.append("Set a realistic close date to keep the pipeline accurate")
        elif r["type"] == "missing_amount":
            recommendations.append("Add a deal amount to include this in pipeline reporting")
        elif r["type"] == "no_poc":
            recommendations.append("Assign a point of contact person to this opportunity")
        elif r["type"] == "overdue_tasks":
            recommendations.append("Complete or reschedule the overdue action items")
        elif r["type"] == "no_notes":
            recommendations.append("Add a status note to document current deal progress")

    return _ok({
        "opportunity": opp,
        "risk_level": risk_level,
        "risks": risks,
        "recommendations": recommendations,
        "recent_notes": notes,
        "open_tasks": tasks,
    })


# ---------------------------------------------------------------------------
# WRITE workflows
# ---------------------------------------------------------------------------

async def _change_company_budget(
    company_id: str,
    new_budget_micros: int,
    currency_code: str = "USD",
    reason: str = "",
) -> dict:
    """Update a company's annual recurring revenue / budget and log a note.

    Reads the current value first, applies the update, then creates an audit
    note with the before/after values and the reason.
    ``new_budget_micros`` is the amount in micros (1 USD = 1_000_000 micros).
    """
    ident = _identity(_change_company_budget._scope)  # type: ignore[attr-defined]

    ok_co, company = _pull(await _run("find_one_company", {"id": company_id}, ident))
    if not ok_co:
        return _err("COMPANY_NOT_FOUND", f"Cannot find company {company_id}: {company}")

    old_arv = company.get("annualRecurringRevenue") or {}
    old_micros = old_arv.get("amountMicros", 0)
    old_currency = old_arv.get("currencyCode", currency_code)

    ok_up, updated = _pull(await _run("update_company", {
        "id": company_id,
        "annualRecurringRevenue": {"amountMicros": new_budget_micros, "currencyCode": currency_code},
    }, ident))
    if not ok_up:
        return _err("UPDATE_FAILED", f"Could not update company: {updated}")

    note_body = (
        f"Budget updated: {old_micros / 1_000_000:.2f} {old_currency} → "
        f"{new_budget_micros / 1_000_000:.2f} {currency_code}."
        + (f"\nReason: {reason}" if reason else "")
    )
    await _run("create_note", {
        "title": "Budget Update",
        "body": note_body,
        "noteTargets": {"createMany": {"data": [{"companyId": company_id}]}},
    }, ident)

    return _ok({
        "action": "change_company_budget",
        "company_id": company_id,
        "old_budget": old_arv,
        "new_budget": {"amountMicros": new_budget_micros, "currencyCode": currency_code},
        "note_logged": True,
    })


async def _onboard_new_client(
    company_name: str,
    contact_first_name: str,
    contact_last_name: str,
    contact_email: str,
    deal_value_micros: int = 0,
    currency_code: str = "USD",
    notes: str = "",
) -> dict:
    """Full client onboarding in one shot.

    Creates: company → person linked to company → opportunity linked to both →
    welcome note → onboarding task checklist (3 tasks: intro call, send contract,
    schedule kickoff).  Returns IDs of all created records.
    """
    ident = _identity(_onboard_new_client._scope)  # type: ignore[attr-defined]

    # 1. Create company
    ok_co, company = _pull(await _run("create_company", {"name": company_name}, ident))
    if not ok_co:
        return _err("CREATE_COMPANY_FAILED", str(company))
    company_id = company.get("id")

    # 2. Create person linked to company
    ok_p, person = _pull(await _run("create_person", {
        "name": {"firstName": contact_first_name, "lastName": contact_last_name},
        "emails": {"primaryEmail": contact_email},
        "company": {"connect": {"id": company_id}},
    }, ident))
    if not ok_p:
        return _err("CREATE_PERSON_FAILED", str(person))
    person_id = person.get("id")

    # 3. Create opportunity
    ok_opp, opp = _pull(await _run("create_opportunity", {
        "name": f"{company_name} — New Client",
        "stage": "NEW",
        "amount": {"amountMicros": deal_value_micros, "currencyCode": currency_code},
        "company": {"connect": {"id": company_id}},
        "pointOfContact": {"connect": {"id": person_id}},
    }, ident))
    opp_id = opp.get("id") if ok_opp else None

    # 4. Welcome note + 3 onboarding tasks (in parallel)
    note_body = f"New client onboarded: {company_name}.\nPrimary contact: {contact_first_name} {contact_last_name} <{contact_email}>." + (f"\n\n{notes}" if notes else "")

    import datetime
    due_call = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).isoformat()
    due_contract = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)).isoformat()
    due_kickoff = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=14)).isoformat()

    targets_co = {"createMany": {"data": [{"companyId": company_id}]}}
    targets_opp = {"createMany": {"data": [{"companyId": company_id}] + ([{"opportunityId": opp_id}] if opp_id else [])}}

    note_r, task1_r, task2_r, task3_r = await asyncio.gather(
        _run("create_note", {"title": "Client Onboarded", "body": note_body, "noteTargets": targets_co}, ident),
        _run("create_task", {"title": f"Intro call — {company_name}", "status": "TODO", "dueAt": due_call, "taskTargets": targets_opp}, ident),
        _run("create_task", {"title": f"Send contract — {company_name}", "status": "TODO", "dueAt": due_contract, "taskTargets": targets_opp}, ident),
        _run("create_task", {"title": f"Schedule kickoff — {company_name}", "status": "TODO", "dueAt": due_kickoff, "taskTargets": targets_opp}, ident),
    )

    return _ok({
        "action": "onboard_new_client",
        "company_id": company_id,
        "person_id": person_id,
        "opportunity_id": opp_id,
        "note_created": note_r.get("ok"),
        "tasks_created": [r.get("ok") for r in [task1_r, task2_r, task3_r]],
    })


async def _qualify_and_create_deal(
    person_name: str,
    company_name: str,
    deal_name: str,
    amount_micros: int = 0,
    currency_code: str = "USD",
    stage: str = "SCREENING",
    qualification_notes: str = "",
) -> dict:
    """Find-or-create a person and company, then create a linked opportunity.

    Searches for existing records first — if not found, creates them.  Then
    creates the opportunity, links everyone, and adds a qualification note.
    Prevents duplicate records from being created for known entities.
    """
    ident = _identity(_qualify_and_create_deal._scope)  # type: ignore[attr-defined]

    # Search for existing person and company in parallel
    first_name, *rest = person_name.strip().split()
    last_name = " ".join(rest) if rest else ""

    people_r, companies_r = await asyncio.gather(
        _run(
            "find_people",
            find_tool_args(
                {"name": {"firstName": {"ilike": f"%{first_name}%"}, "lastName": {"ilike": f"%{last_name}%"}}},
                limit=1,
            ),
            ident,
        ),
        _run("find_companies", find_tool_args({"name": {"ilike": f"%{company_name}%"}}, limit=1), ident),
    )

    existing_people = _edges(people_r)
    existing_companies = _edges(companies_r)

    # Use existing or create
    if existing_companies:
        company_id = existing_companies[0]["id"]
        company_created = False
    else:
        ok_co, co = _pull(await _run("create_company", {"name": company_name}, ident))
        if not ok_co:
            return _err("CREATE_COMPANY_FAILED", str(co))
        company_id = co["id"]
        company_created = True

    if existing_people:
        person_id = existing_people[0]["id"]
        person_created = False
    else:
        ok_p, p = _pull(await _run("create_person", {
            "name": {"firstName": first_name, "lastName": last_name},
            "company": {"connect": {"id": company_id}},
        }, ident))
        if not ok_p:
            return _err("CREATE_PERSON_FAILED", str(p))
        person_id = p["id"]
        person_created = True

    # Create opportunity
    ok_opp, opp = _pull(await _run("create_opportunity", {
        "name": deal_name,
        "stage": stage,
        "amount": {"amountMicros": amount_micros, "currencyCode": currency_code},
        "company": {"connect": {"id": company_id}},
        "pointOfContact": {"connect": {"id": person_id}},
    }, ident))
    if not ok_opp:
        return _err("CREATE_OPP_FAILED", str(opp))
    opp_id = opp["id"]

    # Qualification note
    note_body = f"Deal qualified: {deal_name}\nStage: {stage}\nContact: {person_name} @ {company_name}." + (f"\n\nNotes: {qualification_notes}" if qualification_notes else "")
    await _run("create_note", {
        "title": "Deal Qualification",
        "body": note_body,
        "noteTargets": {"createMany": {"data": [{"opportunityId": opp_id}, {"companyId": company_id}]}},
    }, ident)

    return _ok({
        "action": "qualify_and_create_deal",
        "opportunity_id": opp_id,
        "company_id": company_id,
        "company_created": company_created,
        "person_id": person_id,
        "person_created": person_created,
    })


async def _close_deal(
    opportunity_id: str,
    outcome: str,
    reason: str = "",
    next_steps: str = "",
    follow_up_days: int = 30,
) -> dict:
    """Close a deal (won or lost) with a full wrap-up in one call.

    ``outcome`` must be ``"won"`` or ``"lost"``.
    Advances the stage, creates a closure summary note, and schedules a
    follow-up task (for won: account check-in; for lost: re-engage attempt).
    """
    ident = _identity(_close_deal._scope)  # type: ignore[attr-defined]
    outcome_lower = outcome.lower()
    if outcome_lower not in ("won", "lost"):
        return _err("INVALID_OUTCOME", "outcome must be 'won' or 'lost'")

    stage = "CLOSED_WON" if outcome_lower == "won" else "CLOSED_LOST"
    emoji = "🎉" if outcome_lower == "won" else "❌"
    task_title_prefix = "Account check-in" if outcome_lower == "won" else "Re-engage attempt"

    import datetime
    follow_up_date = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=follow_up_days)).isoformat()

    ok_up, updated = _pull(await _run("update_opportunity", {"id": opportunity_id, "stage": stage}, ident))
    if not ok_up:
        return _err("UPDATE_FAILED", str(updated))

    deal_name = updated.get("name", opportunity_id)
    note_body = (
        f"{emoji} Deal {outcome_lower}: {deal_name}\n"
        + (f"Reason: {reason}\n" if reason else "")
        + (f"Next steps: {next_steps}" if next_steps else "")
    )

    note_r, task_r = await asyncio.gather(
        _run("create_note", {
            "title": f"Deal Closed {'Won' if outcome_lower == 'won' else 'Lost'}",
            "body": note_body,
            "noteTargets": {"createMany": {"data": [{"opportunityId": opportunity_id}]}},
        }, ident),
        _run("create_task", {
            "title": f"{task_title_prefix} — {deal_name}",
            "status": "TODO",
            "dueAt": follow_up_date,
            "taskTargets": {"createMany": {"data": [{"opportunityId": opportunity_id}]}},
        }, ident),
    )

    return _ok({
        "action": "close_deal",
        "opportunity_id": opportunity_id,
        "deal_name": deal_name,
        "stage": stage,
        "note_created": note_r.get("ok"),
        "follow_up_task_created": task_r.get("ok"),
        "follow_up_date": follow_up_date,
    })


async def _pipeline_push(
    opportunity_id: str,
    notes: str = "",
) -> dict:
    """Advance an opportunity by exactly one pipeline stage.

    Reads the current stage, determines the next stage in sequence, updates the
    opportunity, logs a progression note, and creates a next-stage action task.
    Refuses to advance a closed deal.
    """
    ident = _identity(_pipeline_push._scope)  # type: ignore[attr-defined]

    STAGE_ORDER = ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER", "CLOSED_WON"]

    ok_opp, opp = _pull(await _run("find_one_opportunity", {"id": opportunity_id}, ident))
    if not ok_opp:
        return _err("OPP_NOT_FOUND", str(opp))

    current_stage = opp.get("stage", "NEW")
    if current_stage in ("CLOSED_WON", "CLOSED_LOST"):
        return _err("ALREADY_CLOSED", f"Cannot advance a deal that is already {current_stage}. Use close_deal to manage closures.")

    try:
        current_idx = STAGE_ORDER.index(current_stage)
    except ValueError:
        current_idx = 0

    if current_idx >= len(STAGE_ORDER) - 1:
        return _err("AT_FINAL_STAGE", "Deal is already at the final active stage (CUSTOMER). Use close_deal to close it.")

    next_stage = STAGE_ORDER[current_idx + 1]

    import datetime
    task_due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).isoformat()

    next_stage_actions = {
        "SCREENING": "Complete initial qualification call",
        "MEETING": "Schedule and hold intro meeting",
        "PROPOSAL": "Prepare and send proposal/quote",
        "CUSTOMER": "Negotiate and finalise contract",
        "CLOSED_WON": "Close the deal",
    }
    task_title = next_stage_actions.get(next_stage, f"Advance to {next_stage}")

    ok_up, updated = _pull(await _run("update_opportunity", {"id": opportunity_id, "stage": next_stage}, ident))
    if not ok_up:
        return _err("UPDATE_FAILED", str(updated))

    note_body = f"Deal advanced: {current_stage} → {next_stage}." + (f"\n{notes}" if notes else "")
    note_r, task_r = await asyncio.gather(
        _run("create_note", {"title": "Stage Advanced", "body": note_body, "noteTargets": {"createMany": {"data": [{"opportunityId": opportunity_id}]}}}, ident),
        _run("create_task", {"title": task_title, "status": "TODO", "dueAt": task_due, "taskTargets": {"createMany": {"data": [{"opportunityId": opportunity_id}]}}}, ident),
    )

    return _ok({
        "action": "pipeline_push",
        "opportunity_id": opportunity_id,
        "deal_name": opp.get("name"),
        "previous_stage": current_stage,
        "new_stage": next_stage,
        "note_created": note_r.get("ok"),
        "task_created": task_r.get("ok"),
    })


async def _create_meeting_summary(
    company_id: str,
    attendees: str,
    summary: str,
    action_items: list[str] | None = None,
    follow_up_days: int = 7,
) -> dict:
    """Log a meeting summary note and create a task per action item.

    Creates one note with the full summary and attendee list, then one TODO
    task per action item with a due date ``follow_up_days`` from now.
    Pass action_items as a list of strings like
    ["Send pricing deck", "Intro to VP Engineering"].
    """
    ident = _identity(_create_meeting_summary._scope)  # type: ignore[attr-defined]
    action_items = action_items or []

    import datetime
    task_due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=follow_up_days)).isoformat()

    note_lines = [f"Meeting with {attendees}", "", summary]
    if action_items:
        note_lines += ["", "Action items:"] + [f"- {item}" for item in action_items]
    note_body = "\n".join(note_lines)

    targets = {"createMany": {"data": [{"companyId": company_id}]}}

    note_r = await _run("create_note", {"title": "Meeting Summary", "body": note_body, "noteTargets": targets}, ident)

    task_results = []
    for item in action_items:
        tr = await _run("create_task", {
            "title": item,
            "status": "TODO",
            "dueAt": task_due,
            "taskTargets": targets,
        }, ident)
        task_results.append({"item": item, "created": tr.get("ok")})

    return _ok({
        "action": "create_meeting_summary",
        "company_id": company_id,
        "note_created": note_r.get("ok"),
        "tasks_created": task_results,
        "task_due_date": task_due,
    })


async def _upsert_contact_at_company(
    person_first_name: str,
    person_last_name: str,
    company_name: str,
    email: str = "",
    job_title: str = "",
    phone: str = "",
) -> dict:
    """Find-or-create a person AND their company, then link them.

    Searches existing records before creating — safe to call repeatedly without
    making duplicates.  Updates the person's title and phone if provided and
    the record already exists.
    """
    ident = _identity(_upsert_contact_at_company._scope)  # type: ignore[attr-defined]

    people_r, companies_r = await asyncio.gather(
        _run(
            "find_people",
            find_tool_args(
                {
                    "name": {
                        "firstName": {"ilike": f"%{person_first_name}%"},
                        "lastName": {"ilike": f"%{person_last_name}%"},
                    }
                },
                limit=1,
            ),
            ident,
        ),
        _run("find_companies", find_tool_args({"name": {"ilike": f"%{company_name}%"}}, limit=1), ident),
    )

    existing_co = _edges(companies_r)
    if existing_co:
        company_id = existing_co[0]["id"]
        company_action = "found"
    else:
        ok_co, co = _pull(await _run("create_company", {"name": company_name}, ident))
        if not ok_co:
            return _err("CREATE_COMPANY_FAILED", str(co))
        company_id = co["id"]
        company_action = "created"

    existing_people = _edges(people_r)
    person_action: str
    if existing_people:
        person_id = existing_people[0]["id"]
        person_action = "found"
        # Update fields if provided
        updates: dict[str, Any] = {"company": {"connect": {"id": company_id}}}
        if job_title:
            updates["jobTitle"] = job_title
        if phone:
            updates["phones"] = {"primaryPhoneNumber": phone, "primaryPhoneCountryCode": "+1"}
        await _run("update_person", {"id": person_id, **updates}, ident)
    else:
        person_data: dict[str, Any] = {
            "name": {"firstName": person_first_name, "lastName": person_last_name},
            "company": {"connect": {"id": company_id}},
        }
        if email:
            person_data["emails"] = {"primaryEmail": email}
        if job_title:
            person_data["jobTitle"] = job_title
        if phone:
            person_data["phones"] = {"primaryPhoneNumber": phone, "primaryPhoneCountryCode": "+1"}
        ok_p, p = _pull(await _run("create_person", person_data, ident))
        if not ok_p:
            return _err("CREATE_PERSON_FAILED", str(p))
        person_id = p["id"]
        person_action = "created"

    return _ok({
        "action": "upsert_contact_at_company",
        "person_id": person_id,
        "person_action": person_action,
        "company_id": company_id,
        "company_action": company_action,
    })


async def _schedule_account_review(
    company_id: str,
    review_notes: str = "",
    days_from_now: int = 30,
) -> dict:
    """Schedule an account review task and log the agenda as a note.

    Creates a TODO task due in ``days_from_now`` days (default 30) and a note
    capturing the review agenda/context.  Use before quarterly reviews or when
    flagging an account for a health check.
    """
    ident = _identity(_schedule_account_review._scope)  # type: ignore[attr-defined]

    ok_co, company = _pull(await _run("find_one_company", {"id": company_id}, ident))
    if not ok_co:
        return _err("COMPANY_NOT_FOUND", str(company))

    import datetime
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_from_now)).isoformat()

    targets = {"createMany": {"data": [{"companyId": company_id}]}}
    note_body = f"Account review scheduled for {company.get('name', company_id)}." + (f"\n\nAgenda:\n{review_notes}" if review_notes else "")

    task_r, note_r = await asyncio.gather(
        _run("create_task", {"title": f"Account Review — {company.get('name', company_id)}", "status": "TODO", "dueAt": due, "taskTargets": targets}, ident),
        _run("create_note", {"title": "Account Review Scheduled", "body": note_body, "noteTargets": targets}, ident),
    )

    return _ok({
        "action": "schedule_account_review",
        "company_id": company_id,
        "review_due": due,
        "task_created": task_r.get("ok"),
        "note_created": note_r.get("ok"),
    })


async def _bulk_update_deal_stage(
    company_id: str,
    new_stage: str,
    reason: str = "",
    confirmation_token: str | None = None,
) -> dict:
    """Move ALL open opportunities for a company to a new stage at once.

    This is a bulk write — it requires confirmation.  On the first call (no
    token) it returns a ``CONFIRMATION_REQUIRED`` response with a preview of
    what will change.  Pass the token back to execute.
    """
    ident = _identity(_bulk_update_deal_stage._scope)  # type: ignore[attr-defined]

    VALID_STAGES = {"NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER", "CLOSED_WON", "CLOSED_LOST"}
    if new_stage not in VALID_STAGES:
        return _err("INVALID_STAGE", f"'{new_stage}' is not a valid stage. Valid: {sorted(VALID_STAGES)}")

    opps_r = await _run(
        "find_opportunities",
        find_tool_args(
            {"companyId": {"eq": company_id}, "stage": {"notIn": ["CLOSED_WON", "CLOSED_LOST"]}},
        ),
        ident,
    )
    open_opps = _edges(opps_r)

    if not open_opps:
        return _err("NO_OPEN_DEALS", f"No open opportunities found for company {company_id}")

    # First call — issue token and preview
    if not confirmation_token:
        preview = [{"id": o["id"], "name": o.get("name"), "current_stage": o.get("stage")} for o in open_opps]
        token = _issue_token("bulk_update_deal_stage", {
            "company_id": company_id,
            "new_stage": new_stage,
            "reason": reason,
            "opp_ids": [o["id"] for o in open_opps],
        })
        return {
            "ok": False,
            "error": {
                "code": "CONFIRMATION_REQUIRED",
                "message": f"About to move {len(open_opps)} open deal(s) to {new_stage}. Pass the confirmation_token to proceed.",
                "confirmation_token": token,
                "preview": preview,
            },
        }

    # Token provided — validate and execute
    ok_tok, err_msg, payload = _consume_token(confirmation_token, "bulk_update_deal_stage")
    if not ok_tok:
        return _err("INVALID_TOKEN", err_msg)

    opp_ids = payload.get("opp_ids", [o["id"] for o in open_opps])
    updated, failed = [], []
    for opp_id in opp_ids:
        ok_up, _ = _pull(await _run("update_opportunity", {"id": opp_id, "stage": new_stage}, ident))
        (updated if ok_up else failed).append(opp_id)
        # Note per deal
        note_body = f"Stage changed to {new_stage} as part of bulk update." + (f"\nReason: {reason}" if reason else "")
        await _run("create_note", {
            "title": "Bulk Stage Update",
            "body": note_body,
            "noteTargets": {"createMany": {"data": [{"opportunityId": opp_id}]}},
        }, ident)

    return _ok({
        "action": "bulk_update_deal_stage",
        "company_id": company_id,
        "new_stage": new_stage,
        "updated_count": len(updated),
        "failed_count": len(failed),
        "updated_ids": updated,
        "failed_ids": failed,
    })


async def _reassign_account(
    company_id: str,
    new_owner_id: str,
    reason: str = "",
) -> dict:
    """Transfer a company and all its open opportunities to a new owner.

    Updates the company's ``accountOwnerId``, updates ``assigneeId`` on every
    open opportunity, and creates a handoff note summarising the transfer.
    """
    ident = _identity(_reassign_account._scope)  # type: ignore[attr-defined]

    company_r, opps_r = await asyncio.gather(
        _run("find_one_company", {"id": company_id}, ident),
        _run(
            "find_opportunities",
            find_tool_args(
                {"companyId": {"eq": company_id}, "stage": {"notIn": ["CLOSED_WON", "CLOSED_LOST"]}},
            ),
            ident,
        ),
    )
    ok_co, company = _pull(company_r)
    if not ok_co:
        return _err("COMPANY_NOT_FOUND", str(company))

    open_opps = _edges(opps_r)

    # Update company + all opps in parallel
    update_calls = [
        _run("update_company", {"id": company_id, "accountOwnerId": new_owner_id}, ident)
    ] + [
        _run("update_opportunity", {"id": o["id"], "assigneeId": new_owner_id}, ident)
        for o in open_opps
    ]
    results = await asyncio.gather(*update_calls)
    ok_results = [r.get("ok") for r in results]

    note_body = (
        f"Account reassigned to owner {new_owner_id}.\n"
        f"Company: {company.get('name')}\n"
        f"Open deals transferred: {len(open_opps)}"
        + (f"\nReason: {reason}" if reason else "")
    )
    await _run("create_note", {
        "title": "Account Reassignment",
        "body": note_body,
        "noteTargets": {"createMany": {"data": [{"companyId": company_id}]}},
    }, ident)

    return _ok({
        "action": "reassign_account",
        "company_id": company_id,
        "new_owner_id": new_owner_id,
        "open_deals_reassigned": len(open_opps),
        "all_succeeded": all(ok_results),
    })


async def _send_proposal_followup(
    opportunity_id: str,
    proposal_summary: str,
    follow_up_days: int = 5,
) -> dict:
    """Advance deal to Proposal stage, log proposal details, schedule follow-up.

    Moves the deal to PROPOSAL, creates a note with the proposal summary, and
    schedules a follow-up task for ``follow_up_days`` days from now.
    """
    ident = _identity(_send_proposal_followup._scope)  # type: ignore[attr-defined]

    ok_opp, opp = _pull(await _run("find_one_opportunity", {"id": opportunity_id}, ident))
    if not ok_opp:
        return _err("OPP_NOT_FOUND", str(opp))

    import datetime
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=follow_up_days)).isoformat()
    targets = {"createMany": {"data": [{"opportunityId": opportunity_id}]}}

    update_r, note_r, task_r = await asyncio.gather(
        _run("update_opportunity", {"id": opportunity_id, "stage": "PROPOSAL"}, ident),
        _run("create_note", {"title": "Proposal Sent", "body": proposal_summary, "noteTargets": targets}, ident),
        _run("create_task", {"title": f"Follow up on proposal — {opp.get('name', opportunity_id)}", "status": "TODO", "dueAt": due, "taskTargets": targets}, ident),
    )

    return _ok({
        "action": "send_proposal_followup",
        "opportunity_id": opportunity_id,
        "deal_name": opp.get("name"),
        "stage_updated_to": "PROPOSAL",
        "stage_update_ok": update_r.get("ok"),
        "note_created": note_r.get("ok"),
        "follow_up_task_created": task_r.get("ok"),
        "follow_up_due": due,
    })


async def _convert_lead_to_opportunity(
    person_name_or_id: str,
    deal_name: str,
    estimated_value_micros: int = 0,
    currency_code: str = "USD",
) -> dict:
    """Convert a person (lead) into a full opportunity with a qualification task.

    Looks up the person, finds (or notes) their company, creates an opportunity
    linked to both, and creates a qualification task.  If the person is not yet
    linked to a company, the opportunity is created without a company link.
    """
    ident = _identity(_convert_lead_to_opportunity._scope)  # type: ignore[attr-defined]

    # Try ID lookup first, then name search
    person_r = await _run("find_one_person", {"id": person_name_or_id}, ident)
    if not person_r.get("ok"):
        first, *rest = person_name_or_id.strip().split()
        last = " ".join(rest) if rest else ""
        people_r = await _run(
            "find_people",
            find_tool_args(
                {"name": {"firstName": {"ilike": f"%{first}%"}, "lastName": {"ilike": f"%{last}%"}}},
                limit=1,
            ),
            ident,
        )
        people = _edges(people_r)
        if not people:
            return _err("PERSON_NOT_FOUND", f"Cannot find person '{person_name_or_id}'")
        person = people[0]
    else:
        person = person_r.get("data", {})

    person_id = person.get("id")
    company = person.get("company") or {}
    company_id = company.get("id")

    opp_data: dict[str, Any] = {
        "name": deal_name,
        "stage": "SCREENING",
        "amount": {"amountMicros": estimated_value_micros, "currencyCode": currency_code},
        "pointOfContact": {"connect": {"id": person_id}},
    }
    if company_id:
        opp_data["company"] = {"connect": {"id": company_id}}

    ok_opp, opp = _pull(await _run("create_opportunity", opp_data, ident))
    if not ok_opp:
        return _err("CREATE_OPP_FAILED", str(opp))
    opp_id = opp["id"]

    targets_data = [{"opportunityId": opp_id}]
    if company_id:
        targets_data.append({"companyId": company_id})

    import datetime
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).isoformat()

    task_r = await _run("create_task", {
        "title": f"Qualify lead — {person.get('name', {}).get('firstName', '')} {person.get('name', {}).get('lastName', '')}",
        "status": "TODO",
        "dueAt": due,
        "taskTargets": {"createMany": {"data": targets_data}},
    }, ident)

    return _ok({
        "action": "convert_lead_to_opportunity",
        "person_id": person_id,
        "company_id": company_id,
        "opportunity_id": opp_id,
        "qualification_task_created": task_r.get("ok"),
    })


async def _deal_lost_recovery(
    opportunity_id: str,
    recovery_strategy: str,
    follow_up_days: int = 60,
) -> dict:
    """Re-engage a lost deal: restore it, update with new strategy, schedule re-contact.

    Moves the deal from CLOSED_LOST back to SCREENING, creates a recovery note
    explaining the new approach, and schedules a follow-up task.
    """
    ident = _identity(_deal_lost_recovery._scope)  # type: ignore[attr-defined]

    ok_opp, opp = _pull(await _run("find_one_opportunity", {"id": opportunity_id}, ident))
    if not ok_opp:
        return _err("OPP_NOT_FOUND", str(opp))

    if opp.get("stage") not in ("CLOSED_LOST", "CLOSED_WON"):
        return _err("DEAL_NOT_CLOSED", "deal_lost_recovery only works on CLOSED_LOST deals.")

    import datetime
    due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=follow_up_days)).isoformat()
    targets = {"createMany": {"data": [{"opportunityId": opportunity_id}]}}

    update_r, note_r, task_r = await asyncio.gather(
        _run("update_opportunity", {"id": opportunity_id, "stage": "SCREENING"}, ident),
        _run("create_note", {
            "title": "Deal Recovery Initiated",
            "body": f"Deal re-opened for recovery.\n\nNew strategy: {recovery_strategy}",
            "noteTargets": targets,
        }, ident),
        _run("create_task", {
            "title": f"Re-engage — {opp.get('name', opportunity_id)}",
            "status": "TODO",
            "dueAt": due,
            "taskTargets": targets,
        }, ident),
    )

    return _ok({
        "action": "deal_lost_recovery",
        "opportunity_id": opportunity_id,
        "deal_name": opp.get("name"),
        "restored_to_stage": "SCREENING",
        "stage_update_ok": update_r.get("ok"),
        "note_created": note_r.get("ok"),
        "reengagement_task_created": task_r.get("ok"),
        "reengagement_due": due,
    })


async def _emergency_account_escalation(
    company_id: str,
    issue: str,
    escalation_details: str = "",
) -> dict:
    """Raise a CRM-level emergency escalation on an account.

    Creates a high-priority URGENT task (due today), an escalation note with
    full context, and flags all open opportunities with an escalation note.
    Use for churn risk, executive escalation requests, or critical incidents.
    """
    ident = _identity(_emergency_account_escalation._scope)  # type: ignore[attr-defined]

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.isoformat()

    company_r, opps_r = await asyncio.gather(
        _run("find_one_company", {"id": company_id}, ident),
        _run(
            "find_opportunities",
            find_tool_args(
                {"companyId": {"eq": company_id}, "stage": {"notIn": ["CLOSED_WON", "CLOSED_LOST"]}},
            ),
            ident,
        ),
    )
    ok_co, company = _pull(company_r)
    if not ok_co:
        return _err("COMPANY_NOT_FOUND", str(company))

    open_opps = _edges(opps_r)
    note_body = (
        f"🚨 ESCALATION — {company.get('name')}\n\n"
        f"Issue: {issue}\n"
        + (f"\nDetails: {escalation_details}" if escalation_details else "")
    )
    co_targets = {"createMany": {"data": [{"companyId": company_id}]}}

    # Urgent task + escalation note on company (parallel)
    task_r, note_r = await asyncio.gather(
        _run("create_task", {
            "title": f"🚨 ESCALATION — {company.get('name')}: {issue[:60]}",
            "status": "TODO",
            "dueAt": today,
            "taskTargets": co_targets,
        }, ident),
        _run("create_note", {"title": "Account Escalation", "body": note_body, "noteTargets": co_targets}, ident),
    )

    # Flag each open opp with an escalation note
    opp_note_results = []
    for opp in open_opps:
        opp_id = opp["id"]
        r = await _run("create_note", {
            "title": "Escalation Flag",
            "body": f"This deal is flagged under account escalation.\nIssue: {issue}",
            "noteTargets": {"createMany": {"data": [{"opportunityId": opp_id}]}},
        }, ident)
        opp_note_results.append({"opp_id": opp_id, "note_created": r.get("ok")})

    return _ok({
        "action": "emergency_account_escalation",
        "company_id": company_id,
        "company_name": company.get("name"),
        "issue": issue,
        "urgent_task_created": task_r.get("ok"),
        "escalation_note_created": note_r.get("ok"),
        "open_deals_flagged": len(open_opps),
        "deal_flag_results": opp_note_results,
    })


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_WRITE_WORKFLOW_FUNCS = [
    _change_company_budget,
    _onboard_new_client,
    _qualify_and_create_deal,
    _close_deal,
    _pipeline_push,
    _create_meeting_summary,
    _upsert_contact_at_company,
    _schedule_account_review,
    _bulk_update_deal_stage,
    _reassign_account,
    _send_proposal_followup,
    _convert_lead_to_opportunity,
    _deal_lost_recovery,
    _emergency_account_escalation,
]

_READ_WORKFLOW_FUNCS = [
    _account_health_check,
    _deal_risk_report,
]

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "_account_health_check": (
        "Full account health report for a company: health score (0–100), risk flags, "
        "overdue tasks, stale deals, last activity, and open pipeline. Pass company_id."
    ),
    "_deal_risk_report": (
        "Deep risk analysis for a single opportunity: staleness, missing fields, "
        "overdue tasks, risk level (low/medium/high), and actionable recommendations."
    ),
    "_change_company_budget": (
        "Update a company's annual recurring revenue (budget) and automatically log "
        "an audit note with before/after values. "
        "new_budget_micros in micros (1 USD = 1_000_000)."
    ),
    "_onboard_new_client": (
        "Full client onboarding workflow: creates company + person + opportunity + "
        "welcome note + 3 onboarding tasks (intro call, contract, kickoff) in one shot."
    ),
    "_qualify_and_create_deal": (
        "Find-or-create a person and company, then create a linked opportunity with a "
        "qualification note. Prevents duplicate records."
    ),
    "_close_deal": (
        "Close a deal (won or lost): advance stage, create closure summary note, "
        "schedule follow-up task. outcome must be 'won' or 'lost'."
    ),
    "_pipeline_push": (
        "Advance an opportunity by exactly one pipeline stage, log the reason as a note, "
        "and create the next-stage action task. Refuses to move a closed deal."
    ),
    "_create_meeting_summary": (
        "Log a meeting summary note and create one TODO task per action item. "
        "Pass action_items as a list of strings."
    ),
    "_upsert_contact_at_company": (
        "Find-or-create a person and their company, link them, and update fields. "
        "Safe to call repeatedly — never creates duplicates."
    ),
    "_schedule_account_review": (
        "Schedule an account review: creates a TODO task and an agenda note. "
        "days_from_now defaults to 30."
    ),
    "_bulk_update_deal_stage": (
        "Move ALL open opportunities for a company to a new stage at once. "
        "REQUIRES confirmation: first call returns a token; pass it back to execute."
    ),
    "_reassign_account": (
        "Transfer a company and all its open opportunities to a new owner. "
        "Updates accountOwnerId + assigneeId on every open deal + creates handoff note."
    ),
    "_send_proposal_followup": (
        "Advance deal to Proposal stage, log proposal details as a note, and schedule "
        "a follow-up task. follow_up_days defaults to 5."
    ),
    "_convert_lead_to_opportunity": (
        "Convert a person (lead) to an opportunity: finds the person (by ID or name), "
        "creates a linked opportunity at SCREENING stage, and creates a qualification task."
    ),
    "_deal_lost_recovery": (
        "Re-open a lost deal: restore to SCREENING, log recovery strategy note, "
        "schedule re-engagement task."
    ),
    "_emergency_account_escalation": (
        "Raise an emergency escalation on an account: creates an urgent task due TODAY, "
        "an escalation note, and flags all open opportunities. Use for churn risk or "
        "critical incidents."
    ),
}

_TOOL_NAMES: dict[str, str] = {
    "_account_health_check": "account_health_check",
    "_deal_risk_report": "deal_risk_report",
    "_change_company_budget": "change_company_budget",
    "_onboard_new_client": "onboard_new_client",
    "_qualify_and_create_deal": "qualify_and_create_deal",
    "_close_deal": "close_deal",
    "_pipeline_push": "pipeline_push",
    "_create_meeting_summary": "create_meeting_summary",
    "_upsert_contact_at_company": "upsert_contact_at_company",
    "_schedule_account_review": "schedule_account_review",
    "_bulk_update_deal_stage": "bulk_update_deal_stage",
    "_reassign_account": "reassign_account",
    "_send_proposal_followup": "send_proposal_followup",
    "_convert_lead_to_opportunity": "convert_lead_to_opportunity",
    "_deal_lost_recovery": "deal_lost_recovery",
    "_emergency_account_escalation": "emergency_account_escalation",
}


def build_workflow_tools(scope: ToolScope) -> list[StructuredTool]:
    """Return all workflow tools closed over *scope* for identity injection.

    Write workflows go into the WriterWorker's ``extra_tools``; read workflows
    go into the ReaderWorker's ``extra_tools``.  Pass the appropriate scope:
    ``WRITER_SCOPE`` for writes, ``READER_SCOPE`` for reads.

    To get only read or only write workflows use ``build_read_workflow_tools``
    or ``build_write_workflow_tools``.
    """
    all_funcs = _READ_WORKFLOW_FUNCS + _WRITE_WORKFLOW_FUNCS
    tools = []
    for fn in all_funcs:
        fn._scope = scope  # type: ignore[attr-defined]
        tools.append(
            StructuredTool.from_function(
                coroutine=fn,
                name=_TOOL_NAMES[fn.__name__],
                description=_TOOL_DESCRIPTIONS[fn.__name__],
            )
        )
    return tools


def build_read_workflow_tools(scope: ToolScope) -> list[StructuredTool]:
    """Read-only workflow tools for the ReaderWorker."""
    tools = []
    for fn in _READ_WORKFLOW_FUNCS:
        fn._scope = scope  # type: ignore[attr-defined]
        tools.append(
            StructuredTool.from_function(
                coroutine=fn,
                name=_TOOL_NAMES[fn.__name__],
                description=_TOOL_DESCRIPTIONS[fn.__name__],
            )
        )
    return tools


def build_write_workflow_tools(scope: ToolScope) -> list[StructuredTool]:
    """Write workflow tools for the WriterWorker."""
    tools = []
    for fn in _WRITE_WORKFLOW_FUNCS:
        fn._scope = scope  # type: ignore[attr-defined]
        tools.append(
            StructuredTool.from_function(
                coroutine=fn,
                name=_TOOL_NAMES[fn.__name__],
                description=_TOOL_DESCRIPTIONS[fn.__name__],
            )
        )
    return tools
