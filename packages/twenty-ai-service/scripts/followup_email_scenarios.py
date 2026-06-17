"""A library of new inbound-email scenarios to run the Follow-Up agent against.

Each scenario uses a seeded sender (so the reader resolves them to a real person
→ company → deal) and is written to exercise a different agent behavior: new
stakeholders / shadow promotion, competitor & budget extraction, champion
hand-off relationships, strong buying signals, the ambiguous-deal halt, and the
unknown-sender halt. The bodies carry real-looking PII (new names) so they also
exercise the PII masking layer.

Used by ``followup_e2e.py`` (``--scenario <name>`` / ``--list``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailScenario:
    name: str
    sender: str
    subject: str
    body: str
    exercises: str  # one-line note on what behavior this is meant to trigger


SCENARIOS: dict[str, EmailScenario] = {
    "airbnb_new_stakeholder": EmailScenario(
        name="airbnb_new_stakeholder",
        sender="john.park@airbnb.com",
        subject="Re: Airbnb Platform Integration — next steps",
        body=(
            "Hi Sarah,\n\n"
            "Thanks for the revised proposal for the Platform Integration. I'm "
            "concerned about the integration timeline — Q3 is tight given our "
            "engineering freeze in August. Our new VP of Engineering, Rachel "
            "Torres, will need to sign off on the security review before we can "
            "commit; she's really the decision maker on infrastructure now.\n\n"
            "Budget-wise we have approval for up to $250k for this phase. I should "
            "also mention we're evaluating Segment as an alternative, mostly on "
            "price. Can we get a revised SOW by June 25th? Lisa will loop in our "
            "procurement lead, David Kim, as well.\n\nBest,\nJohn"
        ),
        exercises="new authority shadow (Rachel) → auto-promote; competitor/budget/deadline; procurement shadow (David Kim)",
    ),
    "stripe_champion_transition": EmailScenario(
        name="stripe_champion_transition",
        sender="alex.rivera@stripe.com",
        subject="Handing off the Analytics Suite eval",
        body=(
            "Hi Sarah,\n\n"
            "Quick heads-up: I'm moving to a new team internally, so I'm handing "
            "the Analytics Suite evaluation over to Tom Becker, our new Head of "
            "Data. Tom will own the technical decision from here — he reports to "
            "our CTO and is fairly hands-on.\n\n"
            "I still think the product is strong, but Tom wants to re-run the "
            "benchmark before we commit. Please add tom.becker@stripe.com to the "
            "thread.\n\nThanks for everything,\nAlex"
        ),
        exercises="champion leaving (relationship change); new decision-maker shadow (Tom Becker) + reports_to CTO",
    ),
    "notion_pricing_risk": EmailScenario(
        name="notion_pricing_risk",
        sender="kevin.cho@notion.com",
        subject="Re: Workflow Automation — pricing concerns",
        body=(
            "Hi Marcus,\n\n"
            "Honestly the pricing came in higher than we budgeted. We're now also "
            "looking at Airtable, which is meaningfully cheaper for our use case.\n\n"
            "For a deal this size our CFO, Dieter Voss, has to approve, and he's "
            "skeptical about ROI. If you can get the annual cost under $90k I think "
            "we can keep this alive — otherwise it's at risk.\n\nKevin"
        ),
        exercises="negative concern + competitor (Airtable); budget; skeptical authority shadow (CFO Dieter Voss)",
    ),
    "figma_buying_signal": EmailScenario(
        name="figma_buying_signal",
        sender="emma.larsen@figma.com",
        subject="Ready to move forward on Design Collaboration",
        body=(
            "Hi Sarah,\n\n"
            "Great news — the team loved the pilot and we're ready to move forward "
            "with the Design Collaboration Platform. I've got verbal sign-off from "
            "leadership.\n\n"
            "Next step is legal: our counsel, Priya Nair, will review the MSA this "
            "week. If redlines are minor we'd like to sign before end of quarter. "
            "Can you send the order form?\n\nExcited,\nEmma"
        ),
        exercises="strong buying signal + commitment, positive sentiment; new legal shadow (Priya Nair); deadline",
    ),
    "stripe_vague_checkin": EmailScenario(
        name="stripe_vague_checkin",
        sender="alex.rivera@stripe.com",
        subject="Checking in",
        body=(
            "Hi Sarah,\n\n"
            "Just circling back after a busy few weeks. Hope you're well. Let's "
            "find time to reconnect soon — happy to grab a coffee.\n\nAlex"
        ),
        exercises="no deal signal while the company has several open deals → ambiguous-opportunity halt",
    ),
    "unknown_sender": EmailScenario(
        name="unknown_sender",
        sender="dana.fischer@brandnewco.example",
        subject="Interested in your platform",
        body=(
            "Hello,\n\n"
            "I came across your product and we might be interested for our team at "
            "BrandNewCo. Could you share pricing?\n\nThanks,\nDana Fischer"
        ),
        exercises="sender not in CRM → unknown-sender halt (no extraction)",
    ),
}


def get(name: str) -> EmailScenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    return SCENARIOS[name]
