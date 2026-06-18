# Qualification Stage Playbook

## Goal of this stage

Confirm all four BANT dimensions before committing to a full proposal. A deal
in Qualification should not advance until Budget, Authority, Need, and Timeline
are at least partially confirmed.

## Entry criteria

- Opportunity created and at least one stakeholder identified.
- Initial discovery call or contact logged.

## Exit criteria (move to Proposal)

- Budget range confirmed or directionally aligned with deal size.
- Decision maker identified and actively engaged.
- Business need (pain point / desired outcome) documented in the CRM.
- Target close date or decision timeline established.

## Recommended next actions by signal

- **Budget gap** → `qualify_budget`: ask directly about budget range and
  approval process in the next call or email.
- **Authority gap (no decision maker)** → `qualify_authority` or
  `schedule_meeting`: get the economic buyer into a conversation before
  advancing to Proposal.
- **Need gap** → `qualify_need`: schedule or run a structured discovery
  conversation; document the buyer's problem in their own words.
- **Timeline gap** → `qualify_timeline`: confirm when the buyer wants a
  solution and work backwards to a realistic close date.
- **No future meeting scheduled** → `schedule_meeting`: ensure forward
  momentum by booking the next touchpoint now.
- **Engagement gap (>= 7 days since last activity)** → direct outreach to
  re-engage before the deal stalls.
- **All BANT dimensions confirmed** → `update_opportunity` to advance the
  stage to Proposal + `create_task` to trigger proposal preparation.

## Common mistakes to avoid

- Advancing to Proposal with fewer than 3 of 4 BANT dimensions confirmed.
- Treating a champion's enthusiasm as economic buyer authority.
- Moving forward without a documented, buyer-driven timeline.
