# BANT Qualification Framework

BANT is used to qualify whether an opportunity is likely to close and what
to verify before moving it to the next stage. The Next Step Intelligence
Agent uses this framework to recommend qualification actions when gaps are
detected in `DealContext`.

## Budget

- Has the buyer confirmed a budget range or approved spend for this purchase?
- Does the deal `amount` align with what the buyer has indicated they can spend?
- **Gap signal:** No note or profile fact mentions budget, or `amount` is unset.
- **Recommended action when missing:** `qualify_budget` — ask directly about
  budget range and approval process in the next call or email.

## Authority

- Is there a contact on the opportunity flagged `is_decision_maker = true`?
- Has the rep identified who signs off on this purchase?
- **Gap signal:** `context.contacts` has no contact with `is_decision_maker = true`,
  especially once the deal is past Discovery.
- **Recommended action when missing:** `qualify_authority` or
  `schedule_meeting` — get the decision maker into the next conversation.

## Need

- Is there a documented pain point or business driver in the timeline/notes?
- Does the proposed solution map to a specific need the buyer described?
- **Gap signal:** Timeline contains no notes describing a problem, pain
  point, or desired outcome.
- **Recommended action when missing:** `qualify_need` — run or schedule a
  discovery conversation to document the buyer's problem in their own words.

## Timeline

- Has the buyer indicated when they need a solution in place?
- Does `close_date` reflect a realistic, buyer-driven timeline (not just a
  rep guess)?
- **Gap signal:** No `close_date`, or `close_date` has slipped without a new
  date being set.
- **Recommended action when missing:** `qualify_timeline` — confirm the
  buyer's target go-live date and work backwards to a realistic close date.

## Stage-by-stage BANT expectations

| Stage | Budget | Authority | Need | Timeline |
|---|---|---|---|---|
| Discovery | Rough range discussed | Champion identified | Pain point documented | Loose target |
| Proposal | Confirmed range | Decision maker engaged | Solution mapped to need | Target close date set |
| Negotiation | Approved/near-approved | Decision maker actively involved | Reconfirmed | Firm close date |

When a BANT element is missing for the current stage, the Next Step Agent
should prioritize a qualification action over later-stage actions (e.g. do
not recommend `send_proposal` if Budget and Need are both unqualified).
