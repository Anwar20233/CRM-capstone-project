# Sales Best Practices

General guidance the Next Step Intelligence Agent can draw on across stages.

## Engagement cadence

- Deals with no activity for 7+ days are at risk of stalling. The next
  action should almost always include direct outreach (call, email, or
  meeting request) when `engagement.days_since_last_activity >= 7`.
- A deal without a scheduled future meeting (`has_future_meeting = false`)
  in an open stage should have "schedule a meeting" as one of the top
  recommendations.

## Multi-threading

- Deals with only one contact, or no contact flagged `is_decision_maker`,
  are higher risk. Recommend identifying and engaging additional
  stakeholders, especially economic buyers and technical evaluators.

## Task hygiene

- Overdue tasks signal stalled momentum. If `tasks` contains any item with
  `is_overdue = true`, recommend addressing it directly (complete it,
  reschedule it, or replace it with a more relevant next step).

## Evidence-based recommendations

- Every recommendation must cite specific facts: a timeline entry, an
  engagement metric, a contact's role, or a retrieved playbook/BANT
  reference. Avoid generic advice like "follow up with the customer" without
  tying it to *why now* and *based on what*.

## Action specificity

- Prefer concrete, schedulable actions (`schedule_demo`, `send_proposal`,
  `create_task`, `qualify_budget`, `schedule_meeting`) over vague guidance.
- `suggested_crm_instruction` should be specific enough that a Writer agent
  could execute it without follow-up questions (who, what, on which
  opportunity).
