"""Thirty realistic B2B follow-up email scenarios for agent testing.

Scenarios are named by **sales situation** (not vendor company). Each maps to a
seeded CRM sender so ``followup_orchestrator_e2e.py`` can resolve person → deal.

Situation types covered:
  - Long-running / historied relationships (references prior calls, pilots, years)
  - Sparse or thin context (vague check-ins, wrong name, cold inbound)
  - Negative / at-risk (pricing, competitor, security hold, support crisis)
  - Positive momentum (budget approved, pilot win, security cleared, expansion)
  - Process friction (procurement, legal, documentation)
  - Edge cases (unknown sender, ambiguous deal)

Review outputs in LangSmith; compare runs to ``EXPECTATIONS`` via ``followup_batch_e2e.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ScenarioExpectations:
    """Expected agent behavior for automated batch scoring (not golden email text)."""

    expect_pipeline_ok: bool = True
    action_types: frozenset[str] = frozenset()
    urgency: frozenset[str] = frozenset()
    expect_draft: bool | None = None
    expect_calendar: bool | None = None
    risk_band: str | None = None  # low | medium | high


def _exp(
    *,
    pipeline_ok: bool = True,
    actions: tuple[str, ...] = (),
    urgency: tuple[str, ...] = (),
    draft: bool | None = None,
    calendar: bool | None = None,
    risk: str | None = None,
) -> ScenarioExpectations:
    return ScenarioExpectations(
        expect_pipeline_ok=pipeline_ok,
        action_types=frozenset(actions),
        urgency=frozenset(urgency),
        expect_draft=draft,
        expect_calendar=calendar,
        risk_band=risk,
    )


@dataclass(frozen=True)
class EmailScenario:
    name: str
    sender: str
    subject: str
    body: str
    exercises: str
    expectations: ScenarioExpectations | None = None


SCENARIOS: dict[str, EmailScenario] = {
    # --- Historied relationship / rich CRM context (4) ---
    "historied_account_timeline_slip": EmailScenario(
        name="historied_account_timeline_slip",
        sender="john.park@airbnb.com",
        subject="Re: rollout plan — timing concern",
        body=(
            "Hi Sarah,\n\n"
            "We've been working on this since the demo back in April and I still "
            "believe we're aligned technically. That said, our engineering freeze "
            "in August makes the original go-live date unrealistic.\n\n"
            "A new VP of Engineering, Rachel Torres, now owns infrastructure "
            "decisions. She'll need to bless the security review before we commit. "
            "Lisa on procurement is already expecting revised paperwork.\n\n"
            "Can we reset expectations on timeline and send an updated SOW by "
            "June 25? We're also getting pressure to compare against Segment on "
            "price.\n\nJohn"
        ),
        exercises="historied deal; timeline slip; new authority; competitor; procurement in flight",
    ),
    "long_term_contact_procurement_rhythm": EmailScenario(
        name="long_term_contact_procurement_rhythm",
        sender="lisa.huang@airbnb.com",
        subject="Re: vendor paperwork — same process as last year",
        body=(
            "Hi Sarah,\n\n"
            "Following up on the thread with John. As we discussed when you "
            "onboarded as vendor last cycle, our evaluation still takes 2–3 weeks "
            "once documentation is complete.\n\n"
            "We're missing the SOC 2 package and updated MSA. David Kim on my "
            "team will handle commercial terms once security signs off.\n\n"
            "Lisa"
        ),
        exercises="historied procurement contact; documentation gap; predictable process",
    ),
    "renewal_expansion_existing_usage": EmailScenario(
        name="renewal_expansion_existing_usage",
        sender="emma.larsen@figma.com",
        subject="Re: renewal — adding more seats",
        body=(
            "Hi Sarah,\n\n"
            "We've been on the platform since the pilot wrapped last quarter and "
            "adoption is strong. Brand design wants 50 additional seats before "
            "the July rollout.\n\n"
            "Can you send a prorated amendment for the rest of the year? Tyler "
            "already validated the technical side.\n\n"
            "Emma"
        ),
        exercises="positive historied customer; expansion/upsell; existing relationship",
    ),
    "reference_check_from_past_win": EmailScenario(
        name="reference_check_from_past_win",
        sender="rachel.kim@datadog.com",
        subject="Re: evaluation — reference call request",
        body=(
            "Hi Marcus,\n\n"
            "We've been in evaluation for two months and James's POC team is "
            "mostly satisfied. Before I take this to our VP I need a reference "
            "customer in observability at similar scale (500+ hosts).\n\n"
            "Can you arrange a 30-minute reference call next week?\n\n"
            "Rachel"
        ),
        exercises="mid-cycle historied eval; reference request; meeting-ish follow-up",
    ),
    # --- Sparse / thin information (4) ---
    "vague_checkin_no_deal_signal": EmailScenario(
        name="vague_checkin_no_deal_signal",
        sender="alex.rivera@stripe.com",
        subject="Quick hello",
        body=(
            "Hi Sarah,\n\n"
            "Been a hectic quarter. Hope you're doing well. Let's catch up "
            "sometime — coffee or a walk, no agenda.\n\n"
            "Alex"
        ),
        exercises="sparse context; ambiguous opportunity; minimal actionable signal",
    ),
    "one_line_pricing_question": EmailScenario(
        name="one_line_pricing_question",
        sender="kevin.cho@notion.com",
        subject="Pricing?",
        body=(
            "Marcus — ballpark annual cost for ~100 seats? Need a number for "
            "finance even if rough.\n\n"
            "K"
        ),
        exercises="sparse email; thin context; still resolves via known sender",
    ),
    "misaddressed_greeting_wrong_rep": EmailScenario(
        name="misaddressed_greeting_wrong_rep",
        sender="kevin.cho@notion.com",
        subject="Re: contract question",
        body=(
            "Hi Jennifer,\n\n"
            "Legal asked about the liability cap — is it 12 months of fees or "
            "a fixed amount? One sentence is fine.\n\n"
            "Kevin"
        ),
        exercises="wrong rep name; sender still resolves deal; clarification draft",
    ),
    "unknown_sender_no_crm_record": EmailScenario(
        name="unknown_sender_no_crm_record",
        sender="dana.fischer@brandnewco.example",
        subject="Saw you at the conference",
        body=(
            "Hello,\n\n"
            "We met briefly at SaaStr. I didn't catch your card. We're a 40-person "
            "startup exploring tools in your space. Any chance you have a starter "
            "tier?\n\n"
            "Dana"
        ),
        exercises="no CRM match; extract halt; zero historied context",
    ),
    # --- Champion / stakeholder change (4) ---
    "champion_leaving_internal_transfer": EmailScenario(
        name="champion_leaving_internal_transfer",
        sender="alex.rivera@stripe.com",
        subject="Handing this evaluation off",
        body=(
            "Hi Sarah,\n\n"
            "I'm rotating to a new org and won't own this evaluation anymore. "
            "Tom Becker (Head of Data) is taking over — he reports to our CTO and "
            "will want to re-run the benchmark.\n\n"
            "Please add tom.becker@stripe.com. I appreciated the work you've put "
            "in so far.\n\n"
            "Alex"
        ),
        exercises="champion departure; new decision-maker shadow; relationship change",
    ),
    "new_security_owner_joins_late": EmailScenario(
        name="new_security_owner_joins_late",
        sender="nadia.osei@stripe.com",
        subject="Security review — joining late",
        body=(
            "Hi Sarah,\n\n"
            "I'm picking up the security thread mid-evaluation. I wasn't on the "
            "original demo so I have limited context. I need your data retention "
            "policy, subprocessor list, and whether CMK is supported.\n\n"
            "No procurement until I sign off.\n\n"
            "Nadia"
        ),
        exercises="late stakeholder; sparse context for them; security gate",
    ),
    "executive_sponsor_surfaces_late": EmailScenario(
        name="executive_sponsor_surfaces_late",
        sender="maria.santos@notion.com",
        subject="Joining the workflow initiative",
        body=(
            "Hi Marcus,\n\n"
            "Kevin mentioned you've been driving this for weeks — I'm VP Ops and "
            "only now getting looped in as executive sponsor.\n\n"
            "I need a one-page ROI summary and a SaaS case study before I back "
            "budget in August.\n\n"
            "Maria"
        ),
        exercises="late executive; thin exec context; case study ask",
    ),
    "procurement_takes_over_commercial_thread": EmailScenario(
        name="procurement_takes_over_commercial_thread",
        sender="lisa.huang@airbnb.com",
        subject="Commercial terms — taking over from John",
        body=(
            "Hi Sarah,\n\n"
            "John asked me to own commercial negotiations going forward. I don't "
            "have the technical history — just the $85k proposal and your SOC "
            "packet.\n\n"
            "Please confirm payment terms (net 45) and whether professional "
            "services are mandatory.\n\n"
            "Lisa"
        ),
        exercises="handoff to procurement; partial context; commercial focus",
    ),
    # --- At-risk / negative (8) ---
    "pricing_above_approved_budget": EmailScenario(
        name="pricing_above_approved_budget",
        sender="kevin.cho@notion.com",
        subject="Re: quote — over budget",
        body=(
            "Hi Marcus,\n\n"
            "Finance capped this initiative at $90k and your quote is above that. "
            "We're also talking to Airtable because their entry tier fits better.\n\n"
            "Our CFO Dieter Voss is skeptical on ROI. What can you do on price?\n\n"
            "Kevin"
        ),
        exercises="pricing objection; competitor; CFO skepticism; high risk",
    ),
    "competitor_active_in_evaluation": EmailScenario(
        name="competitor_active_in_evaluation",
        sender="rachel.kim@datadog.com",
        subject="Re: shortlist comparison",
        body=(
            "Hi Marcus,\n\n"
            "We're down to two vendors — you and Grafana Cloud. Their quote came "
            "in lower for our metrics volume. Your PagerDuty integration is better "
            "but finance is pushing on cost.\n\n"
            "Match their number or add TAM at no charge? CFO review Friday.\n\n"
            "Rachel"
        ),
        exercises="competitive bake-off; pricing pressure; deadline",
    ),
    "benchmark_results_disputed": EmailScenario(
        name="benchmark_results_disputed",
        sender="alex.rivera@stripe.com",
        subject="Re: benchmark — doesn't match our workload",
        body=(
            "Hi Sarah,\n\n"
            "The new owner of this eval isn't convinced the benchmark reflects "
            "production traffic. Latency under load was higher than we need.\n\n"
            "Re-run with our dataset or share a fintech reference at our scale — "
            "otherwise we deprioritize.\n\n"
            "Alex"
        ),
        exercises="technical objection; skepticism; reference request; stall risk",
    ),
    "feature_gap_threatens_shortlist": EmailScenario(
        name="feature_gap_threatens_shortlist",
        sender="kevin.cho@notion.com",
        subject="Re: feature matrix",
        body=(
            "Hi Marcus,\n\n"
            "The team built a side-by-side matrix. We're losing on native database "
            "views and no-code automations versus the other finalist.\n\n"
            "Do you have parity on the roadmap or is this a hard gap?\n\n"
            "Kevin"
        ),
        exercises="feature objection; competitive; needs substantive follow-up",
    ),
    "technical_blocker_stalls_project": EmailScenario(
        name="technical_blocker_stalls_project",
        sender="john.park@airbnb.com",
        subject="Re: POC — rate limit issue",
        body=(
            "Hi Sarah,\n\n"
            "Engineering paused the POC. API rate limits can't handle our batch "
            "volume without a dedicated tier.\n\n"
            "We may shelve this until Q4 unless product can join a call Thursday.\n\n"
            "John"
        ),
        exercises="technical blocker; negative momentum; meeting request",
    ),
    "security_questionnaire_delay": EmailScenario(
        name="security_questionnaire_delay",
        sender="john.park@airbnb.com",
        subject="Security items before we continue",
        body=(
            "Hi Sarah,\n\n"
            "Infra leadership wants answers on EU residency, SAML, and incident "
            "SLA before we spend more cycles. Standard questionnaire attached "
            "in our portal — we can't advance without it.\n\n"
            "Target decision by July 15 if responses land this week.\n\n"
            "John"
        ),
        exercises="security gate; compliance delay; deadline",
    ),
    "legal_redlines_stall_signature": EmailScenario(
        name="legal_redlines_stall_signature",
        sender="emma.larsen@figma.com",
        subject="Re: MSA — legal comments",
        body=(
            "Hi Sarah,\n\n"
            "Counsel returned redlines on liability, indemnity, and DPA. Nothing "
            "fatal but we need a joint legal call before anyone signs.\n\n"
            "Wednesday or Thursday this week?\n\n"
            "Emma"
        ),
        exercises="legal friction; meeting request; deal still alive",
    ),
    "support_escalation_threatens_eval": EmailScenario(
        name="support_escalation_threatens_eval",
        sender="james.okonkwo@datadog.com",
        subject="P1 still open — losing patience",
        body=(
            "Hi Marcus,\n\n"
            "Alert routing has been broken for 36 hours. Rachel is asking if we "
            "should stop the evaluation.\n\n"
            "I need exec escalation and a written remediation plan today.\n\n"
            "James"
        ),
        exercises="support crisis; churn risk; urgent high-stakes reply",
    ),
    # --- Positive momentum (7) ---
    "pilot_results_exceed_target": EmailScenario(
        name="pilot_results_exceed_target",
        sender="kevin.cho@notion.com",
        subject="Re: pilot — numbers look good",
        body=(
            "Hi Marcus,\n\n"
            "Pilot team saved ~18% on weekly reporting. Maria wants two more "
            "departments in phase two.\n\n"
            "What does enterprise pricing look like for 150 seats with SSO?\n\n"
            "Kevin"
        ),
        exercises="positive pilot; expansion signal; pricing next step",
    ),
    "verbal_yes_pending_legal_only": EmailScenario(
        name="verbal_yes_pending_legal_only",
        sender="emma.larsen@figma.com",
        subject="Re: moving to contract",
        body=(
            "Hi Sarah,\n\n"
            "Leadership gave verbal approval after the pilot. Only blocker is "
            "legal review of the MSA — our counsel Priya Nair has the draft.\n\n"
            "If redlines are light we'd sign before quarter end. Please send the "
            "order form.\n\n"
            "Emma"
        ),
        exercises="strong buying signal; legal last step; order form request",
    ),
    "budget_formally_approved": EmailScenario(
        name="budget_formally_approved",
        sender="rachel.kim@datadog.com",
        subject="Re: finance approval",
        body=(
            "Hi Marcus,\n\n"
            "Finance approved $180k year one. Send order form and implementation "
            "plan. James leads technically; Ben owns production rollout.\n\n"
            "Aiming for signature by July 10.\n\n"
            "Rachel"
        ),
        exercises="budget approved; strong close signal; timeline",
    ),
    "security_cleared_proceed_commercial": EmailScenario(
        name="security_cleared_proceed_commercial",
        sender="nadia.osei@stripe.com",
        subject="Re: security — cleared",
        body=(
            "Hi Sarah,\n\n"
            "Security review complete with no blockers. Commercial team can proceed "
            "on MSA and order form.\n\n"
            "Notify us if subprocessors change before signature.\n\n"
            "Nadia"
        ),
        exercises="positive unblock; handoff to commercial; risk drops",
    ),
    "technical_signoff_complete": EmailScenario(
        name="technical_signoff_complete",
        sender="tyler.briggs@figma.com",
        subject="Re: technical checklist — done",
        body=(
            "Hi Sarah,\n\n"
            "I finished the technical checklist Emma asked for — plugin API, token "
            "sync latency, and Okta SSO all pass.\n\n"
            "Recommending approval to Emma unless legal stalls.\n\n"
            "Tyler"
        ),
        exercises="technical win; champion path to close; positive",
    ),
    "poc_positive_one_open_item": EmailScenario(
        name="poc_positive_one_open_item",
        sender="james.okonkwo@datadog.com",
        subject="Re: POC wrap-up",
        body=(
            "Hi Marcus,\n\n"
            "POC met latency targets. Ben (SRE) liked the DaemonSet deploy story.\n\n"
            "Only open item: raise custom metric cardinality cap to 50k series "
            "and we're ready for commercial terms.\n\n"
            "James"
        ),
        exercises="mostly positive POC; single negotiable gap; path to proposal",
    ),
    "finance_deadline_creates_urgency": EmailScenario(
        name="finance_deadline_creates_urgency",
        sender="alex.rivera@stripe.com",
        subject="Re: need quote by month end",
        body=(
            "Hi Sarah,\n\n"
            "Finance needs final pricing by June 28 to book Q2 spend. Quote for "
            "200 seats plus premium support please.\n\n"
            "Miss that window and earliest start is September.\n\n"
            "Alex"
        ),
        exercises="positive urgency; EOQ deadline; pricing request",
    ),
    # --- Meetings & scheduling (3) ---
    "meeting_request_with_specific_slots": EmailScenario(
        name="meeting_request_with_specific_slots",
        sender="alex.rivera@stripe.com",
        subject="Re: few questions before we decide",
        body=(
            "Hi Sarah,\n\n"
            "A few open questions on the benchmark. Can we do 30 minutes next week?\n\n"
            "I'm free Tuesday 2–4pm PT or Wednesday morning.\n\n"
            "Alex"
        ),
        exercises="meeting_request; calendar + draft with slots",
    ),
    "demo_request_for_executive_review": EmailScenario(
        name="demo_request_for_executive_review",
        sender="kevin.cho@notion.com",
        subject="Re: demo for finance review",
        body=(
            "Hi Marcus,\n\n"
            "Dieter wants a live demo focused on ROI and admin controls before "
            "he'll reopen budget.\n\n"
            "Thursday 10am–12pm PT or Friday 1–3pm PT — 45 minutes with me and "
            "Maria.\n\n"
            "Kevin"
        ),
        exercises="meeting_request; executive stage; multi-attendee",
    ),
    "architecture_review_before_vp_pitch": EmailScenario(
        name="architecture_review_before_vp_pitch",
        sender="rachel.kim@datadog.com",
        subject="Re: architecture session",
        body=(
            "Hi Marcus,\n\n"
            "James and I need a 60-minute architecture review with your solutions "
            "engineer before we present to our VP.\n\n"
            "Monday 1–4pm ET or Tuesday 10am–12pm ET?\n\n"
            "Rachel"
        ),
        exercises="meeting_request; senior review; pre-close stage",
    ),
}


EXPECTATIONS: dict[str, ScenarioExpectations] = {
    "historied_account_timeline_slip": _exp(
        actions=("send_proposal", "follow_up_call", "escalate"),
        urgency=("medium", "high"),
        draft=True,
        risk="high",
    ),
    "long_term_contact_procurement_rhythm": _exp(
        actions=("follow_up_call", "send_proposal"),
        urgency=("medium",),
        draft=True,
        risk="medium",
    ),
    "renewal_expansion_existing_usage": _exp(
        actions=("send_proposal", "close_deal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "reference_check_from_past_win": _exp(
        actions=("schedule_meeting", "follow_up_call", "send_proposal"),
        urgency=("medium",),
        draft=True,
        risk="medium",
    ),
    "vague_checkin_no_deal_signal": _exp(
        actions=("check_in", "follow_up_call", "no_action"),
        urgency=("low", "medium"),
        draft=True,
        risk="low",
    ),
    "one_line_pricing_question": _exp(
        actions=("send_proposal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="medium",
    ),
    "misaddressed_greeting_wrong_rep": _exp(
        actions=("follow_up_call", "check_in", "send_proposal"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "unknown_sender_no_crm_record": _exp(pipeline_ok=False),
    "champion_leaving_internal_transfer": _exp(
        actions=("follow_up_call", "escalate", "check_in"),
        urgency=("medium", "high"),
        draft=True,
        risk="medium",
    ),
    "new_security_owner_joins_late": _exp(
        actions=("follow_up_call", "send_proposal"),
        urgency=("medium", "high"),
        draft=True,
        risk="medium",
    ),
    "executive_sponsor_surfaces_late": _exp(
        actions=("send_proposal", "follow_up_call", "schedule_meeting"),
        urgency=("medium", "high"),
        draft=True,
        risk="medium",
    ),
    "procurement_takes_over_commercial_thread": _exp(
        actions=("send_proposal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="medium",
    ),
    "pricing_above_approved_budget": _exp(
        actions=("escalate", "send_proposal", "follow_up_call"),
        urgency=("high",),
        draft=True,
        risk="high",
    ),
    "competitor_active_in_evaluation": _exp(
        actions=("escalate", "send_proposal", "follow_up_call"),
        urgency=("high",),
        draft=True,
        risk="high",
    ),
    "benchmark_results_disputed": _exp(
        actions=("follow_up_call", "escalate", "send_proposal"),
        urgency=("medium", "high"),
        draft=True,
        risk="high",
    ),
    "feature_gap_threatens_shortlist": _exp(
        actions=("follow_up_call", "send_proposal", "escalate"),
        urgency=("medium", "high"),
        draft=True,
        risk="high",
    ),
    "technical_blocker_stalls_project": _exp(
        actions=("schedule_meeting", "escalate", "follow_up_call"),
        urgency=("high",),
        draft=True,
        calendar=True,
        risk="high",
    ),
    "security_questionnaire_delay": _exp(
        actions=("follow_up_call", "send_proposal"),
        urgency=("medium", "high"),
        draft=True,
        risk="medium",
    ),
    "legal_redlines_stall_signature": _exp(
        actions=("schedule_meeting", "follow_up_call", "close_deal"),
        urgency=("medium", "high"),
        draft=True,
        calendar=True,
        risk="medium",
    ),
    "support_escalation_threatens_eval": _exp(
        actions=("escalate", "follow_up_call"),
        urgency=("high",),
        draft=True,
        risk="high",
    ),
    "pilot_results_exceed_target": _exp(
        actions=("send_proposal", "close_deal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "verbal_yes_pending_legal_only": _exp(
        actions=("close_deal", "send_proposal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "budget_formally_approved": _exp(
        actions=("close_deal", "send_proposal"),
        urgency=("medium", "high"),
        draft=True,
        risk="low",
    ),
    "security_cleared_proceed_commercial": _exp(
        actions=("close_deal", "send_proposal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "technical_signoff_complete": _exp(
        actions=("close_deal", "check_in", "follow_up_call"),
        urgency=("low", "medium"),
        draft=True,
        risk="low",
    ),
    "poc_positive_one_open_item": _exp(
        actions=("send_proposal", "close_deal", "follow_up_call"),
        urgency=("medium",),
        draft=True,
        risk="low",
    ),
    "finance_deadline_creates_urgency": _exp(
        actions=("send_proposal", "follow_up_call", "close_deal"),
        urgency=("high",),
        draft=True,
        risk="medium",
    ),
    "meeting_request_with_specific_slots": _exp(
        actions=("schedule_meeting", "follow_up_call"),
        urgency=("medium", "high"),
        draft=True,
        calendar=True,
        risk="medium",
    ),
    "demo_request_for_executive_review": _exp(
        actions=("schedule_meeting", "follow_up_call"),
        urgency=("high",),
        draft=True,
        calendar=True,
        risk="medium",
    ),
    "architecture_review_before_vp_pitch": _exp(
        actions=("schedule_meeting", "follow_up_call"),
        urgency=("medium", "high"),
        draft=True,
        calendar=True,
        risk="medium",
    ),
}


def get(name: str) -> EmailScenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    scenario = SCENARIOS[name]
    expectations = EXPECTATIONS.get(name)
    if expectations is None:
        return scenario
    return replace(scenario, expectations=expectations)


def list_scenario_names() -> list[str]:
    return sorted(SCENARIOS.keys())


# Rich historied deal — good default for single-scenario e2e runs
DEFAULT_SCENARIO = "historied_account_timeline_slip"
