# Closed Stage Playbook (Closed Won / Closed Lost)

## Scope note

The Next Step Intelligence Agent **skips** opportunities in `Closed Won` or
`Closed Lost` (see `agent.py: _skip_reason`). This playbook exists for
completeness and for other agents (e.g. Risk, Drafting) that may still
operate on closed deals (for example, generating a meeting recap after a
deal closes).

## Closed Won

- Trigger onboarding / handoff to customer success (outside Follow-Up scope
  v1 — surfaced via CRM workflows, not this agent).
- Any `meeting_completed` event after Closed Won may still produce a
  `meeting_recap_email` draft (Drafting Agent), but no next-step
  recommendations are generated.

## Closed Lost

- No next-step recommendations are generated.
- Risk Agent may still log a `derived_insights` profile fact summarizing the
  loss reason for future reference, but this is out of scope for the Next
  Step Agent.

## Why no recommendations are generated

Recommending sales actions (schedule demo, send proposal, qualify budget) on
a deal that is already won or lost would be noise for the rep and could
trigger unwanted CRM writes if accepted. The skip is enforced in
`_skip_reason` and returns `skipped=True` with a clear `skip_reason`.
