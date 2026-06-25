"""Orchestrator Agent test-case catalog for end-to-end QA / regression coverage.

A flat catalog of named, self-contained test cases that ``orchestrator_testcase_eval.py``
iterates over and grades. Each case drives ``agent/orchestrator.py``'s
``Orchestrator.handle(user_message)``, which routes a single user utterance to the
Reader / Writer (and the Followup / Researcher stubs) sub-agents and, through them,
to the CRM tools via ``execute_tool``.

This is the runnable, seed-grounded evolution of the original spec catalog. Two
things changed from the spec so the cases actually execute end to end against the
real orchestrator + a seeded Twenty backend:

  1. **Real entities.** Every name/company/deal below is a REAL seeded record
     (see ``seed_data.py``): companies Airbnb / Stripe / Notion / Figma / Datadog,
     their people, and their opportunities. Placeholder names would never resolve.

  2. **Real control flow.** The live orchestrator resolves entities and detects
     ambiguity at MASK TIME (``Orchestrator._resolve_turn`` → ``CRMResolver``),
     BEFORE any sub-agent runs — not via the Reader. So a "multiple matches"
     turn HALTS with a clarifying question and NO delegation occurs. The grader
     knows this; the ``expected_workflow`` path is treated as a behavioural SHAPE
     (write / read-only / disambiguation-halt / not-found-halt), not a literal
     stage-by-stage trace.

Situation types covered (30 cases):
  - Happy-path single-entity writes (resolve -> write -> confirm)
  - Read-only lookups, scalar reads, relational lookups
  - Analytics (group_by) + composite overviews / health checks
  - Not-found / halt paths (no fabricated success)
  - Disambiguation (multiple candidates -> clarifying question)   [needs dup seed]
  - Tier-3 destructive writes gated behind confirmation             [needs confirm]
  - Validation failures (bad email format) -> no silent success
  - Multi-turn context (pronoun / "it" reuse across turns)
  - PII-heavy writes (phone / personal email in note content must be masked)

Run with::

    .venv/bin/python scripts/orchestrator_testcase_eval.py
    .venv/bin/python scripts/orchestrator_testcase_eval.py --case read_person_title --verbose
    .venv/bin/python scripts/orchestrator_testcase_eval.py --include-requires --json out/tc_eval.json
"""

from __future__ import annotations

from dataclasses import dataclass


# The deterministic stage sequence a normal write-shaped request runs end to end,
# and the shorter traces left by a read-only lookup or a halt. The grader treats
# these as a behavioural SHAPE label (see module docstring), not a literal trace.
WRITE_PATH: tuple[str, ...] = (
    "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
    "reader_resolve", "validate_resolution", "writer_invoke", "tool_execute",
    "audit_log", "response_generation",
)
READ_ONLY_PATH: tuple[str, ...] = (
    "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
    "reader_resolve", "response_generation",
)
DISAMBIGUATION_HALT_PATH: tuple[str, ...] = (
    "intent_detection", "entity_extraction", "mask_pii", "disambiguation_prompt",
)
NOT_FOUND_HALT_PATH: tuple[str, ...] = (
    "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
    "reader_resolve",
)


@dataclass(frozen=True)
class AgentInvocation:
    order: int
    agent: str  # "reader" | "writer" | "followup" | "researcher"
    purpose: str


@dataclass(frozen=True)
class ToolInvocation:
    """One expected tool call.

    ``tool`` is the CRM tool name. For writer calls routed through the generic
    gateway, set ``tool="execute_tool"`` and put the real CRM tool in
    ``inputs["tool"]`` with its ``inputs["tool_args"]`` — the grader unwraps it.

    ``inputs`` values may carry placeholders like ``"<resolved-person-id>"``;
    those mark fields the grader asserts were filled with a real resolved UUID
    (the id-first guarantee), NOT a raw name.
    """

    order: int
    tool: str
    purpose: str
    inputs: dict
    # Some requests have several valid tool strategies (e.g. an "overview" can be
    # one composite read OR a resolve + relational reads). When set, the grader
    # passes if ANY of these tools (plus ``tool``) ran — fair credit, not a trap.
    accept_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrchestratorTestCase:
    """One end-to-end QA case for the Orchestrator Agent."""

    id: str
    scenario: str
    user_input: str
    expected_intent: str
    expected_workflow: tuple[str, ...]
    expected_agents: tuple[AgentInvocation, ...]
    expected_tools: tuple[ToolInvocation, ...]
    expected_output: str
    validation_rules: tuple[str, ...]
    success_criteria: tuple[str, ...]
    negative_cases: tuple[str, ...] = ()

    # --- runnable-grader additions (not in the original spec) ---------------
    # Context turns played through the SAME orchestrator BEFORE the graded
    # user_input, to establish cross-turn state (a resolved record, a pending
    # confirmation). Only the final user_input turn is graded.
    prior_turns: tuple[str, ...] = ()
    # Sub-agents that must NEVER be delegated to (routing discipline — e.g. never
    # "find" a record you are about to CREATE).
    forbid_agents: tuple[str, ...] = ()
    # Un-seeded PII the message PLANTS (new emails / phones / outside names) that
    # is WRITE CONTENT, not a lookup key — it must be masked before any LLM sees
    # it. This is the hard leakage gate.
    planted_pii: tuple[str, ...] = ()
    # Substrings the final reply should contain (looser than expected_output,
    # which is an illustrative full sentence).
    output_includes: tuple[str, ...] = ()
    # Env precondition that must hold for this case to run live. The runner SKIPS
    # cases with a requirement unless --include-requires is passed (e.g. a
    # duplicate-name seed for disambiguation, or a manual confirm for delete).
    requires: str | None = None


TEST_CASES: dict[str, OrchestratorTestCase] = {}
_COUNTER = [0]


def _next_id() -> str:
    _COUNTER[0] += 1
    return f"TC_ORCH_{_COUNTER[0]:03d}"


def _read(
    key: str, *, scenario: str, user_input: str, intent: str,
    tools: tuple[ToolInvocation, ...] = (), output_includes: tuple[str, ...] = (),
    expected_output: str = "", prior_turns: tuple[str, ...] = (),
    validations: tuple[str, ...] = (), success: tuple[str, ...] = (),
) -> None:
    """A read-only case: reader only, no writer, no mutation."""
    TEST_CASES[key] = OrchestratorTestCase(
        id=_next_id(), scenario=scenario, user_input=user_input,
        expected_intent=intent, expected_workflow=READ_ONLY_PATH,
        expected_agents=(AgentInvocation(1, "reader", scenario),),
        expected_tools=tools, expected_output=expected_output or scenario,
        validation_rules=validations or ("A pure read mutates nothing.",
                                         "The answer comes from the record, not fabricated."),
        success_criteria=success or ("Reader resolves and returns the value; no writer runs.",),
        forbid_agents=("writer",), output_includes=output_includes,
        prior_turns=prior_turns,
    )


def _write(
    key: str, *, scenario: str, user_input: str, intent: str,
    agents: tuple[AgentInvocation, ...], tools: tuple[ToolInvocation, ...],
    expected_output: str, validations: tuple[str, ...], success: tuple[str, ...],
    output_includes: tuple[str, ...] = (), planted_pii: tuple[str, ...] = (),
    prior_turns: tuple[str, ...] = (), requires: str | None = None,
    negatives: tuple[str, ...] = (),
) -> None:
    TEST_CASES[key] = OrchestratorTestCase(
        id=_next_id(), scenario=scenario, user_input=user_input,
        expected_intent=intent, expected_workflow=WRITE_PATH,
        expected_agents=agents, expected_tools=tools, expected_output=expected_output,
        validation_rules=validations, success_criteria=success,
        output_includes=output_includes, planted_pii=planted_pii,
        prior_turns=prior_turns, requires=requires, negative_cases=negatives,
    )


def _find(order: int, tool: str, name: str) -> ToolInvocation:
    return ToolInvocation(order, tool, f"Resolve '{name}' by name", {"name": name})


# ===========================================================================
# Group 1 — Simple reads: resolve one record by name / email (6)
# ===========================================================================

_read("read_find_person", scenario="Resolve one person by name.",
      user_input="Find John Park.", intent="find_person",
      tools=(_find(1, "find_people", "John Park"),), output_includes=("Park",))

_read("read_find_company", scenario="Resolve one company by name.",
      user_input="Look up Stripe in the CRM.", intent="find_company",
      tools=(_find(1, "find_companies", "Stripe"),), output_includes=("Stripe",))

_read("read_find_opportunity", scenario="Resolve one opportunity by name.",
      user_input="Show me the Notion Workflow Automation deal.", intent="find_opportunity",
      tools=(ToolInvocation(1, "find_opportunities", "Find the deal",
                            {"name": "Notion Workflow Automation"}),),
      output_includes=("Notion",))

_read("read_find_by_email", scenario="Resolve a person by email (PII handle round-trip).",
      user_input="Find the person with email alex.rivera@stripe.com.", intent="find_person",
      tools=(ToolInvocation(1, "find_people", "Resolve by email",
                            {"email": "alex.rivera@stripe.com"}),),
      output_includes=("Rivera",))

_read("read_find_person_figma", scenario="Resolve a person at Figma by name.",
      user_input="Pull up Emma Larsen's record.", intent="find_person",
      tools=(_find(1, "find_people", "Emma Larsen"),), output_includes=("Larsen",))

_read("read_find_company_datadog", scenario="Resolve Datadog by name.",
      user_input="Find the Datadog company record.", intent="find_company",
      tools=(_find(1, "find_companies", "Datadog"),), output_includes=("Datadog",))


# ===========================================================================
# Group 2 — Scalar field reads off a resolved record (7)
# ===========================================================================

_read("read_person_title", scenario="Read a person's job title.",
      user_input="What's Kevin Cho's job title?", intent="read_person_field",
      tools=(_find(1, "find_people", "Kevin Cho"),), output_includes=("Operations",))

_read("read_deal_amount", scenario="Read a deal's value.",
      user_input="What's the deal value on the Figma deal?", intent="read_opportunity_field",
      tools=(ToolInvocation(1, "find_opportunities", "Resolve the Figma deal",
                            {"name": "Figma"}),),
      output_includes=("62",))

_read("read_company_employees", scenario="Read a company's headcount field (not group_by).",
      user_input="How many employees does Airbnb have?", intent="read_company_field",
      tools=(_find(1, "find_companies", "Airbnb"),), output_includes=("6",))

_read("read_deal_stage", scenario="Read a deal's current stage.",
      user_input="What stage is the Stripe Analytics Suite deal in?",
      intent="read_opportunity_field",
      tools=(ToolInvocation(1, "find_opportunities", "Resolve the Stripe deal",
                            {"name": "Stripe — Analytics Suite"}),),
      output_includes=("Screening",))

_read("read_person_company", scenario="Read which company a person belongs to.",
      user_input="Which company does Lisa Huang work at?", intent="read_person_field",
      tools=(_find(1, "find_people", "Lisa Huang"),), output_includes=("Airbnb",))

_read("read_deal_close_date", scenario="Read a deal's close date.",
      user_input="When does the Stripe Analytics deal close?", intent="read_opportunity_field",
      tools=(ToolInvocation(1, "find_opportunities", "Resolve the Stripe deal",
                            {"name": "Stripe Analytics"}),))

_read("read_person_email", scenario="Read a person's email (masked round-trip).",
      user_input="What's the email on file for Maria Santos?", intent="read_person_field",
      tools=(_find(1, "find_people", "Maria Santos"),), output_includes=("notion.com",))


# ===========================================================================
# Group 3 — Relational lookups: decompose the riddle (4)
# ===========================================================================

_read("read_people_at_company", scenario="List the people at a company.",
      user_input="Who works at Notion?", intent="list_company_people",
      tools=(ToolInvocation(1, "find_people", "People whose company is Notion",
                            {"company": "Notion"}, accept_any=("get_related_entities",
                                                               "get_company_overview")),),
      output_includes=("Cho",))

_read("read_deal_owner", scenario="Read the owner/rep on a deal.",
      user_input="Who owns the Airbnb Platform Integration deal?", intent="read_opportunity_field",
      tools=(ToolInvocation(1, "find_opportunities", "Resolve the deal then read owner",
                            {"name": "Airbnb Platform Integration"}),),
      output_includes=("Sarah",))

_read("read_poc_to_deal", scenario="Which opportunity is a person the point of contact for.",
      user_input="What opportunity is John Park the point of contact for?",
      intent="find_opportunity_by_poc",
      tools=(ToolInvocation(1, "find_people", "Resolve John Park to an id first",
                            {"name": "John Park"},
                            accept_any=("find_opportunities",)),),
      output_includes=("Airbnb",))

_read("read_people_at_stripe", scenario="List the people at Stripe.",
      user_input="List the contacts we have at Stripe.", intent="list_company_people",
      tools=(ToolInvocation(1, "find_people", "People whose company is Stripe",
                            {"company": "Stripe"}, accept_any=("get_related_entities",
                                                               "get_company_overview")),),
      output_includes=("Rivera",))


# ===========================================================================
# Group 4 — Analytics & composite reads (4)
# ===========================================================================

_read("analytics_pipeline_by_stage", scenario="Group/count opportunities by stage.",
      user_input="How many opportunities are in each pipeline stage?",
      intent="pipeline_analytics",
      tools=(ToolInvocation(1, "group_by_opportunities", "Aggregate by stage", {},
                            accept_any=("find_opportunities", "search_all_records")),),
      validations=("Analytics is a read — no mutation.",
                   "Counts come from an aggregation or a full scan, not fabricated."),
      success=("Reply lists stages with counts.",))

_read("analytics_pipeline_value", scenario="Total / largest deal value across the pipeline.",
      user_input="What's our biggest open deal right now?", intent="pipeline_analytics",
      tools=(ToolInvocation(1, "find_opportunities", "Scan opportunities by amount",
                            {}, accept_any=("group_by_opportunities", "search_all_records")),),
      output_includes=("85",))

_read("composite_account_overview", scenario="Composite read — a full account overview.",
      user_input="Give me a full overview of the Airbnb account.", intent="account_overview",
      tools=(ToolInvocation(1, "get_company_overview", "Composite read of the cluster",
                            {"companyId": "<resolved-company-id>"},
                            accept_any=("find_companies", "account_health_check",
                                        "get_related_entities", "get_entity_timeline")),),
      output_includes=("Airbnb",),
      validations=("An overview is ONE composite call where possible; no writer.",),
      success=("Reader returns a multi-part overview for the resolved company.",))

_read("composite_health_check", scenario="Account-health/risk overview for a company.",
      user_input="How healthy is the Figma account?", intent="account_health",
      tools=(ToolInvocation(1, "account_health_check", "Composite health read",
                            {"companyId": "<resolved-company-id>"},
                            accept_any=("get_company_overview", "find_companies",
                                        "get_related_entities")),),
      output_includes=("Figma",))


# ===========================================================================
# Group 5 — Multi-turn context: pronoun / handle reuse across turns (3)
# ===========================================================================

_read("multiturn_deal_stage", scenario="Resolve a deal, then ask its stage with 'it'.",
      user_input="What stage is it in?", intent="read_opportunity_field",
      prior_turns=("Find the Notion Workflow Automation deal.",),
      output_includes=("Meeting",),
      validations=("'it' must bind to the deal from the prior turn, not re-resolve.",),
      success=("Reply names the correct stage for the carried deal.",))

_read("multiturn_person_email", scenario="Resolve a person, then ask their email.",
      user_input="And what's her email?", intent="read_person_field",
      prior_turns=("Look up Priya Sharma.",), output_includes=("stripe.com",),
      validations=("'her' binds to the person resolved in the prior turn.",),
      success=("Reply returns the carried person's email.",))

_read("multiturn_company_people", scenario="Resolve a company, then ask who works there.",
      user_input="Who works there?", intent="list_company_people",
      prior_turns=("Pull up the Datadog account.",), output_includes=("Kim",),
      validations=("'there' binds to the company resolved in the prior turn.",),
      success=("Reply lists Datadog's people.",))


# ===========================================================================
# Group 6 — Halt paths: not-found, disambiguation (3)
# ===========================================================================

_write("not_found_person_update", scenario="Email update for a name with zero matches.",
       user_input="Update Zorglub Quux's email to zorglub@nowhere.example",
       intent="update_person_email",
       agents=(AgentInvocation(1, "reader", "Search 'Zorglub Quux' — no matches"),),
       tools=(_find(1, "find_people", "Zorglub Quux"),),
       expected_output="I couldn't find anyone named Zorglub Quux in the CRM.",
       validations=("Writer must never run when resolution is 'none'.",
                    "No fabricated id or partial-update claim.",
                    "The planted email must be masked, never crossing raw."),
       success=("Pipeline halts with no write; reply admits not found.",),
       planted_pii=("zorglub@nowhere.example",),
       negatives=("Reader returns 'none' -> canonical negative.",))
# Override the WRITE shape: this is a not-found HALT (no successful write expected).
TEST_CASES["not_found_person_update"] = OrchestratorTestCase(
    **{**TEST_CASES["not_found_person_update"].__dict__,
       "expected_workflow": NOT_FOUND_HALT_PATH, "forbid_agents": ("writer",)})

_write("not_found_company", scenario="Lookup for a company that doesn't exist.",
       user_input="Show me the account for Hooli Inc.", intent="find_company",
       agents=(AgentInvocation(1, "reader", "Search 'Hooli Inc' — no matches"),),
       tools=(_find(1, "find_companies", "Hooli Inc"),),
       expected_output="I couldn't find a company called Hooli Inc.",
       validations=("No fabricated company record.",),
       success=("Reply admits the company isn't in the CRM; no write.",))
TEST_CASES["not_found_company"] = OrchestratorTestCase(
    **{**TEST_CASES["not_found_company"].__dict__,
       "expected_workflow": NOT_FOUND_HALT_PATH, "forbid_agents": ("writer",)})

TEST_CASES["disambiguation"] = OrchestratorTestCase(
    id=_next_id(),
    scenario="Name matches more than one record -> ask the user to choose.",
    user_input="Update John Smith's email to john.s@company.com",
    expected_intent="update_person_email",
    expected_workflow=DISAMBIGUATION_HALT_PATH,
    expected_agents=(),  # real flow halts at the mask-time resolver, before any agent
    expected_tools=(),
    expected_output="I found more than one 'John Smith'. Which one did you mean?",
    validation_rules=("Writer must never run while the name is ambiguous.",
                      "Each candidate is shown with a distinguishing attribute.",
                      "The pending update value is held and reapplied after the choice."),
    success_criteria=("Orchestrator halts with a clarifying question ('?').",
                      "No write occurs this turn."),
    forbid_agents=("writer",),
    requires="duplicate-name-seed",
)


# ===========================================================================
# Group 7 — Happy-path writes: resolve -> write -> confirm (4)
# ===========================================================================

_write("write_update_person_email", scenario="Update a person's email (single match).",
       user_input="Update John Park's email to john.park.new@airbnb.com",
       intent="update_person_email",
       agents=(AgentInvocation(1, "reader", "Resolve 'John Park'"),
               AgentInvocation(2, "writer", "Apply the email update")),
       tools=(_find(1, "find_people", "John Park"),
              ToolInvocation(2, "execute_tool", "Update via the CRM gateway",
                             {"tool": "update_person",
                              "tool_args": {"id": "<resolved-person-id>",
                                            "email": "john.park.new@airbnb.com"}})),
       expected_output="John Park's email has been updated.",
       output_includes=("John Park",),
       validations=("Person id resolved before the write.",
                    "update_person carries the resolved UUID, never the raw name.",
                    "The new email is write content — masked before any LLM sees it."),
       success=("Writer runs after Reader; update_person succeeds with a real id.",),
       planted_pii=("john.park.new@airbnb.com",))

_write("write_create_opportunity", scenario="Create an opportunity for an existing company.",
       user_input="Create a new opportunity called 'Airbnb Q3 Expansion' for Airbnb worth $50k",
       intent="create_opportunity",
       agents=(AgentInvocation(1, "reader", "Resolve 'Airbnb'"),
               AgentInvocation(2, "writer", "Create the opportunity")),
       tools=(_find(1, "find_companies", "Airbnb"),
              ToolInvocation(2, "execute_tool", "Create the opportunity",
                             {"tool": "create_opportunity",
                              "tool_args": {"name": "Airbnb Q3 Expansion",
                                            "companyId": "<resolved-company-id>",
                                            "amount": 50000}})),
       expected_output="Created opportunity 'Airbnb Q3 Expansion' for Airbnb.",
       output_includes=("Airbnb Q3 Expansion",),
       validations=("Company resolves to one record before create_opportunity.",
                    "create_opportunity receives a real companyId, not a name."),
       success=("Opportunity created and linked to the resolved company id.",))

_write("write_advance_stage", scenario="Advance an opportunity to the next stage.",
       user_input="Move the Stripe Analytics Suite deal to Meeting",
       intent="update_opportunity_stage",
       agents=(AgentInvocation(1, "reader", "Resolve the Stripe deal"),
               AgentInvocation(2, "writer", "Apply the stage change")),
       tools=(ToolInvocation(1, "find_opportunities", "Resolve the deal",
                             {"name": "Stripe — Analytics Suite"}),
              ToolInvocation(2, "execute_tool", "Persist the stage change",
                             {"tool": "update_opportunity",
                              "tool_args": {"id": "<resolved-opportunity-id>",
                                            "stage": "MEETING"},
                              }, accept_any=("advance_deal_stage",))),
       expected_output="Stripe — Analytics Suite moved to Meeting.",
       output_includes=("Meeting",),
       validations=("Opportunity resolved before the write.",
                    "Stage value is a legal pipeline stage."),
       success=("Stage written with the resolved opportunity id.",))

_write("write_note_with_pii", scenario="Add a note to a person with phone + personal email.",
       user_input=("Add a note to John Park: his direct cell is (415) 555-0188 and his "
                   "personal email is jpark.personal@gmail.com — use these for the renewal call."),
       intent="create_note",
       agents=(AgentInvocation(1, "reader", "Resolve 'John Park'"),
               AgentInvocation(2, "writer", "Create the note and LINK it to the person")),
       tools=(_find(1, "find_people", "John Park"),
              ToolInvocation(2, "execute_tool", "Create the note",
                             {"tool": "create_note", "tool_args": {}}),
              ToolInvocation(3, "execute_tool", "Link the note to the person",
                             {"tool": "create_note_target",
                              "tool_args": {"targetPersonId": "<resolved-person-id>"}})),
       expected_output="Added a note to John Park with the renewal-call details.",
       output_includes=("John Park",),
       validations=("Phone + personal email are write content — both masked (hard gate).",
                    "Note LINKED via create_note_target — an unlinked note is a failure."),
       success=("create_note + create_note_target link the note to the person id.",
                "Neither the phone nor the personal email crosses raw."),
       planted_pii=("(415) 555-0188", "jpark.personal@gmail.com"))


# ===========================================================================
# Group 8 — Validation & gated destructive writes (2)
# ===========================================================================

_write("validation_bad_email", scenario="Email update with a malformed value.",
       user_input="Update Emma Larsen's email to not-a-valid-email",
       intent="update_person_email",
       agents=(AgentInvocation(1, "reader", "Resolve 'Emma Larsen'"),),
       tools=(_find(1, "find_people", "Emma Larsen"),),
       expected_output="That doesn't look like a valid email address, so I didn't update it.",
       validations=("A malformed email must NOT be written.",
                    "The reply flags the bad value rather than claiming success."),
       success=("No successful update_person with the invalid value.",))
# Shape: resolve then REFUSE — graded as a no-silent-success halt.
TEST_CASES["validation_bad_email"] = OrchestratorTestCase(
    **{**TEST_CASES["validation_bad_email"].__dict__,
       "expected_workflow": NOT_FOUND_HALT_PATH})

_write("delete_with_confirmation", scenario="Delete a deal — destructive, must be confirmed.",
       user_input="Yes, I confirm — delete it.",
       prior_turns=("Delete the Datadog Infrastructure Monitoring deal",),
       intent="delete_opportunity",
       agents=(AgentInvocation(1, "reader", "Re-resolve the opportunity"),
               AgentInvocation(2, "writer", "Execute the gated delete after confirmation")),
       tools=(ToolInvocation(1, "execute_tool", "Delete the opportunity, tier-3 gated",
                             {"tool": "delete_opportunity",
                              "tool_args": {"id": "<resolved-opportunity-id>"}}),),
       expected_output="Deleted opportunity Datadog — Infrastructure Monitoring Add-on.",
       validations=("delete_opportunity is tier-3 — it pauses for confirmation first.",
                    "The delete runs only after the user confirms in a later turn."),
       success=("First turn surfaces a confirmation; delete executes only after.",),
       requires="manual-confirm")


def get(key: str) -> OrchestratorTestCase:
    if key not in TEST_CASES:
        raise KeyError(f"unknown test case {key!r}; known: {sorted(TEST_CASES)}")
    return TEST_CASES[key]
