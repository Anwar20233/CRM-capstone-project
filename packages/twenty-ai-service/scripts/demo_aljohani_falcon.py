"""Live demo: a follow-up "from Ibrahim Aljohani at Falcon Sovereign Partners".

This is the flagship, video-ready demo for the Follow-Up Intelligence Agent. It
is built to make ONE thing land on camera: the agent has a *knowledge graph* of
the whole relationship, and it uses it.

Unlike a toy "reply to an email" demo, the CRM here carries a month of scattered
context — a multi-email thread, two private call notes, a past technical deep-dive
meeting, and an overdue task. On top of that we seed the agent's own derived graph:

  * profile_facts        — the board-meeting deadline, the competitor (Helios)
                           undercutting on price, the champion's real sentiment.
  * profile_relationships — Ibrahim needs sign-off from the board chair.
  * shadow_entities       — Khalid (board chair) and Noura (procurement) are named
                           in emails/notes but were NEVER added to the CRM. The
                           agent tracked them anyway.

Then a fresh email arrives from Ibrahim asking for a final alignment call before
the board votes. The agent connects the dots no human would re-read 30 days of
history for: it knows the board date, knows Helios is the live threat, knows a
board chair named Khalid (who isn't even a CRM contact) wants an ROI story — and
it drafts a reply + proposes the meeting that speaks to all of it.

Flow:
  1. (--seed)  Insert Falcon Sovereign + Ibrahim Aljohani + the opportunity, the
               full thread/notes/meeting/task history, and the agent graph rows.
  2. (--fire)  POST the inbound email to /followup/events exactly like a real
               inbound trigger. The agent reads the deal + graph, checks Sarah's
               calendar against the two requested slots, drafts a reply, and writes
               a PENDING ACTION on the deal.
  3. On screen: open the printed opportunity URL. The Follow-Up widget shows the
               Workflow Card with the drafted reply (addressed to Ibrahim) + the
               proposed 45-min meeting. Click **Accept** → reply sent + meeting booked.

Nothing is special-cased: same seed helpers, same /followup/events trigger, same
Accept path a real inbound email takes. The only "trick" is the script posts the
email instead of it arriving in an inbox.

Run from packages/twenty-ai-service (with the .venv that runs the service):

    .venv/bin/python scripts/demo_aljohani_falcon.py            # seed + fire (default)
    .venv/bin/python scripts/demo_aljohani_falcon.py --seed     # only seed the CRM rows
    .venv/bin/python scripts/demo_aljohani_falcon.py --fire     # only fire the email
    .venv/bin/python scripts/demo_aljohani_falcon.py --reset    # clear prior pending actions, then seed+fire

Prerequisites (you start these):
  * Twenty backend on :3000 and the worker running (the agent calls the bridge),
  * the twenty-ai-service running on :8001 (so the UI can reach /followup/*),
  * the full seed has been run once (seed_data.py) so the rep "Sarah Chen" exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

# Make the package importable when run as a script, and load .env first so the
# DB url + service identity are available.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

# Reuse the real seed machinery so these rows look exactly like every other
# seeded record (same audit columns, same idempotent uuid5 ids, same insert path).
import seed_data as sd  # noqa: E402

# ===========================================================================
# CONFIG — edit these before the demo
# ===========================================================================

# Ibrahim Aljohani — the sender the agent resolves and replies to. This is a
# REAL inbox: when you click Accept, the drafted reply is addressed here.
IBRAHIM_EMAIL = "Ibrahim.rabehh@gmail.com"
IBRAHIM_FIRST = "Ibrahim"
IBRAHIM_LAST = "Aljohani"
IBRAHIM_TITLE = "Chief Investment Officer"

# The company + deal the conversation hangs off of.
COMPANY_NAME = "Falcon Sovereign Partners"
COMPANY_DOMAIN = "falconsovereign.com"
OPPORTUNITY_NAME = "Falcon Sovereign — Portfolio Intelligence Platform"
OPPORTUNITY_AMOUNT_USD = 480_000
OPPORTUNITY_STAGE = "Evaluation"  # maps to Twenty stage MEETING via seed_data.STAGE_MAP

# People named in the history but deliberately NOT created as CRM contacts.
# The agent tracks them as shadow entities — this is the graph showcase.
BOARD_CHAIR_NAME = "Khalid Al-Rashid"
PROCUREMENT_NAME = "Noura Al-Qahtani"

# The competitor the agent should know is the live threat.
COMPETITOR = "Helios Analytics"

# The rep on our side whose calendar is checked and who "sends" the reply.
# Sarah Chen is created by the full seed (seed_data.py).
REP = sd.SARAH

EMAIL_SUBJECT = "Re: Falcon Sovereign — final alignment before the board vote?"

# Timezone the requested times are expressed in (AST / Riyadh, UTC+3) so the
# slots render as the wall-clock times Ibrahim asked for on the Workflow card.
DEMO_TZ = timezone(timedelta(hours=3))
MEETING_DURATION_MINUTES = 45


def _key_dates() -> tuple[datetime, datetime, datetime]:
    """Returns (slot_a, slot_b, board_date).

    slot_a  = next Monday 11:00   (first proposed alignment-call slot)
    slot_b  = next Tuesday 09:00  (second proposed alignment-call slot)
    board   = next Wednesday      (the deadline everything hangs on)

    Computed from "now" so the demo never looks stale.
    """
    now = sd.NOW.astimezone(DEMO_TZ)
    mon_date = (now + timedelta(days=(0 - now.weekday()) % 7 or 7)).date()
    tue_date = mon_date + timedelta(days=1)
    wed_date = mon_date + timedelta(days=2)
    slot_a = datetime.combine(mon_date, datetime.min.time(), DEMO_TZ).replace(hour=11)
    slot_b = datetime.combine(tue_date, datetime.min.time(), DEMO_TZ).replace(hour=9)
    board = datetime.combine(wed_date, datetime.min.time(), DEMO_TZ).replace(hour=10)
    return slot_a, slot_b, board


def _proposed_times() -> list[str]:
    """The two requested slots as ISO-8601 starts — passed to /followup/events so
    check_calendar verifies THESE windows instead of free-picking the rep's slot."""
    slot_a, slot_b, _ = _key_dates()
    return [slot_a.isoformat(), slot_b.isoformat()]


def _email_body() -> str:
    slot_a, slot_b, board = _key_dates()
    slot_a_str = slot_a.strftime("%A %B %-d, %-I:%M %p")
    slot_b_str = slot_b.strftime("%A %B %-d, %-I:%M %p")
    board_str = board.strftime("%A %B %-d")
    return (
        f"Hi {REP.first},\n\n"
        f"This is the home stretch. Our investment committee convenes on {board_str} "
        "to approve the analytics vendor for the coming fiscal year, and I want us "
        "walking in aligned.\n\n"
        f"Before then, can we do a focused 45-minute call? I need to be able to defend "
        f"the risk-modeling depth against the cheaper option on the table — the board "
        "will ask, and price alone is not a story I can win on without the numbers in "
        "hand. If you can bring the benchmark figures, that's exactly what I need.\n\n"
        f"Two windows that work on my side:\n"
        f"  • {slot_a_str}\n"
        f"  • {slot_b_str}\n\n"
        "Whichever suits you — send the invite and I'll make sure the right people "
        "are in the room.\n\n"
        "Best regards,\n"
        f"{IBRAHIM_FIRST} {IBRAHIM_LAST}\n"
        f"{IBRAHIM_TITLE}, {COMPANY_NAME}"
    )


# ===========================================================================
# Deterministic ids (so re-running is idempotent and we can print the URL)
# ===========================================================================

COMPANY_KEY = "falcon_demo"
PERSON_KEY = "falcon_ibrahim"
OPP_KEY = "falcon_portfolio_intel"

COMPANY_ID = sd.uid(f"company:{COMPANY_KEY}")
PERSON_ID = sd.uid(f"person:{PERSON_KEY}")
OPPORTUNITY_ID = sd.uid(f"opportunity:{OPP_KEY}")


def _front_url() -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3001").rstrip("/")
    return f"{base}/object/opportunity/{OPPORTUNITY_ID}"


def _ai_service_url() -> str:
    return os.environ.get("AI_SERVICE_URL", "http://localhost:8001").rstrip("/")


# ===========================================================================
# Seeding — build the full deal history + the agent knowledge graph
# ===========================================================================

SIG = (
    "\n\nBest,\nSarah Chen\nSenior Account Executive | OurCompany\n"
    "sarah.chen@ourcompany.com | (415) 555-0142"
)


def _build_rows() -> None:
    """Register the rep, channels, the Falcon deal + its month of history, and the
    agent's derived knowledge graph into seed_data.ROWS."""
    _, _, board = _key_dates()
    board_str = board.strftime("%B %-d")

    # Rep + channels (idempotent — ON CONFLICT DO NOTHING if the full seed ran).
    sd.build_reps()
    sd.register_rep_handles()
    sd.build_channels()

    # --- Core CRM objects ---
    sd.add_company(key=COMPANY_KEY, name=COMPANY_NAME, domain=COMPANY_DOMAIN,
                   employees=220, owner=REP)
    sd.add_person(key=PERSON_KEY, first=IBRAHIM_FIRST, last=IBRAHIM_LAST,
                  email=IBRAHIM_EMAIL, title=IBRAHIM_TITLE, company_id=COMPANY_ID,
                  city="Riyadh")
    sd.add_opportunity(key=OPP_KEY, name=OPPORTUNITY_NAME, stage_label=OPPORTUNITY_STAGE,
                       amount_usd=OPPORTUNITY_AMOUNT_USD, close_date=sd.ahead(weeks=2),
                       company_id=COMPANY_ID, owner=REP, point_of_contact_id=PERSON_ID)

    # --- The email thread (facts get scattered across these on purpose) ---
    thread = sd.add_thread("falcon", OPPORTUNITY_NAME, sd.ago(weeks=4))

    sd.add_email(
        key="fal1", thread_id=thread, channel_owner=REP,
        subject=f"{OPPORTUNITY_NAME} — demo recap",
        sender=REP.email, to=[IBRAHIM_EMAIL], cc=None, when=sd.ago(weeks=4, hours=2),
        body=(
            f"Hi {IBRAHIM_FIRST},\n\nThank you for the time today. Quick recap of what we "
            "showed: the real-time portfolio analytics, the scenario-based risk modeling, "
            "and the exposure dashboards your team flagged as the biggest gaps in your "
            "current stack.\n\nWhat would be most useful to dig into next?" + SIG
        ),
    )
    sd.add_email(
        key="fal2", thread_id=thread, channel_owner=REP,
        subject=f"Re: {OPPORTUNITY_NAME} — demo recap",
        sender=IBRAHIM_EMAIL, to=[REP.email], cc=None, when=sd.ago(weeks=3, days=3),
        body=(
            f"Hi {REP.first},\n\nGenuinely impressed — the risk modeling is a clear step up "
            "from what we run today. I should be transparent about how decisions get made "
            f"here: our investment committee approves vendors quarterly, and the next vote "
            f"is {board_str}. That is the date that matters.\n\nKhalid, our board chair, will "
            "want a crisp ROI story — he is numbers-first and skeptical of anything that "
            f"reads as a 'nice to have'. Help me build that case.\n\nBest,\n{IBRAHIM_FIRST}\n"
            f"{IBRAHIM_TITLE}, {COMPANY_NAME}"
        ),
    )
    sd.add_email(
        key="fal3", thread_id=thread, channel_owner=REP,
        subject=f"Re: {OPPORTUNITY_NAME} — ROI model attached",
        sender=REP.email, to=[IBRAHIM_EMAIL], cc=None, when=sd.ago(weeks=3),
        body=(
            f"Hi {IBRAHIM_FIRST},\n\nAttached is the ROI model built around your portfolio "
            "size — it isolates the risk-modeling time savings and the drawdown the scenario "
            "engine would have flagged early last cycle. Happy to tailor any assumption for "
            "Khalid's read.\n\nWould a technical deep-dive with your team help before the "
            "committee date?" + SIG
        ),
    )

    # --- Past meeting that already happened (relationship momentum) ---
    deep_dive_start = sd.ago(weeks=2).replace(hour=10, minute=0, second=0, microsecond=0)
    sd.add_calendar_event(
        key="falcon_deepdive",
        title="Falcon Sovereign — Technical Deep Dive (Risk Modeling)",
        starts_at=deep_dive_start,
        ends_at=deep_dive_start + timedelta(minutes=60),
        attendees=[
            (REP.email, None, REP.member_id),
            (IBRAHIM_EMAIL, PERSON_ID, None),
        ],
        description=(
            "Walkthrough of the scenario-based risk engine and exposure dashboards for "
            "Ibrahim's analysts ahead of the committee vote."
        ),
    )

    # --- Private call note: the real intel (competitor + procurement pressure) ---
    sd.add_note(
        key="faln1", title="Call with Ibrahim — Helios is the live threat, not product fit",
        when=sd.ago(weeks=2, days=1), opportunity_id=OPPORTUNITY_ID, person_id=PERSON_ID,
        company_id=COMPANY_ID,
        body=(
            f"called Ibrahim after the deep dive. he's sold on the product — said the risk "
            f"modeling is 'clearly ahead' of what they have. the threat is {COMPETITOR}: they "
            "came in materially cheaper and procurement (Noura) is pushing the cheaper option "
            "hard on cost alone. Ibrahim's read: if we arm him with the risk-modeling "
            "benchmark numbers, the board sides with us over Helios because the depth is "
            "undeniable. price is the board's worry, not Ibrahim's. this is winnable if we "
            "get the numbers in his hands before the committee."
        ),
    )
    sd.add_email(
        key="fal4", thread_id=thread, channel_owner=REP,
        subject=f"Re: {OPPORTUNITY_NAME} — differentiation on risk modeling",
        sender=IBRAHIM_EMAIL, to=[REP.email], cc=None, when=sd.ago(days=10),
        body=(
            f"{REP.first} — heads up: {COMPETITOR} sent a revised, lower quote and procurement "
            "is leaning on it. I'm still pushing for you internally because the capability gap "
            "is real, but I need the differentiation in writing — specifically the risk-modeling "
            f"benchmark — to hold the line.\n\n{IBRAHIM_FIRST}"
        ),
    )
    sd.add_email(
        key="fal5", thread_id=thread, channel_owner=REP,
        subject=f"Re: {OPPORTUNITY_NAME} — differentiation brief",
        sender=REP.email, to=[IBRAHIM_EMAIL], cc=None, when=sd.ago(days=7),
        body=(
            f"Hi {IBRAHIM_FIRST},\n\nSending the differentiation brief now and pulling the full "
            "risk-modeling benchmark together — it's the cleanest apples-to-apples we have. Want "
            "to make sure it lands before the committee." + SIG
        ),
    )

    # --- The thing that's slipping: overdue task the agent should surface ---
    sd.add_task(
        key="falcon_benchmark",
        title="Send Ibrahim the risk-modeling benchmark before the committee vote",
        body=(
            f"Ibrahim needs the head-to-head risk-modeling benchmark vs {COMPETITOR} to defend "
            f"the decision to the board on {board_str}. Pull the numbers and get them to him "
            "with time to prep — this is the deciding artifact."
        ),
        due_at=sd.ago(days=2), status="TODO", when=sd.ago(days=7),
        assignee=REP, opportunity_id=OPPORTUNITY_ID, person_id=PERSON_ID,
    )

    # --- Agent knowledge graph (followup_agent schema) ---
    # Shadow entities: named in the history, never added as CRM contacts.
    sd.add_shadow(
        key="falcon_khalid", opportunity_id=OPPORTUNITY_ID, name=BOARD_CHAIR_NAME,
        email_address=None, title_or_role="Board chair / investment committee",
        company_crm_id=COMPANY_ID, aliases=["Khalid"], mention_count=2, status="detected",
        first_seen_at=sd.ago(weeks=3, days=3), last_seen_at=sd.ago(weeks=2, days=1),
    )
    sd.add_shadow(
        key="falcon_noura", opportunity_id=OPPORTUNITY_ID, name=PROCUREMENT_NAME,
        email_address=None, title_or_role="Procurement lead",
        company_crm_id=COMPANY_ID, aliases=["Noura"], mention_count=1, status="detected",
        first_seen_at=sd.ago(weeks=2, days=1), last_seen_at=sd.ago(days=10),
    )

    sd.add_fact(
        key="falcon-f1", opportunity_id=OPPORTUNITY_ID, entity_type="opportunity",
        entity_crm_id=OPPORTUNITY_ID, fact_type="deadline",
        fact_value=f"Investment committee votes on the analytics vendor on {board_str} — the hard decision date",
        confidence=0.95, source_type="email", source_id=sd.uid("message:fal2"),
        extracted_at=sd.ago(weeks=3, days=3),
    )
    sd.add_fact(
        key="falcon-f2", opportunity_id=OPPORTUNITY_ID, entity_type="opportunity",
        entity_crm_id=OPPORTUNITY_ID, fact_type="competitor",
        fact_value=f"{COMPETITOR} is the live threat — came in materially cheaper and sent a revised lower quote; procurement is leaning on cost",
        confidence=0.9, source_type="email", source_id=sd.uid("message:fal4"),
        extracted_at=sd.ago(days=10),
    )
    sd.add_fact(
        key="falcon-f3", opportunity_id=OPPORTUNITY_ID, entity_type="opportunity",
        entity_crm_id=OPPORTUNITY_ID, fact_type="objection",
        fact_value="Decision risk is price, not product fit; risk-modeling benchmark numbers are the artifact that wins the board",
        confidence=0.85, source_type="note", source_id=sd.uid("note:faln1"),
        extracted_at=sd.ago(weeks=2, days=1),
    )
    sd.add_fact(
        key="falcon-f4", opportunity_id=OPPORTUNITY_ID, entity_type="person",
        entity_crm_id=PERSON_ID, fact_type="sentiment",
        fact_value="Champion — sold on the product ('clearly ahead'); actively defending us internally against the cheaper option",
        confidence=0.9, source_type="note", source_id=sd.uid("note:faln1"),
        extracted_at=sd.ago(weeks=2, days=1),
    )
    sd.add_fact(
        key="falcon-f5", opportunity_id=OPPORTUNITY_ID, entity_type="opportunity",
        entity_crm_id=OPPORTUNITY_ID, fact_type="decision_power",
        fact_value=f"{BOARD_CHAIR_NAME} (board chair, not a CRM contact) is numbers-first and skeptical; wants a crisp ROI story",
        confidence=0.8, source_type="email", source_id=sd.uid("message:fal2"),
        extracted_at=sd.ago(weeks=3, days=3),
    )

    sd.add_relationship(
        key="falcon-r1", opportunity_id=OPPORTUNITY_ID, from_id=PERSON_ID, to_id=COMPANY_ID,
        relationship_type="needs_approval_from",
        description=f"Ibrahim needs the investment committee (chaired by {BOARD_CHAIR_NAME}) to approve the vendor",
        confidence=0.9, source_type="email", first_seen_at=sd.ago(weeks=3, days=3),
    )
    sd.add_relationship(
        key="falcon-r2", opportunity_id=OPPORTUNITY_ID, from_id=PERSON_ID, to_id=OPPORTUNITY_ID,
        relationship_type="champions",
        description=f"Ibrahim is championing the deal internally against the cheaper {COMPETITOR} option; {PROCUREMENT_NAME} (procurement) is pushing Helios on cost",
        confidence=0.85, source_type="note", first_seen_at=sd.ago(weeks=2, days=1),
    )


async def _seed(conn: asyncpg.Connection) -> uuid.UUID:
    await conn.set_type_codec(
        "jsonb", encoder=lambda v: v, decoder=json.loads, schema="pg_catalog"
    )
    ws_schema, workspace_id = await sd._discover_target(conn)
    print(f"  workspace schema : {ws_schema}")
    print(f"  workspace id     : {workspace_id}")

    _build_rows()

    total = 0
    async with conn.transaction():
        # Reuse seed_data's full FK-safe insert order; insert whatever we built.
        for schema, table in sd.INSERT_ORDER:
            rows = sd.ROWS.get((schema, table), [])
            if not rows:
                continue
            real_schema = sd._resolve_schema(schema, ws_schema)
            columns = list(rows[0].keys())
            col_sql = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            query = (
                f'INSERT INTO "{real_schema}"."{table}" ({col_sql}) '
                f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
            )
            records = [
                tuple(sd._resolve_value(c, row.get(c), workspace_id) for c in columns)
                for row in rows
            ]
            await conn.executemany(query, records)
            total += len(records)
            print(f"  {real_schema}.{table}: {len(records)} row(s)")
    print(f"  seeded {total} row(s).")
    return workspace_id


async def _reset_pending(conn: asyncpg.Connection) -> None:
    """Expire prior pending actions on this deal so the demo starts with a single,
    fresh Workflow card (the UI lists only status='pending')."""
    try:
        result = await conn.execute(
            f"UPDATE {sd.AGENT_SCHEMA}.followup_pending_actions "
            "SET status = 'expired' "
            "WHERE opportunity_id = $1 AND status = 'pending'",
            OPPORTUNITY_ID,
        )
        count = result.split()[-1] if result else "0"
        print(f"  expired {count} prior pending action(s) for this deal.")
    except asyncpg.UndefinedTableError:
        print("  (no followup_pending_actions table yet — nothing to clear.)")


# ===========================================================================
# Firing — post the inbound email exactly like a real trigger
# ===========================================================================


async def _fire(workspace_id: uuid.UUID) -> dict:
    payload = {
        "email_id": f"demo-falcon-{uuid.uuid4().hex[:8]}",
        "workspace_id": str(workspace_id),
        "sender_email": IBRAHIM_EMAIL,
        "subject": EMAIL_SUBJECT,
        "body": _email_body(),
        # Safety net: pass the deal explicitly so the run never halts even if
        # sender→deal resolution is fuzzy. The agent still re-extracts facts.
        "opportunity_id": str(OPPORTUNITY_ID),
        "owner_user_id": str(REP.user_id),
        # The two slots Ibrahim asked for, parsed to ISO — so check_calendar
        # verifies THESE windows against Sarah's calendar instead of free-picking.
        "proposed_times": _proposed_times(),
        "duration_minutes": MEETING_DURATION_MINUTES,
        "urgency": "high",
    }
    url = f"{_ai_service_url()}/followup/events"
    print(f"\n  POST {url}")
    print(f"  from: {IBRAHIM_EMAIL}  subject: {EMAIL_SUBJECT!r}")
    print(f"  requested slots: {', '.join(_proposed_times())}")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        result = resp.json()
    print(f"  run status       : {result.get('status')}")
    print(f"  pending action id: {result.get('pending_action_id')}")
    if result.get("error"):
        print(f"  error            : {result['error']}")
    return result


# ===========================================================================
# Main
# ===========================================================================


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seed", action="store_true", help="only seed the CRM rows")
    parser.add_argument("--fire", action="store_true", help="only fire the email trigger")
    parser.add_argument("--reset", action="store_true",
                        help="clear prior pending actions on this deal first")
    args = parser.parse_args()

    # Default (no flags) = do the whole thing.
    do_seed = args.seed or not (args.seed or args.fire)
    do_fire = args.fire or not (args.seed or args.fire)

    dsn = os.environ.get(
        "PG_DATABASE_URL", "postgres://postgres:postgres@localhost:5432/default"
    )

    print("=" * 72)
    print("  Falcon Sovereign × Ibrahim Aljohani — Follow-Up demo")
    print("=" * 72)

    workspace_id = None
    conn = await asyncpg.connect(dsn)
    try:
        _, workspace_id = await sd._discover_target(conn)
        if args.reset:
            print("\n[reset]")
            await _reset_pending(conn)
        if do_seed:
            print("\n[seed]")
            workspace_id = await _seed(conn)
    finally:
        await conn.close()

    if do_fire:
        print("\n[fire]")
        await _fire(workspace_id)

    print("\n" + "=" * 72)
    print("  Open this opportunity in Twenty and click Accept on the Workflow card:")
    print(f"  {_front_url()}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
