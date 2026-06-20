"""Orchestrator Agent test cases for QA / regression coverage.

A flat catalog of named, self-contained test cases that a harness can iterate
over and grade. This module drives ``agent/orchestrator.py``'s
``Orchestrator.handle(user_message)``, which routes a single user utterance to
the Reader / Writer / Followup / Researcher sub-agents and, through them, to
CRM tools via ``execute_tool``.

Situation types covered:
  - Happy-path single-entity writes (resolve -> write -> confirm)
  - Disambiguation (multiple candidate records)
  - Not-found / halt paths
  - Tier-3 destructive writes gated behind ``confirmation_token``
  - Read-only lookups and analytics (no Writer involved)
  - Stub agents (Followup, Researcher) that are wired but not fully implemented
  - Validation failures (bad email format, missing permission)
  - Infra failures (tool timeout, agent exception)
  - Multi-turn context (pronoun / handle reuse across turns)

Grounded in:
  - ``agent/orchestrator.py`` (``Orchestrator.handle``, entity masking, disambiguation)
  - ``agent/agent_registry.py`` (Reader, Writer, Followup[stub], Researcher[stub])
  - ``agent/crm_tools.py`` (``get_tool_catalog``, ``learn_tools``, ``execute_tool``,
    ``get_current_user``, and the ``find_one_*``/``find_*``/``create_*``/``update_*``/
    ``delete_*``/``group_by_*`` entity tools reached through ``execute_tool``)
  - ``agent/workers/write_policy.py`` / ``write_gate.py`` (tier 1/2 auto-approve,
    tier 3 requires ``confirmation_token``)
  - ``followup/orchestrator/tasks.py`` (``check_calendar``, ``draft_email``,
    ``write_note``, ``create_task``, ``validate_opportunity_change``)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


# The deterministic stage sequence a normal write-shaped request runs end to end,
# and the shorter traces left by a read-only lookup or a halt (not-found / missing
# auth / disambiguation pending user reply).
WRITE_PATH: tuple[str, ...] = (
    "intent_detection",
    "entity_extraction",
    "mask_pii",
    "agent_selection",
    "reader_resolve",
    "validate_resolution",
    "writer_invoke",
    "tool_execute",
    "audit_log",
    "response_generation",
)
READ_ONLY_PATH: tuple[str, ...] = (
    "intent_detection",
    "entity_extraction",
    "mask_pii",
    "agent_selection",
    "reader_resolve",
    "response_generation",
)
DISAMBIGUATION_HALT_PATH: tuple[str, ...] = (
    "intent_detection",
    "entity_extraction",
    "mask_pii",
    "agent_selection",
    "reader_resolve",
    "disambiguation_prompt",
)
NOT_FOUND_HALT_PATH: tuple[str, ...] = (
    "intent_detection",
    "entity_extraction",
    "mask_pii",
    "agent_selection",
    "reader_resolve",
)


@dataclass(frozen=True)
class AgentInvocation:
    order: int
    agent: str
    purpose: str


@dataclass(frozen=True)
class ToolInvocation:
    order: int
    tool: str
    purpose: str
    inputs: dict


@dataclass(frozen=True)
class OrchestratorTestCase:
    """One QA test case for the Orchestrator Agent.

    Field names track the requested template 1:1 (``expected_*`` sections) so a
    harness — or a human reading a diff — can match this object straight back to
    the spec without translation.
    """

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
    # Set on halted/negative-shaped cases: the actual sub-agents/tools that would
    # run are a strict prefix of the happy-path lists above (e.g. Reader only, no
    # Writer) — callers should assert AGAINST these lists, not skip them.


def _tc(
    *,
    id: str,
    scenario: str,
    user_input: str,
    intent: str,
    workflow: tuple[str, ...],
    agents: tuple[AgentInvocation, ...],
    tools: tuple[ToolInvocation, ...],
    output: str,
    validations: tuple[str, ...],
    success: tuple[str, ...],
    negatives: tuple[str, ...] = (),
) -> OrchestratorTestCase:
    return OrchestratorTestCase(
        id=id,
        scenario=scenario,
        user_input=user_input,
        expected_intent=intent,
        expected_workflow=workflow,
        expected_agents=agents,
        expected_tools=tools,
        expected_output=output,
        validation_rules=validations,
        success_criteria=success,
        negative_cases=negatives,
    )


TEST_CASES: dict[str, OrchestratorTestCase] = {
    # --- Happy-path single-entity writes ---
    "update_person_email_single_match": _tc(
        id="TC_ORCH_001",
        scenario="Update a person's email when the name resolves to exactly one CRM record.",
        user_input="Update Ahmed Al-Qahtani's email to ahmed.q@company.com",
        intent="update_person_email",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve 'Ahmed Al-Qahtani' to a unique person record"),
            AgentInvocation(2, "Writer", "Apply the email update to the resolved record"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Search by name to get candidate records",
                            {"name": "Ahmed Al-Qahtani", "limit": 5}),
            ToolInvocation(2, "execute_tool", "Run the update via the generic CRM tool gateway",
                            {"tool": "update_person",
                             "tool_args": {"id": "<resolved-person-id>",
                                           "email": "ahmed.q@company.com"}}),
        ),
        output="Email for Ahmed Al-Qahtani has been successfully updated to ahmed.q@company.com.",
        validations=(
            "Person ID must be resolved via Reader before any write is attempted.",
            "find_people must return exactly one candidate for this path to be valid.",
            "New email must pass RFC 5322 format validation before execute_tool is called.",
            "Write must be classified tier 1/2 (routine field update) — no confirmation_token required.",
        ),
        success=(
            "Writer is never invoked before Reader returns a single resolved record.",
            "execute_tool is called with the resolved person id, not the raw name.",
            "Final response names the person and the new email value.",
        ),
        negatives=(
            "Person not found -> see not_found_person_update.",
            "Multiple matches -> see update_person_email_disambiguation.",
        ),
    ),
    "create_new_opportunity": _tc(
        id="TC_ORCH_002",
        scenario="Create a new deal/opportunity tied to an existing company.",
        user_input="Create a new opportunity called 'Acme Q3 Expansion' for Acme Corp worth $50k",
        intent="create_opportunity",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve 'Acme Corp' to a company record"),
            AgentInvocation(2, "Writer", "Create the opportunity linked to the resolved company"),
        ),
        tools=(
            ToolInvocation(1, "find_companies", "Search by name to get the company id",
                            {"name": "Acme Corp", "limit": 5}),
            ToolInvocation(2, "execute_tool", "Create the opportunity record",
                            {"tool": "create_opportunity",
                             "tool_args": {"name": "Acme Q3 Expansion",
                                           "companyId": "<resolved-company-id>",
                                           "amount": 50000}}),
        ),
        output="Created opportunity 'Acme Q3 Expansion' for Acme Corp ($50,000).",
        validations=(
            "Company must resolve to a single record before create_opportunity is called.",
            "Amount must parse to a positive numeric value.",
            "Opportunity name must be non-empty.",
        ),
        success=(
            "create_opportunity receives a real companyId, never a bare company name.",
            "Response echoes the opportunity name, linked company, and amount.",
        ),
    ),
    "advance_deal_stage": _tc(
        id="TC_ORCH_003",
        scenario="Move an existing opportunity to the next pipeline stage.",
        user_input="Move the Acme Q3 Expansion deal to 'Negotiation'",
        intent="update_opportunity_stage",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve 'Acme Q3 Expansion' to a unique opportunity"),
            AgentInvocation(2, "Writer", "Apply the stage change"),
        ),
        tools=(
            ToolInvocation(1, "find_opportunities", "Search by name",
                            {"name": "Acme Q3 Expansion", "limit": 5}),
            ToolInvocation(2, "validate_opportunity_change", "Confirm target stage is a legal transition",
                            {"opportunityId": "<resolved-opportunity-id>", "targetStage": "Negotiation"}),
            ToolInvocation(3, "execute_tool", "Persist the stage change",
                            {"tool": "update_opportunity",
                             "tool_args": {"id": "<resolved-opportunity-id>", "stage": "Negotiation"}}),
        ),
        output="Acme Q3 Expansion moved to Negotiation.",
        validations=(
            "Opportunity must resolve to one record.",
            "validate_opportunity_change must pass before execute_tool runs — stage transitions are not freeform.",
        ),
        success=(
            "Stage transition is validated before being written.",
            "Response confirms both the deal name and new stage.",
        ),
    ),
    # --- Disambiguation ---
    "update_person_email_disambiguation": _tc(
        id="TC_ORCH_004",
        scenario="Email update request where the name matches more than one CRM record.",
        user_input="Update John Smith's email to john.s@company.com",
        intent="update_person_email",
        workflow=DISAMBIGUATION_HALT_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Search for 'John Smith' and return all candidates"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Search by name",
                            {"name": "John Smith", "limit": 5}),
        ),
        output=(
            "I found 2 people named John Smith: (1) John Smith — Acme Corp, "
            "(2) John Smith — Globex Inc. Which one did you mean?"
        ),
        validations=(
            "Writer must never be invoked while Reader resolution is 'multiple'.",
            "All candidates must be surfaced with a disambiguating attribute (company, role, or email domain).",
            "Pending email/value from the original request must be held and reapplied once the user disambiguates.",
        ),
        success=(
            "Orchestrator halts after reader_resolve and asks a clarifying question.",
            "No execute_tool call occurs in this turn.",
            "On the next turn, the user's selection binds to the held update payload and the write completes.",
        ),
    ),
    # --- Not found / halt ---
    "not_found_person_update": _tc(
        id="TC_ORCH_005",
        scenario="Email update request for a name with zero CRM matches.",
        user_input="Update Zorglub Quux's email to zorglub@nowhere.example",
        intent="update_person_email",
        workflow=NOT_FOUND_HALT_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Search for 'Zorglub Quux' and find no matches"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Search by name",
                            {"name": "Zorglub Quux", "limit": 5}),
        ),
        output="I couldn't find anyone named Zorglub Quux in the CRM. Could you check the spelling or provide more context?",
        validations=(
            "Writer must never be invoked when Reader resolution is 'none'.",
            "Response must not fabricate a person id or claim a partial update occurred.",
        ),
        success=(
            "Pipeline halts cleanly after reader_resolve with resolution='none'.",
            "No execute_tool call occurs.",
        ),
        negatives=("Reader returns 'none' -> see this case as the canonical negative.",),
    ),
    # --- Tier-3 destructive write gating ---
    "delete_opportunity_with_confirmation": _tc(
        id="TC_ORCH_006",
        scenario="Delete request for a deal, where the user has already supplied a confirmation token from a prior turn.",
        user_input="Yes, delete it. confirmation_token=conf-9f31",
        intent="delete_opportunity",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Re-resolve the opportunity referenced earlier in the conversation"),
            AgentInvocation(2, "Writer", "Execute the gated delete using the supplied confirmation_token"),
        ),
        tools=(
            ToolInvocation(1, "execute_tool", "Delete the opportunity, tier-3 gated",
                            {"tool": "delete_opportunity",
                             "tool_args": {"id": "<resolved-opportunity-id>"},
                             "confirmation_token": "conf-9f31"}),
            ToolInvocation(2, "AuditLogger", "Record the destructive action",
                            {"action": "delete_opportunity", "entityId": "<resolved-opportunity-id>"}),
        ),
        output="Deleted opportunity Acme Q3 Expansion.",
        validations=(
            "delete_opportunity is classified tier 3 by write_policy and MUST carry a valid confirmation_token.",
            "confirmation_token must match the one issued for this specific entity + operation pair (no token reuse across entities).",
            "Audit log entry must be written before the success response is returned.",
        ),
        success=(
            "Delete executes only because a valid, matching confirmation_token is present.",
            "Audit log call occurs for the destructive action.",
        ),
    ),
    "delete_opportunity_without_confirmation": _tc(
        id="TC_ORCH_007",
        scenario="Delete request for a deal with no confirmation_token yet — orchestrator must ask for explicit confirmation rather than deleting.",
        user_input="Delete the Acme Q3 Expansion deal",
        intent="delete_opportunity",
        workflow=(
            "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
            "reader_resolve", "validate_resolution", "write_policy_gate",
        ),
        agents=(
            AgentInvocation(1, "Reader", "Resolve the opportunity by name"),
            AgentInvocation(2, "Writer", "Classify the delete as tier 3 and halt for confirmation"),
        ),
        tools=(
            ToolInvocation(1, "find_opportunities", "Search by name",
                            {"name": "Acme Q3 Expansion", "limit": 5}),
        ),
        output=(
            "Deleting 'Acme Q3 Expansion' is a permanent action. Reply 'yes, delete it' to confirm, "
            "or 'cancel' to stop."
        ),
        validations=(
            "execute_tool for delete_opportunity must never be called without a confirmation_token already present in the request.",
            "A fresh confirmation_token (or confirmation prompt) must be issued/requested per delete, not reused from an unrelated prior delete.",
        ),
        success=(
            "No destructive execute_tool call happens in this turn.",
            "Response explicitly asks for confirmation and names the entity that would be deleted.",
        ),
        negatives=("Missing confirmation_token on a tier-3 write -> canonical negative for write_policy gating.",),
    ),
    # --- Read-only lookups / analytics (no Writer) ---
    "lookup_company_info": _tc(
        id="TC_ORCH_008",
        scenario="Pure read request — no update implied.",
        user_input="What's the latest activity on the Acme Corp account?",
        intent="lookup_company_activity",
        workflow=READ_ONLY_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve Acme Corp and gather recent related activity (notes, tasks, opportunities)"),
        ),
        tools=(
            ToolInvocation(1, "find_companies", "Resolve the company",
                            {"name": "Acme Corp", "limit": 5}),
            ToolInvocation(2, "find_notes", "Fetch recent notes linked to the company",
                            {"companyId": "<resolved-company-id>", "limit": 10}),
        ),
        output="Acme Corp: 1 open opportunity (Negotiation), last note 3 days ago re: pricing discussion.",
        validations=(
            "Writer must never be invoked for a read-only intent.",
            "Response must be grounded only in records actually returned by Reader tools — no invented activity.",
        ),
        success=(
            "Only Reader-side tools are called; no execute_tool write call occurs.",
            "Summary reflects real, returned records.",
        ),
    ),
    "analytics_group_by_stage": _tc(
        id="TC_ORCH_009",
        scenario="Aggregate analytics query over the pipeline.",
        user_input="How many open deals do we have per stage?",
        intent="pipeline_analytics",
        workflow=READ_ONLY_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Run an aggregate query grouping opportunities by stage"),
        ),
        tools=(
            ToolInvocation(1, "execute_tool", "Aggregate opportunities by stage",
                            {"tool": "group_by_opportunities",
                             "tool_args": {"groupBy": ["stage"], "aggregateOperation": "count",
                                           "filter": {"isWon": False, "isLost": False}}}),
        ),
        output="Open pipeline by stage: Discovery (4), Proposal (2), Negotiation (1).",
        validations=(
            "Aggregation must filter out won/lost deals unless the user explicitly asked to include them.",
            "Numbers in the response must match exactly what group_by_opportunities returned.",
        ),
        success=(
            "Single read-only aggregate tool call; no per-record fetch loop.",
            "Reported counts sum correctly and match the tool output.",
        ),
    ),
    # --- Stub agents ---
    "schedule_meeting_via_followup_stub": _tc(
        id="TC_ORCH_010",
        scenario="A scheduling request that should route to the Followup agent (currently a stub).",
        user_input="Set up a 30-minute call with Rachel Kim next week to discuss the reference request",
        intent="schedule_meeting",
        workflow=(
            "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
            "reader_resolve", "followup_invoke",
        ),
        agents=(
            AgentInvocation(1, "Reader", "Resolve Rachel Kim to a person/deal record"),
            AgentInvocation(2, "Followup", "[STUB] Intended to check calendar and draft an invite"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Resolve Rachel Kim",
                            {"name": "Rachel Kim", "limit": 5}),
            ToolInvocation(2, "check_calendar", "Find open slots next week (stub — not wired to a live calendar yet)",
                            {"trigger": {"personId": "<resolved-person-id>", "durationMinutes": 30}}),
        ),
        output=(
            "Scheduling isn't fully wired up yet — I've noted that you want a 30-minute call with "
            "Rachel Kim next week about the reference request, but I can't book it automatically yet."
        ),
        validations=(
            "Orchestrator must surface the Followup stub's limitation rather than silently no-op or fabricate a booked meeting.",
            "Reader resolution of the person must still happen, and still be reported in the response, even though the action itself is stubbed.",
        ),
        success=(
            "Response is honest about the stub limitation — no false claim of a created calendar event.",
            "Reader-side resolution still completes and is reflected in the response.",
        ),
        negatives=("Followup agent is a stub — treat any 'meeting booked' claim as a failed run.",),
    ),
    "enrich_company_via_researcher_stub": _tc(
        id="TC_ORCH_011",
        scenario="An enrichment/background-research request that should route to the Researcher agent (currently a stub).",
        user_input="Find background info on Acme Corp's recent funding round",
        intent="enrich_company_research",
        workflow=(
            "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
            "reader_resolve", "researcher_invoke",
        ),
        agents=(
            AgentInvocation(1, "Reader", "Resolve Acme Corp to a CRM company record"),
            AgentInvocation(2, "Researcher", "[STUB] Intended to enrich with external research"),
        ),
        tools=(
            ToolInvocation(1, "find_companies", "Resolve Acme Corp",
                            {"name": "Acme Corp", "limit": 5}),
        ),
        output="External research/enrichment isn't available yet — I can only show what's already in the CRM for Acme Corp.",
        validations=(
            "Orchestrator must not call out to any external web/search tool that doesn't exist yet.",
            "Response must clearly distinguish CRM-grounded data from the unavailable enrichment capability.",
        ),
        success=(
            "No fabricated external research appears in the response.",
            "Stub limitation is communicated plainly.",
        ),
    ),
    # --- Validation failures ---
    "invalid_email_format_rejected": _tc(
        id="TC_ORCH_012",
        scenario="Update request where the supplied email fails basic format validation.",
        user_input="Update Ahmed Al-Qahtani's email to not-an-email",
        intent="update_person_email",
        workflow=(
            "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
            "reader_resolve", "validate_resolution", "input_validation_failed",
        ),
        agents=(
            AgentInvocation(1, "Reader", "Resolve Ahmed Al-Qahtani to a unique person record"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Resolve the person",
                            {"name": "Ahmed Al-Qahtani", "limit": 5}),
        ),
        output="'not-an-email' doesn't look like a valid email address. Can you confirm the correct one?",
        validations=(
            "Email format must be validated BEFORE execute_tool/update_person is ever called.",
            "Writer must not be invoked when the new value fails validation.",
        ),
        success=(
            "No execute_tool write call occurs.",
            "Response identifies the specific invalid value, not a generic error.",
        ),
        negatives=("Malformed email -> canonical negative for field-level input validation.",),
    ),
    "unauthorized_write_attempt": _tc(
        id="TC_ORCH_013",
        scenario="A user without write permission attempts a destructive or sensitive update.",
        user_input="Delete the Acme Corp company record",
        intent="delete_company",
        workflow=(
            "intent_detection", "entity_extraction", "mask_pii", "agent_selection",
            "reader_resolve", "permission_check_failed",
        ),
        agents=(
            AgentInvocation(1, "Reader", "Resolve Acme Corp to a unique company record"),
        ),
        tools=(
            ToolInvocation(1, "get_current_user", "Determine the requesting workspace member and their role", {}),
            ToolInvocation(2, "find_companies", "Resolve the company",
                            {"name": "Acme Corp", "limit": 5}),
        ),
        output="You don't have permission to delete company records. Ask a workspace admin to do this.",
        validations=(
            "get_current_user (or equivalent permission context) must be checked before any tier-3 write is attempted.",
            "execute_tool for delete_company must never run for a user lacking the required role.",
        ),
        success=(
            "Permission check happens before, not after, attempting the delete.",
            "No execute_tool delete call occurs.",
        ),
        negatives=("Permission denied -> canonical negative for authorization gating.",),
    ),
    # --- Infra failures ---
    "crm_tool_timeout": _tc(
        id="TC_ORCH_014",
        scenario="The CRM API times out while executing a write.",
        user_input="Update Ahmed Al-Qahtani's email to ahmed.q@company.com",
        intent="update_person_email",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve Ahmed Al-Qahtani to a unique person record"),
            AgentInvocation(2, "Writer", "Attempt the email update; the underlying call times out"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Resolve the person",
                            {"name": "Ahmed Al-Qahtani", "limit": 5}),
            ToolInvocation(2, "execute_tool", "Attempt update_person; times out before responding",
                            {"tool": "update_person",
                             "tool_args": {"id": "<resolved-person-id>", "email": "ahmed.q@company.com"}}),
        ),
        output=(
            "I wasn't able to confirm whether Ahmed Al-Qahtani's email was updated — the CRM didn't "
            "respond in time. Please check the record or try again."
        ),
        validations=(
            "On timeout, the orchestrator must NOT report success — an unconfirmed write is reported as unconfirmed, not as done.",
            "No duplicate write retry should fire automatically without idempotency protection.",
        ),
        success=(
            "Response is honest about the unknown/failed state.",
            "No false 'updated successfully' message is produced.",
        ),
        negatives=("Tool timeout mid-write -> canonical negative for partial-failure honesty.",),
    ),
    "subagent_exception": _tc(
        id="TC_ORCH_015",
        scenario="The Writer sub-agent raises an unhandled exception during execution.",
        user_input="Update Ahmed Al-Qahtani's email to ahmed.q@company.com",
        intent="update_person_email",
        workflow=WRITE_PATH,
        agents=(
            AgentInvocation(1, "Reader", "Resolve Ahmed Al-Qahtani to a unique person record"),
            AgentInvocation(2, "Writer", "Raises an unhandled exception before completing the write"),
        ),
        tools=(
            ToolInvocation(1, "find_people", "Resolve the person",
                            {"name": "Ahmed Al-Qahtani", "limit": 5}),
        ),
        output="Something went wrong while trying to update that record. Nothing was changed — please try again.",
        validations=(
            "An exception in Writer must be caught at the orchestrator boundary, not surfaced as a raw stack trace to the user.",
            "Orchestrator must not claim the write succeeded when the sub-agent errored.",
        ),
        success=(
            "User-facing response is a clean, non-leaking error message.",
            "No partial/garbage state is reported as a confirmed update.",
        ),
        negatives=("Unhandled sub-agent exception -> canonical negative for failure containment.",),
    ),
    # --- Multi-turn context ---
    "multi_turn_pronoun_reference": _tc(
        id="TC_ORCH_016",
        scenario="A follow-up turn that refers back to a person resolved in the previous turn via pronoun, relying on the shared EntityHandleMap.",
        user_input="Actually, also update his phone number to +1-555-0182",
        intent="update_person_phone",
        workflow=(
            "intent_detection", "entity_extraction", "resolve_pronoun_via_handle_map",
            "mask_pii", "agent_selection", "validate_resolution", "writer_invoke",
            "tool_execute", "audit_log", "response_generation",
        ),
        agents=(
            AgentInvocation(1, "Writer", "Apply the phone update to the person resolved earlier in the conversation"),
        ),
        tools=(
            ToolInvocation(1, "execute_tool", "Update the phone field on the already-resolved person",
                            {"tool": "update_person",
                             "tool_args": {"id": "<person-id-from-prior-turn>", "phone": "+1-555-0182"}}),
        ),
        output="Updated Ahmed Al-Qahtani's phone number to +1-555-0182.",
        validations=(
            "'his' must resolve through the existing EntityHandleMap from the prior turn — Reader must NOT be re-invoked from scratch with a guessed name.",
            "If no person was resolved in a recent-enough prior turn, the orchestrator must ask who 'his' refers to instead of guessing.",
        ),
        success=(
            "Pronoun resolves to the same person id used in the previous turn without a new name-based search.",
            "Response names the actual person, not just 'his'.",
        ),
    ),
}


def get(case_id: str) -> OrchestratorTestCase:
    if case_id not in TEST_CASES:
        raise KeyError(f"unknown test case {case_id!r}; choose from {', '.join(TEST_CASES)}")
    return TEST_CASES[case_id]


def list_case_names() -> list[str]:
    return sorted(TEST_CASES.keys())


def list_by_id() -> dict[str, OrchestratorTestCase]:
    """Re-key the catalog by its human-facing TC_ORCH_### id."""
    return {case.id: case for case in TEST_CASES.values()}


# Straightforward single-match write — good default for single-case smoke runs.
DEFAULT_CASE = "update_person_email_single_match"
