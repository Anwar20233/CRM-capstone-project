"""Composite-tool eval cases — does the reader/writer discover and call them right?

Twenty composite design (see ``tool_schemas.json``):

- Composite **reads** live on the ``objectName: "composite"`` axis (a *meta-entity*
  — a bundle of linked records). The reader discovers them with
  ``get_tool_catalog(object_name="composite")``. **Every composite read needs an
  id**, except ``search_all_records`` which is the id-bootstrap (takes a query).
- Composite **writes** live on the ``operation: "composite"`` axis of a *real*
  ``objectName`` (a *meta-operation* — many writes on one record). The writer
  discovers them with ``get_tool_catalog(object_name=<entity>, operation="composite")``.
- ``debated`` tools are hidden from BOTH agents — never in the catalog, never
  learnable, never executable. The agent must fall back to bridge primitives.

Each case is a behavioural expectation, not a unit test of the plumbing: feed
``prompt`` to ``agent`` (with the listed handles already resolved) and check the
final tool-call trace against ``expect``. ``negative`` cases assert a composite is
NOT reachable / NOT called.

Run shape (harness, once the wiring lands)::

    for case in CASES:
        trace = run_agent(case["agent"], case["prompt"], handles=case["handles"])
        assert_expectation(trace, case)

Fields
------
id          stable case id
agent       "reader" | "writer"
prompt      the natural-language instruction handed to the agent
handles     entity handles already resolved before the agent runs (id-first
            assumption — these come from an earlier reader pass / the orchestrator)
expect      {discovery, tool, args_include}  the composite call we expect, where
              discovery   = the get_tool_catalog(...) filter the agent should use
              tool        = the composite tool name it should execute
              args_include= key args that must be present (values may be handles)
negative    optional {reason, must_not_call} — the tool that must NOT be called
            (debated) or the behaviour we forbid (e.g. calling a read composite
            with no id instead of resolving first)
"""

from __future__ import annotations

CASES: list[dict] = [
    # ----- Reader: composite reads (id present) ----------------------------
    {
        "id": "R01-company-overview",
        "agent": "reader",
        "prompt": "Give me a full overview of Acme Corp.",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite")',
            "tool": "get_company_overview",
            "args_include": {"company_id": "company001.id"},
        },
    },
    {
        "id": "R02-account-health",
        "agent": "reader",
        "prompt": "How healthy is the Acme Corp account right now?",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite", operation="health")',
            "tool": "account_health_check",
            "args_include": {"company_id": "company001.id"},
        },
    },
    {
        "id": "R03-person-timeline",
        "agent": "reader",
        "prompt": "Show me the recent activity timeline for Sarah Connor.",
        "handles": {"person001": {"type": "person", "id": "<sarah-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite", operation="timeline")',
            "tool": "get_entity_timeline",
            "args_include": {"entity_id": "person001.id", "entity_type": "person"},
        },
    },
    {
        "id": "R04-opportunity-related",
        "agent": "reader",
        "prompt": "What records are linked to the Northwind renewal deal?",
        "handles": {"opportunity001": {"type": "opportunity", "id": "<deal-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite", operation="related")',
            "tool": "get_related_entities",
            "args_include": {
                "entity_id": "opportunity001.id",
                "entity_type": "opportunity",
            },
        },
    },
    {
        "id": "R05-company-timeline",
        "agent": "reader",
        "prompt": "Pull the last 5 notes and tasks for Acme Corp in one view.",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite", operation="timeline")',
            "tool": "get_entity_timeline",
            "args_include": {
                "entity_id": "company001.id",
                "entity_type": "company",
                "limit": 5,
            },
        },
    },
    # ----- Reader: id bootstrap (no id yet) --------------------------------
    {
        "id": "R06-search-bootstrap",
        "agent": "reader",
        "prompt": "Find anything in the CRM matching 'Volkov' — could be a person, company, or deal.",
        "handles": {},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="composite", operation="search")',
            "tool": "search_all_records",
            "args_include": {"query": "Volkov"},
        },
    },
    {
        "id": "R07-overview-needs-resolve-first",
        "agent": "reader",
        "prompt": "Give me a full overview of Globex (no id known yet).",
        "handles": {},
        "expect": {
            # Correct path: resolve the id first (search_all_records or find_companies),
            # THEN call get_company_overview with the resolved id.
            "discovery": 'get_tool_catalog(object_name="composite", operation="search")',
            "tool": "search_all_records",
            "args_include": {"query": "Globex"},
        },
        "negative": {
            "reason": "composite reads require an id — must not invent one or call with a name",
            "must_not_call": "get_company_overview(company_id='Globex')",
        },
    },
    # ----- Reader: must NOT reach writes or debated ------------------------
    {
        "id": "R08-reader-no-write-composite",
        "agent": "reader",
        "prompt": "Onboard a new client called Initech.",
        "handles": {},
        "expect": {
            # Reader recognises this as a write and redirects to the writer.
            "discovery": None,
            "tool": None,
            "args_include": {},
        },
        "negative": {
            "reason": "onboard_new_client is a write composite — out of reader scope",
            "must_not_call": "onboard_new_client",
        },
    },
    {
        "id": "R09-reader-debated-deal-risk-hidden",
        "agent": "reader",
        "prompt": "Run a deep risk report on the Northwind renewal deal.",
        "handles": {"opportunity001": {"type": "opportunity", "id": "<deal-uuid>"}},
        "expect": {
            # deal_risk_report is debated → hidden. Reader falls back to a composite
            # it CAN use (related/timeline) or primitive finds to assemble context.
            "discovery": 'get_tool_catalog(object_name="composite", operation="related")',
            "tool": "get_related_entities",
            "args_include": {"entity_id": "opportunity001.id"},
        },
        "negative": {
            "reason": "deal_risk_report is debated and never exposed",
            "must_not_call": "deal_risk_report",
        },
    },
    {
        "id": "R10-pipeline-stages-metadata",
        "agent": "reader",
        "prompt": "What are the valid opportunity pipeline stages?",
        "handles": {},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="opportunity", operation="metadata")',
            "tool": "get_pipeline_stages",
            "args_include": {},
        },
    },
    # ----- Writer: composite writes (id present, id-first) -----------------
    {
        "id": "W01-onboard-new-client",
        "agent": "writer",
        "prompt": "Onboard a new client: company Initech, contact Peter Gibbons "
                  "(peter@initech.com), deal worth $50,000.",
        "handles": {},  # pure-create: nothing to resolve, takes names
        "expect": {
            "discovery": 'get_tool_catalog(object_name="company", operation="composite")',
            "tool": "onboard_new_client",
            "args_include": {
                "company_name": "Initech",
                "contact_first_name": "Peter",
                "contact_last_name": "Gibbons",
                "contact_email": "peter@initech.com",
                "deal_value_micros": 50000000000,
            },
        },
    },
    {
        "id": "W02-close-deal-won",
        "agent": "writer",
        "prompt": "Mark the Northwind renewal deal as won.",
        "handles": {"opportunity001": {"type": "opportunity", "id": "<deal-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="opportunity", operation="composite")',
            "tool": "close_deal",
            "args_include": {"opportunity_id": "opportunity001.id", "outcome": "won"},
        },
    },
    {
        "id": "W03-change-company-budget",
        "agent": "writer",
        "prompt": "Set Acme Corp's annual budget to $250,000.",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="company", operation="composite")',
            "tool": "change_company_budget",
            "args_include": {
                "company_id": "company001.id",
                "new_budget_micros": 250000000000,
            },
        },
    },
    {
        "id": "W04-schedule-account-review",
        "agent": "writer",
        "prompt": "Schedule an account review for Acme Corp in 2 weeks.",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="company", operation="composite")',
            "tool": "schedule_account_review",
            "args_include": {"company_id": "company001.id", "days_from_now": 14},
        },
    },
    {
        "id": "W05-send-proposal-followup",
        "agent": "writer",
        "prompt": "Send the proposal for the Northwind renewal and follow up in 3 days. "
                  "Proposal: annual plan at $120k.",
        "handles": {"opportunity001": {"type": "opportunity", "id": "<deal-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="opportunity", operation="composite")',
            "tool": "send_proposal_followup",
            "args_include": {
                "opportunity_id": "opportunity001.id",
                "follow_up_days": 3,
            },
        },
    },
    {
        "id": "W06-reassign-account",
        "agent": "writer",
        "prompt": "Transfer the Acme Corp account and all its open deals to Dana.",
        "handles": {
            "company001": {"type": "company", "id": "<acme-uuid>"},
            "person002": {"type": "person", "id": "<dana-uuid>"},
        },
        "expect": {
            "discovery": 'get_tool_catalog(object_name="company", operation="composite")',
            "tool": "reassign_account",
            "args_include": {
                "company_id": "company001.id",
                "new_owner_id": "person002.id",
            },
        },
    },
    {
        "id": "W07-bulk-update-deal-stage",
        "agent": "writer",
        "prompt": "Move every open deal at Acme Corp to the Meeting stage.",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            "discovery": 'get_tool_catalog(object_name="company", operation="composite")',
            "tool": "bulk_update_deal_stage",
            "args_include": {"company_id": "company001.id", "new_stage": "MEETING"},
        },
    },
    # ----- Writer: must NOT reach reads or debated ------------------------
    {
        "id": "W08-writer-debated-pipeline-push-hidden",
        "agent": "writer",
        "prompt": "Advance the Northwind renewal deal to the next stage.",
        "handles": {"opportunity001": {"type": "opportunity", "id": "<deal-uuid>"}},
        "expect": {
            # pipeline_push is debated → hidden. Writer uses the dedicated bridge
            # advance path instead.
            "discovery": 'get_tool_catalog(object_name="opportunity", operation="advance_stage")',
            "tool": "advance_deal_stage",
            "args_include": {"deal_id": "opportunity001.id"},
        },
        "negative": {
            "reason": "pipeline_push is debated and never exposed",
            "must_not_call": "pipeline_push",
        },
    },
    {
        "id": "W09-writer-debated-qualify-hidden",
        "agent": "writer",
        "prompt": "Qualify a new deal for Jane Doe at a brand-new company FreshCo.",
        "handles": {},
        "expect": {
            # qualify_and_create_deal is debated (mixes lookup + write). The writer
            # must build it from primitives: create_company, create_person,
            # create_opportunity.
            "discovery": 'get_tool_catalog(object_name="company", operation="create")',
            "tool": "create_company",
            "args_include": {"name": "FreshCo"},
        },
        "negative": {
            "reason": "qualify_and_create_deal is debated and never exposed",
            "must_not_call": "qualify_and_create_deal",
        },
    },
    {
        "id": "W10-writer-no-read-composite",
        "agent": "writer",
        "prompt": "What's the overview of Acme Corp?",
        "handles": {"company001": {"type": "company", "id": "<acme-uuid>"}},
        "expect": {
            # Read request — out of writer scope; writer refuses / defers to reader.
            "discovery": None,
            "tool": None,
            "args_include": {},
        },
        "negative": {
            "reason": "get_company_overview is a read composite — out of writer scope",
            "must_not_call": "get_company_overview",
        },
    },
]

assert len(CASES) == 20, f"expected 20 eval cases, got {len(CASES)}"
