"""Live smoke for the follow-up SUBAGENT adapters (no bridge / no DB).

Exercises the anti-corruption layer end to end against the real subagent model
(Qwen by default): builds an in-memory DealContext from a seeded email scenario,
runs the next-step adapter (→ a NextStepPlan), then for each draft_email/
book_meeting step runs the drafting adapter (→ a DraftResult). This is the part
of the orchestrator that does NOT need the reader/calendar bridge, so it runs
with just LLM creds.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_subagents_smoke.py
    .venv/bin/python scripts/followup_subagents_smoke.py --scenario figma_buying_signal
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from scripts.followup_email_scenarios import SCENARIOS, get  # noqa: E402

from followup.agents.bundle import subagent_model  # noqa: E402
from followup.agents.drafting_adapter import OrchestratorDraftingAgent  # noqa: E402
from followup.agents.next_step_adapter import OrchestratorNextStepAgent  # noqa: E402
from followup.contracts.drafting import DraftRequest  # noqa: E402
from followup.contracts.events import EmailSignalEvent  # noqa: E402
from followup.contracts.next_step import NextStepRequest  # noqa: E402
from followup.profile.schemas import ContactSummary, DealContext  # noqa: E402


def _deal_for(scenario) -> DealContext:
    """A plausible deal picture for the scenario's sender (what load_profile would build)."""
    sender = scenario.sender
    name = sender.split("@")[0].replace(".", " ").title()
    company = sender.split("@")[1].split(".")[0].title()
    return DealContext(
        opportunity_id=str(uuid.uuid4()),
        opportunity_name=f"{company} Platform Deal",
        deal_stage="PROPOSAL",
        deal_value=250_000.0,
        company_name=company,
        profile_narrative=(
            f"{company} is mid-cycle on a platform deal. Primary contact {name}. "
            "Recent thread raised timeline and budget topics; a competitor is in play."
        ),
        contacts=[
            ContactSummary(crm_id=str(uuid.uuid4()), name=name, role="VP", email=sender, facts=[])
        ],
        recent_activities=[
            {"type": "note", "date": "2026-06-10T10:00:00+00:00", "summary": "Sent revised proposal"},
            {"type": "meeting", "date": "2026-06-05T15:00:00+00:00", "summary": "Technical deep-dive call"},
        ],
        key_relationships=[],
        open_concerns=[{"id": "c1", "content": "Integration timeline is tight for Q3", "fact_type": "concern"}],
        risk_score=0.55,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="airbnb_new_stakeholder", choices=sorted(SCENARIOS))
    args = parser.parse_args()
    scenario = get(args.scenario)
    deal = _deal_for(scenario)
    model = subagent_model()

    print("=" * 78)
    print(f"  SUBAGENT SMOKE — scenario={scenario.name}  subagent_model={model}")
    print("=" * 78)
    print(f"  sender : {scenario.sender}")
    print(f"  deal   : {deal.opportunity_name} (stage {deal.deal_stage}, ${deal.deal_value:,.0f})")
    print(f"  email  : {scenario.subject}")

    classification = {"type": "objection", "urgency": "high", "requires_calendar": False}
    trigger = EmailSignalEvent(
        sender_email=scenario.sender, subject=scenario.subject, body=scenario.body,
        received_at="2026-06-17T09:00:00+00:00", opportunity_id=deal.opportunity_id,
    )

    print("\n[1] next-step adapter → NextStepPlan")
    plan = await OrchestratorNextStepAgent(model=model).run(
        NextStepRequest(
            deal_context=deal, trigger_type="email_signal", trigger=trigger,
            narrative=deal.profile_narrative, classification=classification,
        )
    )
    print(f"    headline : {plan.headline_action}")
    print(f"    summary  : {plan.summary}")
    for step in plan.steps:
        print(f"      · {step.kind} ({step.priority}): {step.intent}")
        if step.metadata.get("approach"):
            print(f"          approach: {step.metadata['approach']}")
        if step.metadata.get("evidence"):
            print(f"          evidence: {step.metadata['evidence']}")

    email_steps = [s for s in plan.steps if s.kind in ("draft_email", "book_meeting")]
    if not email_steps:
        print("\n[2] no email step in plan — nothing to draft")
        return

    print("\n[2] drafting adapter → DraftResult (per email step)")
    drafter = OrchestratorDraftingAgent(model=model)
    for step in email_steps:
        draft = await drafter.run(
            DraftRequest(
                deal_context=deal, intent=step.intent, classification=classification,
                recipient_email=deal.contacts[0].email,
                reply_context={"sender_email": scenario.sender, "subject": scenario.subject, "body": scenario.body},
            )
        )
        print(f"    subject : {draft.subject}")
        print(f"    to      : {draft.recipient_email}  tone={draft.tone}  type={draft.metadata.get('draft_type')}")
        print("    body    :")
        print("        " + (draft.body or "").replace("\n", "\n        "))
        print()


if __name__ == "__main__":
    asyncio.run(main())
