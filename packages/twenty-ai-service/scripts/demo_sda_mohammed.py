"""Live demo: a follow-up "from Dr. Mohammed at SDA" that you can ACCEPT on screen.

This stages a single, believable end-to-end story for showing the Follow-Up
Intelligence Agent in the real Twenty UI:

  1. (--seed)  Inserts an "SDA" company, "Dr. Mohammed" as a contact, and an
               opportunity (the bootcamp capstone) into the live Twenty workspace.
  2. (--fire)  Posts an inbound email *as if it came from Mohammed* to the
               follow-up service. The agent reads the deal, checks the rep's
               calendar, drafts a reply, and writes a PENDING ACTION on the deal.
  3. On screen: open the printed opportunity URL in Twenty. The Follow-Up
               Intelligence widget shows a Workflow Card with the drafted reply
               + the proposed meeting. Click **Accept** → the reply is sent to
               Mohammed and the meeting is booked.

Nothing here is special-cased for the demo: it uses the same seed helpers, the
same `/followup/events` trigger, and the same Accept path a real inbound email
would take. The only "trick" is that the email is posted by this script instead
of arriving in an inbox.

Run from packages/twenty-ai-service (with the .venv that runs the service):

    .venv/bin/python scripts/demo_sda_mohammed.py            # seed + fire (default)
    .venv/bin/python scripts/demo_sda_mohammed.py --seed     # only seed the CRM rows
    .venv/bin/python scripts/demo_sda_mohammed.py --fire     # only fire the email
    .venv/bin/python scripts/demo_sda_mohammed.py --reset    # clear prior pending actions, then seed+fire

Prerequisites (you start these):
  * Twenty backend on :3000 and the worker running (the agent calls the bridge),
  * the twenty-ai-service running on :8001 (so the UI can reach /followup/*),
  * the full seed has been run once (seed_data.py) so the rep "Sarah Chen" exists.

Edit the CONFIG block below to set Mohammed's real email before the demo.
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

# Dr. Mohammed — the sender the agent will resolve and reply to.
# >>> Put his real address here when you have it. <<<
MOHAMMED_EMAIL = "mohammed@sda.example"
MOHAMMED_FIRST = "Mohammed"
MOHAMMED_LAST = "Al-Otaibi"
MOHAMMED_TITLE = "Lead Instructor"

# The company + deal the conversation hangs off of.
SDA_COMPANY_NAME = "SDA — Saudi Digital Academy"
SDA_DOMAIN = "sda.example"
OPPORTUNITY_NAME = "SDA Agentic Bootcamp — Capstone Review"
OPPORTUNITY_AMOUNT_USD = 75_000
OPPORTUNITY_STAGE = "Evaluation"  # maps to Twenty stage MEETING via seed_data.STAGE_MAP

# The rep on our side whose calendar is checked and who "sends" the reply.
# Sarah Chen is created by the full seed (seed_data.py).
REP = sd.SARAH

# The inbound email — written to read like a genuine request that needs both a
# reply AND a meeting, which is what makes the Accept button do two things.
EMAIL_SUBJECT = "Re: SDA bootcamp capstone — can we lock a review session?"

# Timezone the requested times are expressed in. Matches the demo workspace's
# display tz so "2:00pm" in the email shows as 2:00 PM on the Workflow card.
DEMO_TZ = timezone(timedelta(hours=3))  # AST / Riyadh (UTC+3)
MEETING_DURATION_MINUTES = 30


def _meeting_slots() -> tuple[datetime, datetime]:
    """The two concrete slots the email requests: next Tue 2:00pm and Wed 10:00am.

    Computed from "now" (in DEMO_TZ) so the demo never looks stale, and returned as
    timezone-aware datetimes so they round-trip as ISO-8601 with an offset.
    """
    now = sd.NOW.astimezone(DEMO_TZ)
    tue_date = (now + timedelta(days=(1 - now.weekday()) % 7 or 7)).date()
    wed_date = tue_date + timedelta(days=1)
    tue_2pm = datetime.combine(tue_date, datetime.min.time(), DEMO_TZ).replace(hour=14)
    wed_10am = datetime.combine(wed_date, datetime.min.time(), DEMO_TZ).replace(hour=10)
    return tue_2pm, wed_10am


def _proposed_times() -> list[str]:
    """The requested slots as ISO-8601 starts — passed to /followup/events so the
    agent checks the rep's calendar against THESE windows, not its own free pick."""
    return [slot.isoformat() for slot in _meeting_slots()]


def _email_body() -> str:
    tue, wed = _meeting_slots()
    tue_str = tue.strftime("%A %B %-d")
    wed_str = wed.strftime("%A %B %-d")
    return (
        f"Hi {REP.first},\n\n"
        "Great progress on the agentic capstone — the team is impressed with how "
        "the follow-up agent is shaping up.\n\n"
        "Before the cohort demo I'd like to sit down for 30 minutes to walk through "
        "the final scope and the evaluation criteria. Could we do "
        f"{tue_str} 2:00–4:00pm or {wed_str} morning?\n\n"
        "Send over a calendar invite for whichever works and a short recap of what "
        "you'll cover.\n\n"
        "Best,\n"
        f"{MOHAMMED_FIRST}\n"
        "SDA"
    )


# ===========================================================================
# Deterministic ids (so re-running is idempotent and we can print the URL)
# ===========================================================================

COMPANY_KEY = "sda_demo"
PERSON_KEY = "sda_mohammed"
OPP_KEY = "sda_capstone_review"

COMPANY_ID = sd.uid(f"company:{COMPANY_KEY}")
PERSON_ID = sd.uid(f"person:{PERSON_KEY}")
OPPORTUNITY_ID = sd.uid(f"opportunity:{OPP_KEY}")


def _front_url() -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3001").rstrip("/")
    return f"{base}/object/opportunity/{OPPORTUNITY_ID}"


def _ai_service_url() -> str:
    return os.environ.get("AI_SERVICE_URL", "http://localhost:8001").rstrip("/")


# ===========================================================================
# Seeding — insert only the SDA rows (reusing seed_data's builders + insert)
# ===========================================================================


def _build_rows() -> None:
    """Register the rep + SDA company/person/opportunity into seed_data.ROWS."""
    # The rep (Sarah Chen). Idempotent — ON CONFLICT DO NOTHING if the full seed
    # already created her.
    sd.build_reps()
    sd.register_rep_handles()

    sd.add_company(
        key=COMPANY_KEY,
        name=SDA_COMPANY_NAME,
        domain=SDA_DOMAIN,
        employees=120,
        owner=REP,
    )
    sd.add_person(
        key=PERSON_KEY,
        first=MOHAMMED_FIRST,
        last=MOHAMMED_LAST,
        email=MOHAMMED_EMAIL,
        title=MOHAMMED_TITLE,
        company_id=COMPANY_ID,
        city="Riyadh",
    )
    sd.add_opportunity(
        key=OPP_KEY,
        name=OPPORTUNITY_NAME,
        stage_label=OPPORTUNITY_STAGE,
        amount_usd=OPPORTUNITY_AMOUNT_USD,
        close_date=sd.ahead(weeks=3),
        company_id=COMPANY_ID,
        owner=REP,
        point_of_contact_id=PERSON_ID,
    )


# Only the tables this demo touches, in FK-safe order.
_DEMO_INSERT_ORDER = [
    ("core", "user"),
    ("core", "userWorkspace"),
    (sd.WS, "workspaceMember"),
    (sd.WS, "company"),
    (sd.WS, "person"),
    (sd.WS, "opportunity"),
]


async def _seed(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=lambda v: v, decoder=json.loads, schema="pg_catalog"
    )
    ws_schema, workspace_id = await sd._discover_target(conn)
    print(f"  workspace schema : {ws_schema}")
    print(f"  workspace id     : {workspace_id}")

    _build_rows()

    total = 0
    async with conn.transaction():
        for schema, table in _DEMO_INSERT_ORDER:
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
        # asyncpg returns e.g. "UPDATE 2"
        count = result.split()[-1] if result else "0"
        print(f"  expired {count} prior pending action(s) for this deal.")
    except asyncpg.UndefinedTableError:
        print("  (no followup_pending_actions table yet — nothing to clear.)")


# ===========================================================================
# Firing — post the inbound email exactly like a real trigger
# ===========================================================================


async def _fire(workspace_id: uuid.UUID) -> None:
    payload = {
        "email_id": f"demo-mohammed-{uuid.uuid4().hex[:8]}",
        "workspace_id": str(workspace_id),
        "sender_email": MOHAMMED_EMAIL,
        "subject": EMAIL_SUBJECT,
        "body": _email_body(),
        # Safety net: pass the deal explicitly so the run never halts even if
        # sender→deal resolution is fuzzy. The agent still re-extracts facts.
        "opportunity_id": str(OPPORTUNITY_ID),
        "owner_user_id": str(REP.user_id),
        # The slots Mohammed asked for, parsed to ISO — so check_calendar verifies
        # THESE windows against the rep's calendar instead of free-picking a time.
        "proposed_times": _proposed_times(),
        "duration_minutes": MEETING_DURATION_MINUTES,
        "urgency": "high",
    }
    url = f"{_ai_service_url()}/followup/events"
    print(f"\n  POST {url}")
    print(f"  from: {MOHAMMED_EMAIL}  subject: {EMAIL_SUBJECT!r}")
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
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
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
    print("  SDA × Dr. Mohammed — Follow-Up demo")
    print("=" * 72)

    workspace_id = None
    conn = await asyncpg.connect(dsn)
    try:
        ws_schema, workspace_id = await sd._discover_target(conn)
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
