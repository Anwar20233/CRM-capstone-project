"""Sixty CRM read/write scenarios for the MAIN orchestrator (agent/orchestrator.py).

This is the orchestrator analogue of ``followup_email_scenarios.py``. Where that
file grades the follow-up *email pipeline*, this one grades the chat
**orchestrator** end to end: a natural-language user message goes in, the
orchestrator plans it, delegates to the reader/writer sub-agents, and we assert it
reached the right answer the RIGHT WAY — the correct sub-agents, in the correct
order, calling the correct CRM tools with the correct targets.

What each scenario pins down (only what matters for that case is set):

  agents          the sub-agents that MUST be delegated to, as an ordered
                  subsequence (reader before writer when both are needed).
  forbid_agents   sub-agents that must NOT be called (routing discipline — e.g.
                  never "find" a record you are about to CREATE).
  read_tools      reader CRM tools that must execute (pin only when the tool
                  choice is the point, e.g. composite reads / group_by).
  read_entity     a substring that must appear in the reader's resolved output
                  (proves it resolved the right record).
  read_resolution "single" | "none" — the reader's structured resolution.
  write_tools     writer CRM tools that must ALL execute successfully.
  write_any_of    at least ONE of these must execute (composite OR primitive).
  link_targets    target columns a note/task MUST carry (targetPersonId, …) —
                  an unlinked note/task floats invisibly and is a failure.
  expect_interrupt a tier-3 write (close/delete) should pause for approval.
  response_includes substrings the final consolidated reply should contain.
  no_write        the run must change nothing (pure read).
  clarify         the orchestrator should ask a clarifying question, not act.

All entities below are REAL seeded records (see ``seed_data.py``): companies
Airbnb/Stripe/Notion/Figma/Datadog, their people, and their opportunities. Run
through ``orchestrator_crm_eval.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OrchExpectations:
    agents: tuple[str, ...] = ()
    forbid_agents: tuple[str, ...] = ()
    read_tools: tuple[str, ...] = ()
    read_entity: str | None = None
    read_resolution: str | None = None
    write_tools: tuple[str, ...] = ()
    write_any_of: tuple[str, ...] = ()
    link_targets: tuple[str, ...] = ()
    expect_interrupt: bool = False
    response_includes: tuple[str, ...] = ()
    no_write: bool = False
    clarify: bool = False
    # Un-seeded PII the message PLANTS (new names / raw emails / phones). Every
    # one MUST be masked before any LLM (raw-OpenAI boundary) sees it — the core
    # leakage guarantee. Seeded contacts are excluded (they resolve to handles).
    pii_must_mask: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrchScenario:
    name: str
    message: str
    exercises: str
    expectations: OrchExpectations = field(default_factory=OrchExpectations)


def _exp(**kwargs) -> OrchExpectations:
    return OrchExpectations(**kwargs)


# ---------------------------------------------------------------------------
# Scenario catalog (60). Grouped by what they exercise.
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, OrchScenario] = {}


def _add(scenario: OrchScenario) -> None:
    SCENARIOS[scenario.name] = scenario


# --- Group 1: reader — simple lookups (8) ---------------------------------
_add(OrchScenario(
    "r_find_person", "Find John Park.",
    "simplest read: resolve one person by name",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_entity="Park", read_resolution="single", no_write=True),
))
_add(OrchScenario(
    "r_find_company", "Look up Stripe in the CRM.",
    "resolve one company by name",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_entity="Stripe", read_resolution="single", no_write=True),
))
_add(OrchScenario(
    "r_find_opportunity", "Show me the Notion Workflow Automation deal.",
    "resolve one opportunity by name",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_entity="Notion", no_write=True),
))
_add(OrchScenario(
    "r_deal_amount", "What's the deal value on the Figma deal?",
    "read a scalar field off a resolved deal",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("62",), no_write=True),
))
_add(OrchScenario(
    "r_person_title", "What's Kevin Cho's job title?",
    "read a scalar field off a resolved person",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("Operations",), no_write=True),
))
_add(OrchScenario(
    "r_company_employees", "How many employees does Airbnb have?",
    "headcount = read employees field, NOT group_by",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("6",), no_write=True),
))
_add(OrchScenario(
    "r_find_by_email", "Find the person with email alex.rivera@stripe.com.",
    "resolve a person by email (PII handle round-trip)",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_entity="Rivera", read_resolution="single", no_write=True),
))
_add(OrchScenario(
    "r_deal_close_date", "When does the Stripe Analytics deal close?",
    "read close date off a resolved deal",
    _exp(agents=("reader",), forbid_agents=("writer",), no_write=True),
))

# --- Group 2: reader — relational + lists (8) -----------------------------
_add(OrchScenario(
    "r_people_at_company", "Who works at Notion?",
    "relational: list a company's people",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("find_people",),
         response_includes=("Kevin",), no_write=True),
))
_add(OrchScenario(
    "r_person_at_company", "Find Maria at Notion.",
    "person filtered by BOTH name and company",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_entity="Santos", read_resolution="single", no_write=True),
))
_add(OrchScenario(
    "r_head_of_finance", "What's the email of the head of finance at Notion?",
    "relational riddle: decompose company -> its people -> the right one",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("maria.santos@notion.com",), no_write=True),
))
_add(OrchScenario(
    "r_point_of_contact", "Who is the point of contact on the Airbnb deal?",
    "follow an opportunity relation to a person",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("John",), no_write=True),
))
_add(OrchScenario(
    "r_deals_at_company", "List the open deals for Datadog.",
    "relational: a company's opportunities",
    _exp(agents=("reader",), forbid_agents=("writer",), no_write=True),
))
_add(OrchScenario(
    "r_account_owner", "Who owns the Figma account?",
    "read account owner (workspace member)",
    _exp(agents=("reader",), forbid_agents=("writer",),
         response_includes=("Sarah",), no_write=True),
))
_add(OrchScenario(
    "r_notes_on_deal", "Show me the notes on the Notion deal.",
    "list notes linked to a deal",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("find_notes",), no_write=True),
))
_add(OrchScenario(
    "r_tasks_on_deal", "What tasks are open on the Airbnb deal?",
    "list tasks linked to a deal",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("find_tasks",), no_write=True),
))

# --- Group 3: reader — composite reads, ONE call (6) ----------------------
_add(OrchScenario(
    "r_company_overview", "Give me a full overview of the Stripe account.",
    "composite read: one get_company_overview, not a fan-out",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("get_company_overview",), no_write=True),
))
_add(OrchScenario(
    "r_account_health", "How healthy is the Notion account right now?",
    "composite read: account_health_check",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("account_health_check",), no_write=True),
))
_add(OrchScenario(
    "r_timeline", "Show me the recent activity timeline for the Figma deal.",
    "composite read: get_entity_timeline on an opportunity",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("get_entity_timeline",), no_write=True),
))
_add(OrchScenario(
    "r_related", "What records are linked to Emma Larsen?",
    "composite read: get_related_entities on a person",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("get_related_entities",), no_write=True),
))
_add(OrchScenario(
    "r_search", "Search the CRM for anything matching Datadog.",
    "composite read: search_all_records id bootstrap",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("search_all_records",), no_write=True),
))
_add(OrchScenario(
    "r_group_by_stage", "How many deals do we have in each stage?",
    "aggregation across groups: group_by_opportunities",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_tools=("group_by_opportunities",), no_write=True),
))

# --- Group 4: writer — notes (reader resolves, then writer links) (6) -----
_add(OrchScenario(
    "w_note_person", "Add a note to John Park: left him a voicemail today.",
    "note linked to a PERSON (person-only note is valid)",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetPersonId",)),
))
_add(OrchScenario(
    "w_note_deal", "Log a note on the Airbnb deal that David finally approved the timeline.",
    "note linked to an OPPORTUNITY",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetOpportunityId",)),
))
_add(OrchScenario(
    "w_note_person_and_deal",
    "Add a note to Kevin Cho and the Notion deal about the pricing pushback.",
    "note linked to BOTH a person and an opportunity (two target rows)",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetPersonId", "targetOpportunityId")),
))
_add(OrchScenario(
    "w_note_company", "Add a note to the Figma company record: renewal is coming up.",
    "note linked to a COMPANY",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetCompanyId",)),
))
_add(OrchScenario(
    "w_note_two_deals", "Add a note 'Q3 sync done' to both the Airbnb and Stripe deals.",
    "two deals resolved, note linked to each",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetOpportunityId",)),
))
_add(OrchScenario(
    "w_note_person_only", "Note on Tyler Briggs: he signed off on the technical review.",
    "person-only note, no deal mentioned",
    _exp(agents=("reader", "writer"),
         write_tools=("create_note", "create_note_target"),
         link_targets=("targetPersonId",)),
))

# --- Group 5: writer — tasks (with date resolution) (5) -------------------
_add(OrchScenario(
    "w_task_person", "Create a task to call Alex Rivera next Tuesday.",
    "task linked to a person + relative date resolution",
    _exp(agents=("reader", "writer"),
         write_tools=("create_task", "create_task_target"),
         write_any_of=("resolve_date",),
         link_targets=("targetPersonId",)),
))
_add(OrchScenario(
    "w_task_deal", "Add a task on the Stripe deal: send the SOC 2 report by Friday.",
    "task linked to a deal + date",
    _exp(agents=("reader", "writer"),
         write_tools=("create_task", "create_task_target"),
         link_targets=("targetOpportunityId",)),
))
_add(OrchScenario(
    "w_task_person_deal",
    "Create a task to follow up with Rachel Kim on the Datadog deal next week.",
    "task linked to person AND deal",
    _exp(agents=("reader", "writer"),
         write_tools=("create_task", "create_task_target"),
         link_targets=("targetPersonId", "targetOpportunityId")),
))
_add(OrchScenario(
    "w_task_self", "Add a task for myself to prep the Figma proposal tomorrow.",
    "task assigned to the current user (no person lookup strictly needed)",
    _exp(agents=("writer",),
         write_tools=("create_task",)),
))
_add(OrchScenario(
    "w_task_company", "Create a task on the Notion account to review pricing options.",
    "task linked to a company",
    _exp(agents=("reader", "writer"),
         write_tools=("create_task", "create_task_target"),
         link_targets=("targetCompanyId",)),
))

# --- Group 6: writer — updates (8) ----------------------------------------
_add(OrchScenario(
    "w_update_amount", "Update the Notion deal amount to $30,000.",
    "resolve deal -> update opportunity amount",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_opportunity", "update_one_opportunity")),
))
_add(OrchScenario(
    "w_update_close_date", "Push the Figma deal close date out by two weeks.",
    "update close date + relative date resolution",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_opportunity", "update_one_opportunity")),
))
_add(OrchScenario(
    "w_advance_stage", "Advance the Stripe Analytics deal to the Meeting stage.",
    "dedicated advance_deal_stage path, not a generic update",
    _exp(agents=("reader", "writer"),
         write_tools=("advance_deal_stage",)),
))
_add(OrchScenario(
    "w_update_person_title",
    "Update Tyler Briggs's title to Principal Design Systems Lead.",
    "resolve person -> update person field",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_person", "update_one_person")),
))
_add(OrchScenario(
    "w_update_person_phone", "Set Kevin Cho's phone number to (415) 555-0188.",
    "update person phone (PII value must be masked to the LLM)",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_person", "update_one_person"),
         pii_must_mask=("(415) 555-0188", "415) 555-0188")),
))
_add(OrchScenario(
    "w_update_company_employees", "Update Notion's employee count to 220.",
    "resolve company -> update company field",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_company", "update_one_company")),
))
_add(OrchScenario(
    "w_update_person_city", "Change Alex Rivera's city to New York.",
    "update person city",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_person", "update_one_person")),
))
_add(OrchScenario(
    "w_update_deal_poc",
    "Set the point of contact on the Datadog deal to James Okonkwo.",
    "resolve deal AND person, then update the deal relation",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_opportunity", "update_one_opportunity")),
))

# --- Group 7: writer — creates; routing discipline (5) --------------------
_add(OrchScenario(
    "w_create_company", "Create a new company called Globex, domain globex.com.",
    "pure create — NEVER 'find' a record you are creating",
    _exp(agents=("writer",), forbid_agents=("reader",),
         write_any_of=("create_company", "create_one_company")),
))
_add(OrchScenario(
    "w_create_person_at_existing",
    "Add a new contact Dana Lee, dana.lee@figma.com, working at Figma.",
    "create person, but the EXISTING company must be resolved first",
    _exp(agents=("reader", "writer"),
         write_any_of=("create_person", "create_one_person"),
         pii_must_mask=("Dana Lee", "dana.lee@figma.com")),
))
_add(OrchScenario(
    "w_create_opportunity",
    "Create a new opportunity 'Datadog Expansion' worth $40k on the Datadog account.",
    "create opportunity on an existing (resolved) company",
    _exp(agents=("reader", "writer"),
         write_any_of=("create_opportunity", "create_one_opportunity")),
))
_add(OrchScenario(
    "w_create_two_people",
    "Add two contacts at Stripe: Sam Diaz (sam@stripe.com) and Lee Wu (lee@stripe.com).",
    "bulk create_many for same-type entities",
    _exp(agents=("reader", "writer"),
         write_any_of=("create_many_people", "create_people", "create_person"),
         pii_must_mask=("Sam Diaz", "sam@stripe.com", "Lee Wu", "lee@stripe.com")),
))
_add(OrchScenario(
    "w_onboard",
    "Onboard a new client: company Initech, contact Peter Gibbons (peter@initech.com), "
    "deal worth $50,000.",
    "composite onboard_new_client — pure create, no reader",
    _exp(agents=("writer",), forbid_agents=("reader",),
         write_any_of=("onboard_new_client",),
         pii_must_mask=("Peter Gibbons", "peter@initech.com")),
))

# --- Group 8: writer — composite writes (5) -------------------------------
_add(OrchScenario(
    "w_close_deal_won", "Mark the Figma deal as won.",
    "terminal stage move = tier-3, should pause for approval",
    _exp(agents=("reader", "writer"),
         write_any_of=("close_deal", "advance_deal_stage"),
         expect_interrupt=True),
))
_add(OrchScenario(
    "w_change_budget", "Set Airbnb's annual budget to $250,000.",
    "composite change_company_budget (or primitive update)",
    _exp(agents=("reader", "writer"),
         write_any_of=("change_company_budget", "update_company", "update_one_company")),
))
_add(OrchScenario(
    "w_schedule_review", "Schedule an account review for the Stripe account in two weeks.",
    "composite schedule_account_review (or task)",
    _exp(agents=("reader", "writer"),
         write_any_of=("schedule_account_review", "create_task")),
))
_add(OrchScenario(
    "w_send_proposal_followup",
    "Send the proposal for the Notion deal and follow up in 3 days.",
    "composite send_proposal_followup (or note+task)",
    _exp(agents=("reader", "writer"),
         write_any_of=("send_proposal_followup", "create_task", "create_note")),
))
_add(OrchScenario(
    "w_reassign", "Reassign the Notion account to Marcus Webb.",
    "composite reassign_account",
    _exp(agents=("reader", "writer"),
         write_any_of=("reassign_account", "update_company", "update_one_company")),
))

# --- Group 9: multi-intent chains (5) -------------------------------------
_add(OrchScenario(
    "m_advance_and_note",
    "Advance the Stripe deal to Meeting and add a note that security finally cleared.",
    "two writes: advance + linked note, one resolve",
    _exp(agents=("reader", "writer"),
         write_tools=("advance_deal_stage", "create_note", "create_note_target"),
         link_targets=("targetOpportunityId",)),
))
_add(OrchScenario(
    "m_lookup_then_task",
    "Who's the point of contact on the Airbnb deal? Create a task to email them this week.",
    "read the POC, then create a task linked to that person",
    _exp(agents=("reader", "writer"),
         write_tools=("create_task", "create_task_target"),
         link_targets=("targetPersonId",)),
))
_add(OrchScenario(
    "m_overview_then_note",
    "Give me an overview of the Notion account, then log a note that we discussed pricing.",
    "composite read then a linked write",
    _exp(agents=("reader", "writer"),
         read_tools=("get_company_overview",),
         write_tools=("create_note", "create_note_target")),
))
_add(OrchScenario(
    "m_two_field_update",
    "Update the Figma deal to $70k and push the close date to next month.",
    "two field updates on one resolved deal",
    _exp(agents=("reader", "writer"),
         write_any_of=("update_opportunity", "update_one_opportunity")),
))
_add(OrchScenario(
    "m_create_and_task",
    "Add a new contact Nora Vance (nora@datadog.com) at Datadog, then create a task to "
    "introduce her on the Datadog deal.",
    "create a person, then a task linked to the new person AND the deal",
    _exp(agents=("reader", "writer"),
         write_any_of=("create_person", "create_one_person"),
         write_tools=("create_task", "create_task_target"),
         pii_must_mask=("Nora Vance", "nora@datadog.com")),
))

# --- Group 10: routing discipline / edge / negative (4) -------------------
_add(OrchScenario(
    "e_unknown_person", "Find a person named Zachary Quirke.",
    "no CRM match -> reader returns resolution none, no write",
    _exp(agents=("reader",), forbid_agents=("writer",),
         read_resolution="none", no_write=True),
))
_add(OrchScenario(
    "e_readonly_no_write",
    "Just tell me the current stage of the Figma deal — don't change anything.",
    "explicit read-only; writer must NOT be involved",
    _exp(agents=("reader",), forbid_agents=("writer",), no_write=True),
))
_add(OrchScenario(
    "e_create_no_resolve", "Create a brand-new company called Hooli.",
    "create -> writer only; reader must not 'find' the new name",
    _exp(agents=("writer",), forbid_agents=("reader",),
         write_any_of=("create_company", "create_one_company")),
))
_add(OrchScenario(
    "e_chitchat", "Thanks, that's all for now.",
    "no actionable intent -> no delegation at all",
    _exp(forbid_agents=("reader", "writer"), no_write=True),
))


def get(name: str) -> OrchScenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    return SCENARIOS[name]


def list_scenario_names() -> list[str]:
    return list(SCENARIOS.keys())


DEFAULT_SCENARIO = "w_note_person_and_deal"

assert len(SCENARIOS) == 60, f"expected 60 scenarios, got {len(SCENARIOS)}"
