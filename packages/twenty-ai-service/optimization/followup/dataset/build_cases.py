"""Build the next-step gold dataset for DSPy optimization.

Constructs ``DealContext`` objects with the real Pydantic models (so every case
is schema-valid by construction), pairs each with a trigger email and a gold
label, assigns a train/val/test split, and writes ``next_step_cases.json``.

Gold labels mirror the dimensions the production eval already grades
(``scripts/followup_eval.py``): the *orchestrator tool* of the headline action,
its *urgency band*, and whether the email warrants *no action* at all. They are
deliberately encoded as accepted *sets* (several actions can be defensible),
exactly like ``EXPECTATIONS`` in ``scripts/followup_email_scenarios.py``.

    python optimization/followup/dataset/build_cases.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Service root (packages/twenty-ai-service) on sys.path so ``followup`` imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from followup.next_step.context.schemas import (
    CompanySnapshot,
    ContactSnapshot,
    DealContext,
    EngagementMetrics,
    OpportunitySnapshot,
    ProfileFact,
    TimelineItem,
)

_OUT = Path(__file__).resolve().parent / "next_step_cases.json"
_NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _ago(days: int) -> datetime:
    return _NOW - timedelta(days=days)


def _context(
    *,
    opp_id: str,
    name: str,
    stage: str,
    company: str,
    contacts: list[ContactSnapshot],
    timeline: list[TimelineItem],
    engagement: EngagementMetrics,
    facts: list[ProfileFact] | None = None,
    amount: float | None = 80000.0,
    close_in_days: int | None = 30,
) -> DealContext:
    return DealContext(
        opportunity=OpportunitySnapshot(
            id=opp_id,
            name=name,
            stage=stage,
            amount=amount,
            close_date=_NOW + timedelta(days=close_in_days) if close_in_days is not None else None,
            company_id=f"{opp_id}-co",
        ),
        company=CompanySnapshot(id=f"{opp_id}-co", name=company, industry="Software"),
        contacts=contacts,
        timeline=timeline,
        engagement=engagement,
        active_facts=facts or [],
        loaded_at=_NOW,
    )


def _tl(type_: str, title: str, summary: str, days_ago: int) -> TimelineItem:
    return TimelineItem(type=type_, title=title, summary=summary, occurred_at=_ago(days_ago))


def _contact(cid: str, name: str, role: str, dm: bool = False) -> ContactSnapshot:
    return ContactSnapshot(id=cid, name=name, role=role, is_decision_maker=dm)


# ---------------------------------------------------------------------------
# Synthetic archetypes (one per common follow-up situation)
# ---------------------------------------------------------------------------

def _synthetic_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # 1. Deal going cold — long silence, downward trend, at-risk. HIGH urgency.
    cases.append({
        "id": "ns_cold_silence",
        "split": "train",
        "category": "at_risk",
        "event_type": "activity_logged",
        "context": _context(
            opp_id="opp-cold",
            name="Northwind — Platform Integration",
            stage="Proposal",
            company="Northwind",
            contacts=[_contact("c-john", "John Park", "Director of Eng", dm=True)],
            timeline=[
                _tl("email", "Proposal sent", "Sent pricing + rollout plan, no reply.", 35),
                _tl("email", "Check-in", "Nudged for feedback, silence.", 18),
            ],
            engagement=EngagementMetrics(
                days_since_last_activity=18,
                activity_count_14d=0,
                activity_count_prior_14d=3,
                has_future_meeting=False,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Re: rollout plan\n\n(no inbound — proactive sweep: the buyer "
            "has gone silent for 18 days after we sent the proposal.)"
        ),
        "gold": {
            "tools": ["send_email", "create_task", "schedule_meeting"],
            "urgency": ["high", "medium"],
            "expect_no_action": False,
        },
    })

    # 2. Buyer asked a concrete question — written reply is the move. MEDIUM.
    cases.append({
        "id": "ns_question_only",
        "split": "train",
        "category": "inbound_question",
        "event_type": "email_sent",
        "context": _context(
            opp_id="opp-q",
            name="Initech — Analytics Rollout",
            stage="Qualification",
            company="Initech",
            contacts=[_contact("c-amy", "Amy Chen", "Data Lead", dm=False)],
            timeline=[_tl("email", "Demo follow-up", "Walked through analytics module.", 2)],
            engagement=EngagementMetrics(
                days_since_last_activity=0,
                activity_count_14d=4,
                activity_count_prior_14d=2,
                has_future_meeting=False,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Quick question on data residency\n\n"
            "Hi — before we go further, can you confirm whether data can be pinned "
            "to the EU region? That's the one open item from our side. Thanks, Amy"
        ),
        "gold": {
            "tools": ["send_email"],
            "urgency": ["medium", "low"],
            "expect_no_action": False,
        },
    })

    # 3. Positive momentum, no deadline — good news is NOT high urgency. LOW/MED.
    cases.append({
        "id": "ns_positive_momentum",
        "split": "train",
        "category": "positive_momentum",
        "event_type": "meeting_completed",
        "context": _context(
            opp_id="opp-pos",
            name="Hooli — Pilot Expansion",
            stage="Proposal",
            company="Hooli",
            contacts=[_contact("c-gav", "Gavin Belson", "VP Product", dm=True)],
            timeline=[_tl("meeting", "Pilot review", "Pilot exceeded targets, team happy.", 1)],
            engagement=EngagementMetrics(
                days_since_last_activity=1,
                activity_count_14d=6,
                activity_count_prior_14d=4,
                has_future_meeting=True,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Pilot went great\n\n"
            "Just wanted to say the pilot results look strong — the team is "
            "impressed. We'll loop back internally on next steps. No action needed "
            "from you right now."
        ),
        "gold": {
            "tools": ["send_email", "log_activity", "create_task"],
            "urgency": ["low", "medium"],
            "expect_no_action": False,
        },
    })

    # 4. Pure FYI / "no reply needed" — the NO-ACTION case.
    cases.append({
        "id": "ns_fyi_no_action",
        "split": "val",
        "category": "no_action",
        "event_type": "email_sent",
        "context": _context(
            opp_id="opp-fyi",
            name="Stark — Renewal",
            stage="Negotiation",
            company="Stark Industries",
            contacts=[_contact("c-pep", "Pepper Potts", "COO", dm=True)],
            timeline=[_tl("email", "Contract sent", "Sent redlines for legal review.", 3)],
            engagement=EngagementMetrics(
                days_since_last_activity=0,
                activity_count_14d=5,
                activity_count_prior_14d=5,
                has_future_meeting=True,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Out of office\n\n"
            "I'm out until Monday with limited email access. No need to reply — "
            "I'll pick this back up when I'm back. — Pepper"
        ),
        "gold": {
            "tools": ["log_activity"],
            "urgency": ["low"],
            "expect_no_action": True,
        },
    })

    # 5. Competitor actively displacing — HIGH urgency.
    cases.append({
        "id": "ns_competitor_pressure",
        "split": "train",
        "category": "competitor",
        "event_type": "email_sent",
        "context": _context(
            opp_id="opp-comp",
            name="Umbrella — Security Platform",
            stage="Proposal",
            company="Umbrella Corp",
            contacts=[_contact("c-al", "Alice Wong", "CISO", dm=True)],
            timeline=[_tl("email", "Eval update", "Buyer comparing two vendors.", 4)],
            engagement=EngagementMetrics(
                days_since_last_activity=1,
                activity_count_14d=7,
                activity_count_prior_14d=5,
                has_future_meeting=False,
            ),
            facts=[ProfileFact(
                fact_id="f-comp", category="risk", fact_key="competitor",
                value="Evaluating CompetitorX in parallel; price-sensitive.", confidence=0.9,
            )],
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Re: final evaluation\n\n"
            "Heads up — leadership is leaning toward CompetitorX on price. If you "
            "can address the cost gap this week we can still make the case for you, "
            "but the decision meeting is Friday."
        ),
        "gold": {
            "tools": ["send_email", "schedule_meeting", "create_task"],
            "urgency": ["high"],
            "expect_no_action": False,
        },
    })

    # 6. Buyer explicitly proposes times for a call — schedule a meeting. MED/HIGH.
    cases.append({
        "id": "ns_meeting_request",
        "split": "train",
        "category": "meeting_request",
        "event_type": "email_sent",
        "context": _context(
            opp_id="opp-mtg",
            name="Wayne — Infra Modernization",
            stage="Qualification",
            company="Wayne Enterprises",
            contacts=[_contact("c-luc", "Lucius Fox", "CTO", dm=True)],
            timeline=[_tl("email", "Intro", "Initial scoping email exchange.", 5)],
            engagement=EngagementMetrics(
                days_since_last_activity=1,
                activity_count_14d=3,
                activity_count_prior_14d=1,
                has_future_meeting=False,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Let's set up a working session\n\n"
            "Can we get 45 minutes next week to walk through the architecture? "
            "Tuesday or Wednesday afternoon both work on my end."
        ),
        "gold": {
            "tools": ["schedule_meeting"],
            "urgency": ["medium", "high"],
            "expect_no_action": False,
        },
    })

    # 7. Stated hard deadline within days — HIGH.
    cases.append({
        "id": "ns_deadline_soon",
        "split": "val",
        "category": "deadline",
        "event_type": "email_sent",
        "context": _context(
            opp_id="opp-deadline",
            name="Cyberdyne — Q3 Rollout",
            stage="Negotiation",
            company="Cyberdyne",
            contacts=[_contact("c-sar", "Sarah Connor", "Procurement", dm=True)],
            timeline=[_tl("email", "Quote", "Sent final quote awaiting PO.", 2)],
            engagement=EngagementMetrics(
                days_since_last_activity=0,
                activity_count_14d=6,
                activity_count_prior_14d=4,
                has_future_meeting=False,
            ),
            close_in_days=5,
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Need the signed order by Thursday\n\n"
            "Our budget cycle closes Thursday EOD. If we don't have the paperwork "
            "finalized by then we lose the allocation this quarter. What do you "
            "need from me to get this over the line?"
        ),
        "gold": {
            "tools": ["send_email", "create_task", "schedule_meeting"],
            "urgency": ["high"],
            "expect_no_action": False,
        },
    })

    # 8. Routine cadence check-in, healthy deal — LOW/MED.
    cases.append({
        "id": "ns_routine_checkin",
        "split": "test",
        "category": "routine",
        "event_type": "activity_logged",
        "context": _context(
            opp_id="opp-routine",
            name="Soylent — Expansion",
            stage="Discovery",
            company="Soylent Corp",
            contacts=[_contact("c-joe", "Joe Roan", "Ops Manager", dm=False)],
            timeline=[_tl("note", "Discovery call", "Mapped current workflow.", 7)],
            engagement=EngagementMetrics(
                days_since_last_activity=7,
                activity_count_14d=2,
                activity_count_prior_14d=2,
                has_future_meeting=False,
            ),
        ).model_dump(mode="json"),
        "trigger": (
            "Subject: Re: discovery follow-up\n\n"
            "Thanks for the call last week — useful overview. We're still early in "
            "our planning; circle back in a couple weeks?"
        ),
        "gold": {
            "tools": ["send_email", "create_task", "create_reminder", "log_activity"],
            "urgency": ["low", "medium"],
            "expect_no_action": False,
        },
    })

    return cases


# ---------------------------------------------------------------------------
# Fixture-backed cases (reuse the two committed next-step fixtures)
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parents[3] / "followup/next_step/tests/fixtures"


def _fixture_cases() -> list[dict[str, Any]]:
    discovery = json.loads((_FIXTURES / "next_step_context_discovery.json").read_text())
    proposal = json.loads((_FIXTURES / "next_step_context_proposal.json").read_text())
    return [
        {
            "id": "ns_fixture_discovery_authority",
            "split": "train",
            "category": "qualification_gap",
            "event_type": "opportunity_stage_changed",
            "context": discovery,
            "trigger": (
                "Subject: Re: next steps\n\n"
                "Sounds good — what would you need from us to move forward? Happy to "
                "pull in whoever should be involved."
            ),
            "gold": {
                # No decision maker flagged → qualify authority / pull DM into a call.
                "tools": ["schedule_meeting", "send_email", "create_task"],
                "urgency": ["medium", "high"],
                "expect_no_action": False,
            },
        },
        {
            "id": "ns_fixture_proposal_advance",
            "split": "test",
            "category": "proposal_advance",
            "event_type": "opportunity_stage_changed",
            "context": proposal,
            "trigger": (
                "Subject: Re: proposal\n\n"
                "Proposal looks solid. A couple of line items we want to confirm — "
                "can you send a short summary we can take to finance?"
            ),
            "gold": {
                "tools": ["send_email", "schedule_meeting"],
                "urgency": ["medium", "high"],
                "expect_no_action": False,
            },
        },
    ]


def build() -> list[dict[str, Any]]:
    cases = _synthetic_cases() + _fixture_cases()
    # Validate every inline context round-trips through the real model.
    for case in cases:
        DealContext.model_validate(case["context"])
    return cases


def main() -> None:
    cases = build()
    _OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["split"]] = counts.get(case["split"], 0) + 1
    print(f"Wrote {len(cases)} next-step cases -> {_OUT}")
    print(f"splits: {counts}")


if __name__ == "__main__":
    main()
