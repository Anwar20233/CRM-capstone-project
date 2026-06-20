"""End-to-end live trace for the Follow-Up Agent: email in → profile out.

Drives the REAL pipeline against running services (no fakes):

  * the CRM Reader resolves the sender through the Node bridge → live backend,
  * the extraction + synthesis LLM calls hit the configured real provider,
  * facts / relationships / shadow entities are written to the `followup_agent`
    schema of the live `default` Postgres,

then reads the knowledge graph back through Step 3 (`ProfileService`).

What it prints, in order:
  1. resolved sender / company / candidate deals,
  2. the extraction outcome (chosen deal, facts/relationships/shadows counts),
  3. a DB diff of `followup_agent` (what rows were created / superseded),
  4. the synthesized ProfileNarrative + DealContext for the chosen deal.

Prerequisites (the user starts these):
  * Twenty backend on :3000 (the bridge — NODE_BRIDGE_BASE_URL),
  * the twenty-ai-service env (.env): LLM_* provider + TWENTY_* identity,
  * seeded data (seed_data.py) so the sender resolves to a real person/deal.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_e2e.py
    .venv/bin/python scripts/followup_e2e.py --sender alex.rivera@stripe.com
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

# Make the package importable when run as a script, and load .env before any
# agent import reads os.environ for identity / LLM config.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from scripts.followup_email_scenarios import DEFAULT_SCENARIO, SCENARIOS, get  # noqa: E402

from followup.profile.dependencies import PipelineDeps  # noqa: E402
from followup.profile.extraction import extract_from_email  # noqa: E402
from followup.profile.service import ProfileNotFound, ProfileService  # noqa: E402
from followup.store.repositories import SCHEMA, Database  # noqa: E402


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s : %(message)s",
        datefmt="%H:%M:%S",
    )
    # The interesting traces: reader worker tool calls + our pipeline steps.
    logging.getLogger("followup").setLevel(logging.DEBUG)
    logging.getLogger("agent").setLevel(logging.DEBUG)
    # Silence transport/SDK chatter that would drown the trace.
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


async def _snapshot(db: Database) -> dict[str, dict]:
    """Current followup_agent rows keyed by id, for a before/after diff."""
    async with db.pool.acquire() as conn:
        facts = await conn.fetch(
            f"SELECT id, opportunity_id, entity_type, entity_crm_id, shadow_entity_id, "
            f"fact_type, fact_value, sentiment, confidence, superseded_by "
            f"FROM {SCHEMA}.profile_facts"
        )
        rels = await conn.fetch(
            f"SELECT id, opportunity_id, relationship_type, description FROM {SCHEMA}.profile_relationships"
        )
        shadows = await conn.fetch(
            f"SELECT id, opportunity_id, name, title_or_role, mention_count, status, "
            f"promoted_to_crm_id FROM {SCHEMA}.shadow_entities"
        )
    return {
        "facts": {r["id"]: dict(r) for r in facts},
        "relationships": {r["id"]: dict(r) for r in rels},
        "shadows": {r["id"]: dict(r) for r in shadows},
    }


def _print_diff(before: dict[str, dict], after: dict[str, dict], opportunity_id: str | None) -> None:
    opp = uuid.UUID(opportunity_id) if opportunity_id else None

    def _scope(rows: dict) -> dict:
        if opp is None:
            return rows
        return {k: v for k, v in rows.items() if v.get("opportunity_id") == opp}

    # New facts.
    new_facts = {k: v for k, v in _scope(after["facts"]).items() if k not in before["facts"]}
    print(f"\n  + {len(new_facts)} new fact(s):")
    for f in new_facts.values():
        tag = f["entity_type"]
        who = f["entity_crm_id"] or f["shadow_entity_id"] or "—"
        print(f"      [{f['fact_type']}] {f['fact_value']!r}  "
              f"(sentiment={f['sentiment']}, conf={f['confidence']}, {tag}:{who})")

    # Superseded facts (state changed from null → set).
    superseded = [
        k for k in before["facts"]
        if k in after["facts"]
        and before["facts"][k]["superseded_by"] is None
        and after["facts"][k]["superseded_by"] is not None
    ]
    if superseded:
        print(f"\n  ~ {len(superseded)} fact(s) superseded:")
        for k in superseded:
            print(f"      {before['facts'][k]['fact_value']!r} → replaced")

    new_rels = {k: v for k, v in _scope(after["relationships"]).items() if k not in before["relationships"]}
    print(f"\n  + {len(new_rels)} new relationship(s):")
    for r in new_rels.values():
        print(f"      {r['relationship_type']}  {r['description'] or ''}")

    new_shadows = {k: v for k, v in _scope(after["shadows"]).items() if k not in before["shadows"]}
    print(f"\n  + {len(new_shadows)} new shadow entity(ies):")
    for s in new_shadows.values():
        promoted = f"  PROMOTED→{s['promoted_to_crm_id']}" if s["promoted_to_crm_id"] else ""
        print(f"      {s['name']} ({s['title_or_role'] or 'role?'})  "
              f"mentions={s['mention_count']} status={s['status']}{promoted}")


def _print_dataclass(obj, *, skip: tuple[str, ...] = ()) -> None:
    for key, value in asdict(obj).items():
        if key in skip:
            continue
        if isinstance(value, list):
            print(f"  {key}: [{len(value)} item(s)]")
            for item in value:
                print(f"      - {item}")
        else:
            print(f"  {key}: {value}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        choices=sorted(SCENARIOS),
        help="named email scenario to run (see --list)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    # Optional ad-hoc overrides; default to the chosen scenario's fields.
    parser.add_argument("--sender", default=None, help="override the sender email")
    parser.add_argument("--subject", default=None)
    parser.add_argument("--body", default=None)
    args = parser.parse_args()

    if args.list:
        print("Available email scenarios:\n")
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<28} {scenario.sender}")
            print(f"  {'':<28} → {scenario.exercises}\n")
        return

    scenario = get(args.scenario)
    sender = args.sender or scenario.sender
    subject = args.subject or scenario.subject
    body = args.body or scenario.body

    _configure_logging()
    source_id = str(uuid.uuid4())
    workspace_id = __import__("os").environ.get("TWENTY_WORKSPACE_ID", "")

    _rule(f"INPUT EMAIL — scenario: {scenario.name}")
    print(f"  exercises: {scenario.exercises}")
    print(f"  sender   : {sender}")
    print(f"  subject  : {subject}")
    print(f"  source_id: {source_id}")
    print(f"  workspace: {workspace_id}")
    print("  ---")
    print("  " + body.replace("\n", "\n  "))

    db = await Database.connect()
    try:
        deps = PipelineDeps.create(db)

        _rule("BEFORE — followup_agent snapshot")
        before = await _snapshot(db)
        print(f"  facts={len(before['facts'])}  "
              f"relationships={len(before['relationships'])}  "
              f"shadows={len(before['shadows'])}")

        _rule("RUNNING EXTRACTION (live reader + real LLM)")
        source_text = f"Subject: {subject}\n\n{body}"
        outcome = await extract_from_email(
            workspace_id=workspace_id,
            source_type="email",
            source_id=source_id,
            source_text=source_text,
            sender_email=sender,
            deps=deps,
        )

        _rule("EXTRACTION OUTCOME")
        print(f"  status              : {outcome.status}")
        print(f"  sender_crm_id       : {outcome.sender_crm_id}")
        print(f"  company_crm_id      : {outcome.company_crm_id}")
        print(f"  candidate deals     : {outcome.candidate_opportunity_ids}")
        print(f"  chosen opportunity  : {outcome.opportunity_id}")
        if outcome.reason:
            print(f"  reason              : {outcome.reason}")
        if outcome.extraction is not None:
            e = outcome.extraction
            print(f"  facts created       : {e.facts_created}  (superseded {e.facts_superseded})")
            print(f"  relationships       : {e.relationships_created} created / {e.relationships_updated} updated")
            print(f"  shadows             : {e.shadows_created} created / {e.shadows_updated} matched")
            print(f"  unresolved mentions : {e.unresolved_mentions}")

        _rule("DB DIFF — what the write path changed")
        after = await _snapshot(db)
        _print_diff(before, after, outcome.opportunity_id)

        if not outcome.opportunity_id:
            _rule("READ PATH SKIPPED")
            print("  No opportunity was resolved, so there is no profile to synthesize.")
            return

        _rule("READ PATH — ProfileService.build_profile_narrative (real LLM)")
        service = ProfileService(deps)
        try:
            narrative = await service.build_profile_narrative(outcome.opportunity_id)
        except ProfileNotFound as exc:
            print(f"  ProfileNotFound: {exc}")
            return

        print("\n  --- SYNTHESIZED NARRATIVE ---")
        print("  " + (narrative.narrative or "(empty)").replace("\n", "\n  "))
        print("\n  --- STRUCTURED ---")
        print(f"  risk_score   : {narrative.risk_score}")
        print(f"  generated_at : {narrative.generated_at.isoformat()}")
        print(f"  contacts     : {len(narrative.contacts)}")
        for c in narrative.contacts:
            print(f"      - {c.name} ({c.role}) — {len(c.facts)} fact(s)")
        print(f"  key_facts    : {len(narrative.key_facts)} (top 20, newest first)")
        for f in narrative.key_facts[:8]:
            print(f"      - [{f['fact_type']}] {f['fact_value']}")
        print(f"  relationships: {len(narrative.relationships)}")

        _rule("READ PATH — ProfileService.build_deal_context")
        context = await service.build_deal_context(outcome.opportunity_id)
        print(f"  deal         : {context.opportunity_name}  (stage {context.deal_stage}, ${context.deal_value:,.0f})")
        print(f"  company      : {context.company_name}")
        print(f"  risk_score   : {context.risk_score}")
        print(f"  open_concerns: {len(context.open_concerns)}")
        for c in context.open_concerns:
            print(f"      - {c['fact_value']}")
        print(f"  recent_activities: {len(context.recent_activities)}")
        for a in context.recent_activities[:5]:
            print(f"      - [{a.get('type')}] {a.get('date')}  {a.get('summary')}")
        print(f"  key_relationships: {len(context.key_relationships)}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
