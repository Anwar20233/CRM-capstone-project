# Risk Agent Overview And Workflow

## What The Risk Agent Is

The Risk Agent is a backend scoring agent for CRM opportunities.

Its job is to answer:

```text
Given this opportunity, what is the current risk level and why?
```

It returns:

```text
risk_score: 0.0 to 1.0
risk_level: low | medium | high
factors: why the opportunity is risky
reasoning_summary: short explanation
recommended_notification: whether the sales rep should be notified
metadata: how much evidence was used
```

The Risk Agent does not need P1 to build or pass a full `DealContext`. P1 or any backend caller only needs to pass:

```text
opportunity_id
workspace_id, optional
trigger_type
```

The agent then fetches the risk evidence it needs directly from PostgreSQL.

## Single Deal Workflow

When code calls:

```python
await evaluate_deal_risk(
    opportunity_id="...",
    trigger_type="manual",
)
```

the Risk Agent does this:

```text
1. Resolve the workspace schema.
2. Fetch the opportunity row.
3. Fetch profile facts.
4. Fetch stakeholder relationships.
5. Fetch latest profile narrative / pending action context.
6. Fetch recent messages, notes, and tasks.
7. Build an internal RiskDealContext.
8. Score the opportunity.
9. Return a RiskAssessment.
```

The internal context is private to the Risk Agent. It is not passed in from P1.

## Database Sources

The Risk Agent reads from:

```text
core."dataSource"
workspace_schema."opportunity"
followup_agent.profile_facts
followup_agent.profile_relationships
followup_agent.followup_pending_actions
workspace_schema."message"
workspace_schema."note"
workspace_schema."noteTarget"
workspace_schema."task"
workspace_schema."taskTarget"
```

The agent deliberately does not use these as scoring evidence:

```text
followup_agent.risk_snapshots
followup_agent.followup_pending_actions.risk_assessment
```

Those are old or previously computed risk outputs. The current score is calculated from current CRM, profile, relationship, and activity evidence.

## What The Agent Looks For

Risk-increasing signals include:

```text
engagement_gap:
  Opportunity not updated recently, no recent activity.

close_date_pressure:
  Close date is overdue or coming soon.

deal_velocity_drop:
  Deal is stuck in PROPOSAL, MEETING, or SCREENING.

missing_stakeholder:
  No owner, no point of contact, or no champion relationship.

unresolved_objection:
  Profile facts mention objection, concern, blocker, procurement, legal, security,
  budget, or timeline issues.

sentiment_decline:
  Negative profile facts.

missing_next_step:
  No open task or overdue tasks.
```

Risk-reducing signals include:

```text
positive_momentum:
  Buying signals, approvals, commitment, scheduled next steps, or similar positive evidence.
```

## Real Database Example

This real local database example was tested with:

```text
database: postgres://postgres:postgres@localhost:5432/default
schema override: workspace_c4en9trdpordobem3offy83aa
```

Opportunity:

```text
name: Airbnb — Platform Integration
opportunity_id: a45a119e-376d-5eb2-8b96-33c2631660ea
```

Result:

```text
risk_score: 1.0
risk_level: high
should_notify: true
urgency: high
```

Evidence used:

```text
facts_considered: 14
relationships_considered: 16
messages_considered: 20
notes_considered: 7
tasks_considered: 5
```

Top risk signals:

```text
1. engagement_gap
   Opportunity has not been updated for 47 days.

2. sentiment_decline
   Q3 integration timeline is tight due to engineering freeze in August.

3. deal_velocity_drop
   Deal appears stuck in PROPOSAL.
```

Plain-language interpretation:

```text
The Airbnb deal is high risk because it has been stale for 47 days,
there is a known timeline concern from extracted profile facts,
and the opportunity is stuck in proposal stage.
```

## Daily Risk Sweep

The daily sweep is a standalone backend job around the Risk Agent.

The Risk Agent scores one opportunity. The daily sweep runs the Risk Agent across all active opportunities.

Command:

```bash
PYTHONPATH=. .venv/bin/python -m followup.risk.daily_sweep
```

The daily sweep does this:

```text
1. Discover active opportunities.
2. Score each opportunity with DatabaseRiskAgent.
3. Store every score in followup_agent.risk_daily_scores.
4. Compare the new score/level with the latest stored daily score.
5. Create a risk_alert pending action only when threshold rules require it.
6. Avoid duplicate alerts when a pending risk_alert already exists.
```

## Daily Sweep Real Database Result

First real DB sweep:

```text
scanned: 19
scored: 19
alerts_created: 8
skipped: 11
failed: 0
```

Database writes after the first sweep:

```text
risk_daily_scores total: 19
risk_alert pending_actions total: 8
```

Second real DB sweep:

```text
scanned: 19
scored: 19
alerts_created: 0
skipped: 19
failed: 0
```

This confirms deduplication works. The second run did not create duplicate risk alerts because pending `risk_alert` actions already existed.

Final database state after two sweeps:

```text
risk_daily_scores total: 38
risk_alert pending_actions total: 8
```

## Where Notifications Are Stored

The sweep does not directly show UI notifications.

It creates pending action rows in:

```text
followup_agent.followup_pending_actions
```

Risk alert rows use:

```text
trigger_type = risk_alert
status = pending
urgency = medium | high
action_type = follow_up_call | escalate
risk_assessment = full RiskAssessment
action_payload = notification payload + risk details
```

P1 or the frontend must read these rows and render them to the sales rep.

## What Is Missing For Full Integration

The backend scoring and daily sweep are working. Full product integration still needs:

```text
1. Production scheduler
   Run python -m followup.risk.daily_sweep every morning, for example 06:00 Asia/Riyadh.

2. P1/frontend notification UI
   Read followup_agent.followup_pending_actions where trigger_type = risk_alert
   and status = pending.

3. Notification display
   Show urgency, title, reasoning, top risk factors, and recommended action.

4. Rep actions
   Let the sales rep accept, dismiss, snooze, or act on the alert.

5. Status updates
   Update the pending action status to accepted, rejected, edited, or expired.

6. Production workspace discovery
   Local testing used FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE because the test
   schema was not registered in core."dataSource". Production should use real
   core."dataSource" mappings.

7. Scheduler monitoring
   Add logs/alerts if the daily sweep fails.

8. Retention policy
   Decide whether old risk_daily_scores rows should be kept forever, archived,
   or deleted after a retention window.
```

## Simple Mental Model

```text
Risk Agent = scores one deal right now.

Daily Sweep = runs the Risk Agent for all active deals every morning.

Pending Action = stored alert data that P1/frontend should show to the sales rep.
```
