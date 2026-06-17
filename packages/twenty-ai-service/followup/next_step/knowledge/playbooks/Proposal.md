# Proposal Stage Playbook

## Goal of this stage

Deliver a proposal that maps directly to the documented need, confirm budget
fit, and get a commitment to a decision timeline.

## Entry criteria

- Need, rough budget, and at least one engaged stakeholder confirmed from
  Discovery.

## Exit criteria (move to Negotiation)

- Proposal/pricing sent and acknowledged by the buyer.
- Budget confirmed as sufficient (or escalation path identified).
- Decision-making process and stakeholders mapped.
- Target close date confirmed or refined.

## Recommended next actions by signal

- **Stage just changed to Proposal, no proposal sent yet** →
  `send_proposal`: prepare and send a proposal tailored to the documented
  need and budget range.
- **opportunity_stage_changed into Proposal but Budget/Need still
  unqualified** → prioritize `qualify_budget` or `qualify_need` before
  `send_proposal` — sending a proposal without qualification often results
  in stalled deals.
- **No decision maker engaged** → `schedule_meeting`: get the economic buyer
  into a proposal walkthrough.
- **No future meeting scheduled (`has_future_meeting = false`)** →
  `schedule_meeting`: set a follow-up meeting to review the proposal and
  next steps.
- **meeting_completed event during Proposal** → `create_task`: log a
  follow-up task summarizing decisions/objections and the agreed next step.
- **Engagement gap (>= 7 days since last activity)** → `schedule_meeting` or
  a direct follow-up email/call to re-engage before the deal goes cold.

## Common mistakes to avoid

- Re-sending the same proposal without addressing prior objections.
- Letting a proposal sit with no scheduled follow-up.
