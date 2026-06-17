# Negotiation Stage Playbook

## Goal of this stage

Resolve remaining objections (price, terms, legal), get executive sponsor
alignment, and drive to a signed agreement.

## Entry criteria

- Proposal sent and reviewed; buyer has expressed intent to move forward
  pending specific concerns.

## Exit criteria (move to Closed Won)

- All material objections addressed and documented.
- Legal/procurement review complete.
- Executive sponsor sign-off obtained.
- Contract sent for signature.

## Recommended next actions by signal

- **Objection noted in timeline (price, terms, scope)** →
  `create_task`: address the specific objection directly — prepare a
  response, alternative pricing, or scope adjustment, and schedule a call to
  discuss.
- **No executive sponsor engaged** → `schedule_meeting`: bring in an
  executive sponsor on the vendor side to match the buyer's decision maker.
- **Legal/procurement review pending** → `create_task`: track legal review
  as a task with a due date to avoid silent stalls.
- **Engagement gap (>= 7 days since last activity)** → direct follow-up to
  confirm the deal hasn't stalled in legal/procurement.
- **No future meeting scheduled** → `schedule_meeting`: set a checkpoint
  call to review contract status.

## Common mistakes to avoid

- Treating silence from procurement/legal as "no news is good news" —
  proactively check status.
- Conceding on price without addressing the underlying objection.
