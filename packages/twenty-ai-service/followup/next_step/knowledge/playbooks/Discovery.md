# Discovery Stage Playbook

## Goal of this stage

Confirm the opportunity is worth pursuing: there is a real problem (Need),
someone who can buy (Authority), a plausible budget (Budget), and a reason
to act now (Timeline).

## Entry criteria

- Opportunity created or qualified from an inbound/outbound lead.
- At least one contact identified.

## Exit criteria (move to Proposal)

- Pain point / business driver documented in notes.
- Decision maker or champion identified (`is_decision_maker = true` on at
  least one contact, or a clear path to one).
- Rough budget range discussed.
- Target timeline / close date established.

## Recommended next actions by signal

- **No demo or discovery call yet on the timeline** → `schedule_demo`:
  schedule a discovery call or product demo to uncover requirements.
- **No contact flagged as decision maker** → `qualify_authority`: identify
  and engage the economic buyer or champion.
- **No documented pain point** → `qualify_need`: run a structured discovery
  conversation; document the problem in the buyer's words.
- **No budget signal** → `qualify_budget`: ask about budget range and
  approval process.
- **No close date or target timeline** → `qualify_timeline`: confirm when
  the buyer wants a solution in place.
- **opportunity_created event with no recent activity** → `create_task`:
  create a task to schedule the first discovery call within 2 business days.

## Common mistakes to avoid

- Moving to Proposal before Budget and Need are at least partially
  qualified.
- Sending pricing/proposals to a single non-decision-maker contact.
