import asyncio, json
from dotenv import load_dotenv
load_dotenv()

from agent.tool_scope import READER_SCOPE, WRITER_SCOPE
from agent.tools.composite_reads import build_composite_read_tools
from agent.tools.workflows import build_write_workflow_tools, build_read_workflow_tools

# ──────────────────────────────────────────────────────────────────
# PASTE REAL UUIDs HERE once you have a workspace running.
# Until then these placeholders will make ID-dependent tools return
# NOT_FOUND / VALIDATION errors, which is expected and handled below.
# ──────────────────────────────────────────────────────────────────
COMPANY_ID = "YOUR-COMPANY-UUID-HERE"
PERSON_ID  = "YOUR-PERSON-UUID-HERE"
OPP_ID     = "YOUR-OPPORTUNITY-UUID-HERE"
MEMBER_ID  = "YOUR-WORKSPACE-MEMBER-UUID"


def show(label, result):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    print(json.dumps(result, indent=2))


async def run(label, tool, args):
    try:
        result = await tool.ainvoke(args)
        show(label, result)
    except Exception as exc:
        show(label, {"ok": False, "error": {"code": "EXCEPTION", "message": str(exc)}})


async def main():
    read_tools = {t.name: t for t in build_composite_read_tools(READER_SCOPE)}
    write_wf   = {t.name: t for t in build_write_workflow_tools(WRITER_SCOPE)}
    read_wf    = {t.name: t for t in build_read_workflow_tools(READER_SCOPE)}

    # ── READ TOOLS ────────────────────────────────────────────────

    # No IDs needed — has a static fallback, always works
    await run("get_pipeline_stages",
              read_tools["get_pipeline_stages"], {})

    await run("get_company_overview",
              read_tools["get_company_overview"], {
                  "company_id": COMPANY_ID
              })

    # No IDs needed
    await run("search_all_records",
              read_tools["search_all_records"], {
                  "query": "Acme",
                  "limit": 3
              })

    await run("get_entity_timeline",
              read_tools["get_entity_timeline"], {
                  "entity_id": COMPANY_ID,
                  "entity_type": "company",
                  "limit": 5
              })

    await run("get_related_entities",
              read_tools["get_related_entities"], {
                  "entity_id": PERSON_ID,
                  "entity_type": "person"
              })

    await run("account_health_check",
              read_wf["account_health_check"], {
                  "company_id": COMPANY_ID
              })

    await run("deal_risk_report",
              read_wf["deal_risk_report"], {
                  "opportunity_id": OPP_ID
              })

    # ── WRITE WORKFLOWS ───────────────────────────────────────────
    # These create real records, so they don't need pre-existing IDs.

    await run("onboard_new_client",
              write_wf["onboard_new_client"], {
                  "company_name":        "Test Corp",
                  "contact_first_name":  "Jane",
                  "contact_last_name":   "Smith",
                  "contact_email":       "jane@testcorp.com",
                  "deal_value_micros":   75_000_000,   # $75,000
                  "currency_code":       "USD",
                  "notes":               "Came in via referral"
              })

    await run("qualify_and_create_deal",
              write_wf["qualify_and_create_deal"], {
                  "person_name":          "Bob Jones",
                  "company_name":         "Jones Industries",
                  "deal_name":            "Jones Q3 Deal",
                  "amount_micros":        30_000_000,   # $30,000
                  "stage":                "SCREENING",
                  "qualification_notes":  "Budget confirmed, decision Q3"
              })

    await run("pipeline_push",
              write_wf["pipeline_push"], {
                  "opportunity_id": OPP_ID,
                  "notes":          "Demo went well, moving to proposal"
              })

    await run("close_deal (won)",
              write_wf["close_deal"], {
                  "opportunity_id": OPP_ID,
                  "outcome":        "won",
                  "reason":         "Beat competitor on price and support",
                  "next_steps":     "Send contract, schedule kickoff",
                  "follow_up_days": 14
              })

    await run("create_meeting_summary",
              write_wf["create_meeting_summary"], {
                  "company_id":    COMPANY_ID,
                  "attendees":     "Alice (us), Bob (client), Carol (client)",
                  "summary":       "Discussed Q3 roadmap and pricing",
                  "action_items":  [
                      "Send updated pricing deck",
                      "Intro call with their VP Engineering",
                      "Follow up on legal review"
                  ],
                  "follow_up_days": 5
              })

    await run("upsert_contact_at_company",
              write_wf["upsert_contact_at_company"], {
                  "person_first_name": "Mark",
                  "person_last_name":  "Taylor",
                  "company_name":      "Taylor Tech",
                  "email":             "mark@taylortech.com",
                  "job_title":         "CTO",
                  "phone":             "5551234567"
              })

    await run("schedule_account_review",
              write_wf["schedule_account_review"], {
                  "company_id":    COMPANY_ID,
                  "review_notes":  "Check renewal status, upsell opportunity",
                  "days_from_now": 30
              })

    await run("change_company_budget",
              write_wf["change_company_budget"], {
                  "company_id":        COMPANY_ID,
                  "new_budget_micros": 120_000_000,  # $120,000
                  "currency_code":     "USD",
                  "reason":            "Upsell closed in Q2"
              })

    await run("send_proposal_followup",
              write_wf["send_proposal_followup"], {
                  "opportunity_id":    OPP_ID,
                  "proposal_summary":  "3-year SaaS contract, $75k/yr, includes onboarding",
                  "follow_up_days":    5
              })

    await run("convert_lead_to_opportunity",
              write_wf["convert_lead_to_opportunity"], {
                  "person_name_or_id":      PERSON_ID,
                  "deal_name":              "New Expansion Deal",
                  "estimated_value_micros": 50_000_000,
                  "currency_code":          "USD"
              })

    await run("reassign_account",
              write_wf["reassign_account"], {
                  "company_id":   COMPANY_ID,
                  "new_owner_id": MEMBER_ID,
                  "reason":       "Territory restructure"
              })

    # ── DANGEROUS — requires two calls ───────────────────────────

    # First call → returns confirmation_token
    await run("bulk_update_deal_stage (call 1 - get token)",
              write_wf["bulk_update_deal_stage"], {
                  "company_id": COMPANY_ID,
                  "new_stage":  "PROPOSAL",
                  "reason":     "Entire account moved to proposal phase"
              })

    # Paste the token from the output above, then uncomment:
    # await run("bulk_update_deal_stage (call 2 - confirm)",
    #           write_wf["bulk_update_deal_stage"], {
    #               "company_id":         COMPANY_ID,
    #               "new_stage":          "PROPOSAL",
    #               "reason":             "Entire account moved to proposal phase",
    #               "confirmation_token": "PASTE-TOKEN-HERE"
    #           })

    await run("emergency_account_escalation",
              write_wf["emergency_account_escalation"], {
                  "company_id":         COMPANY_ID,
                  "issue":              "Client threatening churn",
                  "escalation_details": "3 support tickets unresolved for 2 weeks"
              })

    await run("deal_lost_recovery",
              write_wf["deal_lost_recovery"], {
                  "opportunity_id":    OPP_ID,
                  "recovery_strategy": "New pricing tier launched, re-engage with updated offer",
                  "follow_up_days":    45
              })


asyncio.run(main())
