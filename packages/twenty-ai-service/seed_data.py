"""Seed data for the Follow-Up Intelligence Agent, mapped onto Twenty CRM's real schema.

This module generates a complete, interconnected B2B-SaaS sales dataset covering five
deal scenarios and inserts it into the *existing* Twenty CRM database. It does NOT invent
a parallel CRM schema: companies/contacts/opportunities/emails/calls/meetings/tasks are
written to Twenty's actual workspace tables (person, company, opportunity, message + the
messaging join tables, calendarEvent, note, task, ...).

The Follow-Up Agent's own derived data (profile facts, relationships, and shadow entities)
has nowhere to live in Twenty's standard schema, so it is stored in a
dedicated `followup_agent` Postgres schema inside the SAME database (no new database is
created). Those tables are created on demand by `seed_database()`.

Key design points:
  * Deterministic UUIDs via uuid5(NAMESPACE_URL, "...") so re-seeding is fully idempotent.
  * Inserts use parameterized asyncpg queries with ON CONFLICT (id) DO NOTHING.
  * Dates are relative to datetime.now(timezone.utc) using timedelta, so data stays fresh.
  * The target workspace + its schema name are discovered dynamically at runtime.

Run:  python seed_data.py
Env:  PG_DATABASE_URL (falls back to packages/twenty-server/.env style default)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

# ---------------------------------------------------------------------------
# Determinism helpers
# ---------------------------------------------------------------------------

NS = uuid.NAMESPACE_URL


def uid(identifier: str) -> uuid.UUID:
    """Stable UUID derived from a content string (idempotent across reruns)."""
    return uuid.uuid5(NS, identifier)


NOW = datetime.now(timezone.utc)


def ago(**kwargs: float) -> datetime:
    return NOW - timedelta(**kwargs)


def ahead(**kwargs: float) -> datetime:
    return NOW + timedelta(**kwargs)


# ---------------------------------------------------------------------------
# Row registry. Schema "WS" is a placeholder for the dynamically-discovered
# workspace schema; it is resolved at insert time.
# ---------------------------------------------------------------------------

WS = "WS"  # placeholder token for the workspace schema
AGENT_SCHEMA = "followup_agent"

ROWS: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
_seen_ids: set[tuple[str, str, Any]] = set()

# Workspace id is discovered at runtime; the Follow-Up Agent shadow rows need it.
# We use a sentinel and patch it in once discovered.
WORKSPACE_ID_SENTINEL = "__workspace_id__"

_position = 0.0


def _next_position() -> float:
    global _position
    _position += 1.0
    return _position


def add(schema: str, table: str, row: dict[str, Any]) -> None:
    key = (schema, table, row["id"])
    if key in _seen_ids:
        return
    _seen_ids.add(key)
    ROWS[(schema, table)].append(row)


# Insertion order respects foreign-key dependencies.
INSERT_ORDER: list[tuple[str, str]] = [
    ("core", "user"),
    ("core", "userWorkspace"),
    (WS, "workspaceMember"),
    (WS, "company"),
    (WS, "person"),
    (WS, "opportunity"),
    (WS, "connectedAccount"),
    (WS, "messageChannel"),
    (WS, "calendarChannel"),
    (WS, "messageThread"),
    (WS, "message"),
    (WS, "messageParticipant"),
    (WS, "messageChannelMessageAssociation"),
    (WS, "note"),
    (WS, "noteTarget"),
    (WS, "task"),
    (WS, "taskTarget"),
    (WS, "calendarEvent"),
    (WS, "calendarEventParticipant"),
    (AGENT_SCHEMA, "shadow_entities"),
    (AGENT_SCHEMA, "profile_facts"),
    (AGENT_SCHEMA, "profile_relationships"),
]


def ws_meta(created_at: datetime, source: str = "IMPORT", name: str = "Seed Script") -> dict[str, Any]:
    """Common audit columns required (NOT NULL) on every Twenty workspace row."""
    return {
        "createdAt": created_at,
        "updatedAt": created_at,
        "createdBySource": source,
        "createdByName": name,
        "updatedBySource": source,
        "updatedByName": name,
        "position": _next_position(),
    }


# ---------------------------------------------------------------------------
# Sales reps (Twenty: core.user + core.userWorkspace + workspaceMember)
# ---------------------------------------------------------------------------


@dataclass
class Rep:
    key: str
    first: str
    last: str
    email: str

    @property
    def user_id(self) -> uuid.UUID:
        return uid(f"user:{self.email}")

    @property
    def member_id(self) -> uuid.UUID:
        return uid(f"member:{self.email}")


SARAH = Rep("sarah", "Sarah", "Chen", "sarah.chen@ourcompany.com")
MARCUS = Rep("marcus", "Marcus", "Webb", "marcus.webb@ourcompany.com")
REPS = [SARAH, MARCUS]


def build_reps() -> None:
    for rep in REPS:
        add("core", "user", {
            "id": rep.user_id,
            "firstName": rep.first,
            "lastName": rep.last,
            "email": rep.email,
            "isEmailVerified": True,
            "disabled": False,
            "passwordHash": None,
            "canImpersonate": False,
            "canAccessFullAdminPanel": False,
            "createdAt": NOW,
            "updatedAt": NOW,
            "locale": "en",
        })
        add("core", "userWorkspace", {
            "id": uid(f"userWorkspace:{rep.email}"),
            "userId": rep.user_id,
            "workspaceId": WORKSPACE_ID_SENTINEL,
            "defaultAvatarUrl": None,
            "locale": "en",
            "createdAt": NOW,
            "updatedAt": NOW,
        })
        row = {
            "id": rep.member_id,
            "nameFirstName": rep.first,
            "nameLastName": rep.last,
            "colorScheme": "System",
            "locale": "en",
            "avatarUrl": None,
            "userEmail": rep.email,
            "calendarStartDay": 7,
            "userId": rep.user_id,
            "timeZone": "system",
            "dateFormat": "SYSTEM",
            "timeFormat": "SYSTEM",
            "numberFormat": "SYSTEM",
        }
        row.update(ws_meta(NOW))
        add(WS, "workspaceMember", row)


# ---------------------------------------------------------------------------
# CRM entities
# ---------------------------------------------------------------------------

# Registry of email handle -> (display name, person_id | None, workspace_member_id | None)
HANDLE_INFO: dict[str, tuple[str, Optional[uuid.UUID], Optional[uuid.UUID]]] = {}


def register_rep_handles() -> None:
    for rep in REPS:
        HANDLE_INFO[rep.email] = (f"{rep.first} {rep.last}", None, rep.member_id)


def add_company(key: str, name: str, domain: str, employees: int, owner: Rep) -> uuid.UUID:
    cid = uid(f"company:{key}")
    row = {
        "id": cid,
        "name": name,
        "domainNamePrimaryLinkLabel": domain,
        "domainNamePrimaryLinkUrl": f"https://{domain}",
        "domainNameSecondaryLinks": [],
        "employees": float(employees),
        "idealCustomerProfile": False,
        "accountOwnerId": owner.member_id,
        "annualRecurringRevenueAmountMicros": None,
        "annualRecurringRevenueCurrencyCode": "USD",
    }
    row.update(ws_meta(ago(weeks=8)))
    add(WS, "company", row)
    return cid


def add_person(key: str, first: str, last: str, email: str, title: str,
               company_id: uuid.UUID, city: str = "San Francisco") -> uuid.UUID:
    pid = uid(f"person:{key}")
    row = {
        "id": pid,
        "nameFirstName": first,
        "nameLastName": last,
        "emailsPrimaryEmail": email,
        "emailsAdditionalEmails": [],
        "jobTitle": title,
        "city": city,
        "companyId": company_id,
        "phonesAdditionalPhones": [],
    }
    row.update(ws_meta(ago(weeks=7)))
    add(WS, "person", row)
    HANDLE_INFO[email] = (f"{first} {last}", pid, None)
    return pid


# Maps the scenario's descriptive stage labels to Twenty's opportunity_stage enum.
STAGE_MAP = {
    "Negotiation": "PROPOSAL",
    "Discovery": "SCREENING",
    "Evaluation": "MEETING",
    "Proposal": "PROPOSAL",
    "Qualification": "NEW",
}


def add_opportunity(key: str, name: str, stage_label: str, amount_usd: int,
                    close_date: datetime, company_id: uuid.UUID,
                    owner: Rep, point_of_contact_id: uuid.UUID) -> uuid.UUID:
    oid = uid(f"opportunity:{key}")
    row = {
        "id": oid,
        "name": name,
        "amountAmountMicros": amount_usd * 1_000_000,
        "amountCurrencyCode": "USD",
        "closeDate": close_date,
        "stage": STAGE_MAP[stage_label],
        "companyId": company_id,
        "pointOfContactId": point_of_contact_id,
        "ownerId": owner.member_id,
    }
    row.update(ws_meta(ago(weeks=6)))
    add(WS, "opportunity", row)
    return oid


# ---------------------------------------------------------------------------
# Communication channels (one connected account + channels per rep)
# ---------------------------------------------------------------------------


def build_channels() -> None:
    for rep in REPS:
        account_id = uid(f"account:{rep.email}")
        acc = {
            "id": account_id,
            "handle": rep.email,
            "provider": "google",
            "accessToken": None,
            "refreshToken": None,
            "handleAliases": None,
            "connectionParameters": None,
            "accountOwnerId": rep.member_id,
        }
        acc.update(ws_meta(ago(weeks=10), source="MANUAL"))
        add(WS, "connectedAccount", acc)

        chan = {
            "id": uid(f"messageChannel:{rep.email}"),
            "visibility": "SHARE_EVERYTHING",
            "handle": rep.email,
            "type": "EMAIL",
            "isContactAutoCreationEnabled": True,
            "contactAutoCreationPolicy": "SENT_AND_RECEIVED",
            "messageFolderImportPolicy": "ALL_FOLDERS",
            "excludeNonProfessionalEmails": False,
            "excludeGroupEmails": False,
            "pendingGroupEmailsAction": "NONE",
            "isSyncEnabled": True,
            "syncCursor": None,
            "syncStatus": "ACTIVE",
            "syncStage": "MESSAGE_LIST_FETCH_PENDING",
            "throttleFailureCount": 0.0,
            "connectedAccountId": account_id,
        }
        chan.update(ws_meta(ago(weeks=10), source="MANUAL"))
        add(WS, "messageChannel", chan)

    # Calendar channel only for Sarah (her week of meetings is seeded).
    cal = {
        "id": uid(f"calendarChannel:{SARAH.email}"),
        "handle": SARAH.email,
        "visibility": "SHARE_EVERYTHING",
        "isContactAutoCreationEnabled": True,
        "contactAutoCreationPolicy": "AS_PARTICIPANT_AND_ORGANIZER",
        "isSyncEnabled": True,
        "syncCursor": None,
        "syncStatus": "ACTIVE",
        "syncStage": "CALENDAR_EVENT_LIST_FETCH_PENDING",
        "throttleFailureCount": 0.0,
        "connectedAccountId": uid(f"account:{SARAH.email}"),
    }
    cal.update(ws_meta(ago(weeks=10), source="MANUAL"))
    add(WS, "calendarChannel", cal)


# ---------------------------------------------------------------------------
# Activity builders: emails -> Twenty messaging tables
# ---------------------------------------------------------------------------


def _participant(message_id: uuid.UUID, role: str, handle: str,
                 fallback_display: Optional[str], when: datetime, salt: str) -> None:
    display, person_id, member_id = HANDLE_INFO.get(handle, (fallback_display or handle, None, None))
    row = {
        "id": uid(f"participant:{message_id}:{role}:{handle}:{salt}"),
        "role": role,
        "handle": handle,
        "displayName": display,
        "messageId": message_id,
        "personId": person_id,
        "workspaceMemberId": member_id,
    }
    row.update(ws_meta(when, source="EMAIL"))
    add(WS, "messageParticipant", row)


def add_email(*, key: str, thread_id: uuid.UUID, channel_owner: Rep, subject: str,
              sender: str, to: list[str], cc: Optional[list[str]], body: str,
              when: datetime, sender_display: Optional[str] = None,
              recipient_display: Optional[dict[str, str]] = None) -> uuid.UUID:
    """Create a full Twenty email: message + FROM/TO/CC participants + channel association."""
    recipient_display = recipient_display or {}
    cc = cc or []
    message_id = uid(f"message:{key}")
    header = f"<{key}@mail.ourcompany.com>"

    msg = {
        "id": message_id,
        "headerMessageId": header,
        "subject": subject,
        "text": body,
        "receivedAt": when,
        "messageThreadId": thread_id,
    }
    msg.update(ws_meta(when, source="EMAIL"))
    add(WS, "message", msg)

    _participant(message_id, "FROM", sender, sender_display, when, "from")
    for handle in to:
        _participant(message_id, "TO", handle, recipient_display.get(handle), when, "to")
    for handle in cc:
        _participant(message_id, "CC", handle, recipient_display.get(handle), when, "cc")

    direction = "OUTGOING" if sender == channel_owner.email else "INCOMING"
    assoc = {
        "id": uid(f"assoc:{key}"),
        "messageExternalId": header,
        "messageThreadExternalId": str(thread_id),
        "direction": direction,
        "messageChannelId": uid(f"messageChannel:{channel_owner.email}"),
        "messageId": message_id,
        "messageThreadId": thread_id,
    }
    assoc.update(ws_meta(when, source="EMAIL"))
    add(WS, "messageChannelMessageAssociation", assoc)
    return message_id


def add_thread(key: str, subject: str, when: datetime) -> uuid.UUID:
    tid = uid(f"thread:{key}")
    row = {"id": tid, "subject": subject}
    row.update(ws_meta(when, source="EMAIL"))
    add(WS, "messageThread", row)
    return tid


# ---------------------------------------------------------------------------
# Activity builders: calls / freeform notes -> Twenty note + noteTarget
# ---------------------------------------------------------------------------


def add_note(*, key: str, title: str, body: str, when: datetime,
             opportunity_id: uuid.UUID, person_id: Optional[uuid.UUID] = None,
             company_id: Optional[uuid.UUID] = None) -> uuid.UUID:
    note_id = uid(f"note:{key}")
    row = {
        "id": note_id,
        "title": title,
        "bodyV2Blocknote": None,
        "bodyV2Markdown": body,
    }
    row.update(ws_meta(when, source="MANUAL"))
    add(WS, "note", row)

    def target(suffix: str, **target_cols: Any) -> None:
        t = {
            "id": uid(f"noteTarget:{key}:{suffix}"),
            "noteId": note_id,
            "targetCompanyId": None,
            "targetPersonId": None,
            "targetOpportunityId": None,
        }
        t.update(target_cols)
        t.update(ws_meta(when, source="MANUAL"))
        add(WS, "noteTarget", t)

    target("opp", targetOpportunityId=opportunity_id)
    if person_id:
        target("person", targetPersonId=person_id)
    if company_id:
        target("company", targetCompanyId=company_id)
    return note_id


# ---------------------------------------------------------------------------
# Activity builders: tasks -> Twenty task + taskTarget
# ---------------------------------------------------------------------------


def add_task(*, key: str, title: str, body: str, due_at: datetime, status: str,
             when: datetime, assignee: Rep, opportunity_id: uuid.UUID,
             person_id: Optional[uuid.UUID] = None) -> uuid.UUID:
    task_id = uid(f"task:{key}")
    row = {
        "id": task_id,
        "title": title,
        "bodyV2Blocknote": None,
        "bodyV2Markdown": body,
        "dueAt": due_at,
        "status": status,
        "assigneeId": assignee.member_id,
    }
    row.update(ws_meta(when, source="MANUAL"))
    add(WS, "task", row)

    tt = {
        "id": uid(f"taskTarget:{key}:opp"),
        "taskId": task_id,
        "targetCompanyId": None,
        "targetPersonId": person_id,
        "targetOpportunityId": opportunity_id,
    }
    tt.update(ws_meta(when, source="MANUAL"))
    add(WS, "taskTarget", tt)
    return task_id


# ---------------------------------------------------------------------------
# Calendar builders -> Twenty calendarEvent + calendarEventParticipant
# ---------------------------------------------------------------------------


def add_calendar_event(*, key: str, title: str, starts_at: datetime, ends_at: datetime,
                       attendees: list[tuple[str, Optional[uuid.UUID], Optional[uuid.UUID]]],
                       location: str = "", description: str = "") -> uuid.UUID:
    event_id = uid(f"calEvent:{key}")
    row = {
        "id": event_id,
        "title": title,
        "isCanceled": False,
        "isFullDay": False,
        "startsAt": starts_at,
        "endsAt": ends_at,
        "externalCreatedAt": starts_at,
        "externalUpdatedAt": starts_at,
        "description": description,
        "location": location,
        "iCalUid": f"{event_id}@ourcompany.com",
        "conferenceSolution": "Google Meet" if not location else None,
        "conferenceLinkPrimaryLinkLabel": None,
        "conferenceLinkPrimaryLinkUrl": None,
        "conferenceLinkSecondaryLinks": [],
    }
    row.update(ws_meta(starts_at, source="CALENDAR"))
    add(WS, "calendarEvent", row)

    for idx, (handle, person_id, member_id) in enumerate(attendees):
        display, reg_person, reg_member = HANDLE_INFO.get(handle, (handle, person_id, member_id))
        part = {
            "id": uid(f"calPart:{key}:{handle}"),
            "handle": handle,
            "displayName": display,
            "isOrganizer": idx == 0,
            "responseStatus": "ACCEPTED" if idx == 0 else "NEEDS_ACTION",
            "calendarEventId": event_id,
            "personId": person_id or reg_person,
            "workspaceMemberId": member_id or reg_member,
        }
        part.update(ws_meta(starts_at, source="CALENDAR"))
        add(WS, "calendarEventParticipant", part)
    return event_id


# ---------------------------------------------------------------------------
# Follow-Up Agent derived data (separate followup_agent schema)
# ---------------------------------------------------------------------------


def add_fact(*, key: str, opportunity_id: uuid.UUID, entity_type: str,
             entity_crm_id: Optional[uuid.UUID], fact_type: str, fact_value: str,
             confidence: float, source_type: str, source_id: Optional[uuid.UUID],
             extracted_at: datetime) -> None:
    add(AGENT_SCHEMA, "profile_facts", {
        "id": uid(f"fact:{key}"),
        "opportunity_id": opportunity_id,
        "entity_type": entity_type,
        "entity_crm_id": entity_crm_id,
        "fact_type": fact_type,
        "fact_value": fact_value,
        "confidence": confidence,
        "source_type": source_type,
        "source_id": source_id,
        "extracted_at": extracted_at,
    })


def add_relationship(*, key: str, opportunity_id: uuid.UUID,
                     from_id: Optional[uuid.UUID], to_id: Optional[uuid.UUID],
                     relationship_type: str, description: str, confidence: float,
                     source_type: str, first_seen_at: datetime) -> None:
    add(AGENT_SCHEMA, "profile_relationships", {
        "id": uid(f"rel:{key}"),
        "opportunity_id": opportunity_id,
        "from_entity_crm_id": from_id,
        "to_entity_crm_id": to_id,
        "relationship_type": relationship_type,
        "description": description,
        "confidence": confidence,
        "source_type": source_type,
        "first_seen_at": first_seen_at,
    })


def add_shadow(*, key: str, opportunity_id: uuid.UUID, name: str,
               email_address: Optional[str], title_or_role: Optional[str],
               company_crm_id: Optional[uuid.UUID], aliases: list[str],
               mention_count: int, status: str,
               first_seen_at: datetime, last_seen_at: datetime) -> uuid.UUID:
    shadow_id = uid(f"shadow:{key}")
    add(AGENT_SCHEMA, "shadow_entities", {
        "id": shadow_id,
        "opportunity_id": opportunity_id,
        "workspace_id": WORKSPACE_ID_SENTINEL,
        "name": name,
        "email_address": email_address,
        "title_or_role": title_or_role,
        "company_crm_id": company_crm_id,
        "aliases": aliases,
        "mention_count": mention_count,
        "status": status,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    })
    return shadow_id


# ===========================================================================
# SCENARIO A — Airbnb — "The deal going cold" (owned by Sarah)
# ===========================================================================

SIG_SARAH = "\n\nBest,\nSarah Chen\nSenior Account Executive | OurCompany\nsarah.chen@ourcompany.com | (415) 555-0142"


def scenario_a() -> None:
    company = add_company("airbnb", "Airbnb", "airbnb.com", 6500, SARAH)
    john = add_person("john_park", "John", "Park", "john.park@airbnb.com",
                      "Senior Engineering Manager", company)
    lisa = add_person("lisa_huang", "Lisa", "Huang", "lisa.huang@airbnb.com",
                      "Procurement Lead", company)
    opp = add_opportunity("airbnb_platform", "Airbnb — Platform Integration",
                          "Negotiation", 85_000, ahead(weeks=6), company, SARAH, john)

    thread = add_thread("airbnb", "Airbnb — Platform Integration", ago(weeks=5))

    e1 = add_email(
        key="a1", thread_id=thread, channel_owner=SARAH,
        subject="Airbnb — Platform Integration",
        sender=SARAH.email, to=["john.park@airbnb.com"], cc=None,
        when=ago(weeks=5, hours=3),
        body=(
            "Hi John,\n\nThank you so much for having the whole team on the demo yesterday — "
            "it was a genuinely fun session. I could tell the real-time sync and the granular "
            "permission model really landed with your engineers, especially the part where we "
            "showed conflict resolution across regions.\n\n"
            "As promised, I'm pulling together a tailored rollout plan focused on the platform "
            "integration use cases you described. A couple of the features you were excited about "
            "(the webhook replay and the audit log export) are exactly where we see enterprise "
            "teams get the fastest time-to-value.\n\n"
            "Would it make sense to get procurement involved early so we don't lose time on the "
            "paperwork side? Happy to send over pricing whenever you're ready." + SIG_SARAH
        ),
    )
    add_email(
        key="a2", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration",
        sender="john.park@airbnb.com", to=[SARAH.email], cc=None,
        when=ago(weeks=5, hours=1),
        body=(
            "Hi Sarah,\n\nHonestly the team loved it. We've looked at three vendors now and yours "
            "is the first demo where nobody started multitasking halfway through :) The webhook "
            "replay is a big deal for us.\n\n"
            "I'll loop in procurement next week — her name is Lisa Huang, she runs our vendor "
            "evaluations. One more thing: our VP of Engineering, David, also wants to see the "
            "technical architecture before we go too far. He's pretty hands-on with anything that "
            "touches the core platform, so getting him comfortable early matters.\n\n"
            "Thanks,\nJohn\n\nJohn Park\nSenior Engineering Manager, Airbnb"
        ),
    )
    add_email(
        key="a3", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration — pricing + intro to procurement",
        sender=SARAH.email, to=["john.park@airbnb.com", "lisa.huang@airbnb.com"], cc=None,
        when=ago(weeks=4, days=2),
        body=(
            "Hi Lisa, lovely to meet you (cc'ing John who's been my main point of contact).\n\n"
            "John mentioned you handle vendor evaluations, so I wanted to introduce myself and get "
            "the commercial conversation started in parallel with the technical review. I've "
            "attached our pricing proposal for the Platform Integration package — it reflects the "
            "scope John and I discussed, at $85k ARR.\n\n"
            "Please don't hesitate to send over any security or procurement questionnaires you "
            "need completed. I'd rather front-load the paperwork than have it become the "
            "bottleneck later." + SIG_SARAH
        ),
    )
    add_email(
        key="a4", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration — pricing + intro to procurement",
        sender="lisa.huang@airbnb.com", to=[SARAH.email], cc=["john.park@airbnb.com"],
        when=ago(weeks=4, days=1),
        body=(
            "Hello Sarah,\n\nThank you for the introduction and for the proposal. I'll review it "
            "with my team and come back with questions. As a heads up, our standard vendor "
            "evaluation process typically takes 2–3 weeks once we have all the documentation, so "
            "I'd encourage us to keep things moving on the security review in parallel.\n\n"
            "Best regards,\nLisa Huang\nProcurement Lead, Airbnb"
        ),
    )

    add_note(
        key="a5", title="Call with John — David has timeline concerns",
        when=ago(weeks=3), opportunity_id=opp, person_id=john,
        body=("quick call w/ John. says David (VP Eng) has some concerns about the integration "
              "timeline — thinks 6 wks might be tight given their migration freeze. but John says "
              "he's 'generally supportive'. John will send the technical requirements doc by EOW. "
              "need to get David in front of our solutions architect asap, he's the real "
              "decision maker on the tech side."),
    )
    add_email(
        key="a6", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration — technical requirements",
        sender="john.park@airbnb.com", to=[SARAH.email], cc=None,
        when=ago(weeks=3, days=1),
        body=(
            "Hi Sarah,\n\nSending over a partial requirements list — sorry it's not complete, I "
            "wanted to get you something rather than nothing. David is traveling this week so we "
            "couldn't finalize the architecture section, but we'll lock it down next week when "
            "he's back.\n\nThe big open items are around the integration timeline and the data "
            "residency requirements. More soon.\n\nJohn"
        ),
    )
    add_email(
        key="a7", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration — checking in",
        sender=SARAH.email, to=["john.park@airbnb.com"], cc=None,
        when=ago(weeks=2, days=2),
        body=(
            "Hey John,\n\nHope you had a good week! Just checking in on two things:\n\n"
            "1. The complete requirements doc once David's back from travel\n"
            "2. Any feedback from David on the integration timeline — I'd love to set up 30 "
            "minutes with our solutions architect to walk him through how we de-risk the "
            "migration window.\n\nNo rush, just keeping us on track for the close date. Happy to "
            "hop on a quick call if that's easier than email." + SIG_SARAH
        ),
    )
    # (intentional silence — no reply for ~2 weeks)
    add_email(
        key="a9", thread_id=thread, channel_owner=SARAH,
        subject="Re: Airbnb — Platform Integration — checking in",
        sender=SARAH.email, to=["john.park@airbnb.com"], cc=None,
        when=ago(days=10),
        body=(
            "Hi John,\n\nFollowing up again — I haven't heard back over the last couple of weeks "
            "and wanted to make sure everything's okay on your end. Has anything changed with the "
            "project, priorities, or timeline? Totally understand if things have shifted; I'd just "
            "rather know so I can be helpful rather than keep chasing.\n\nIf it's easier, I'm "
            "happy to hop on a quick 15-minute call this week. Even a one-line reply would mean a "
            "lot." + SIG_SARAH
        ),
    )
    # (intentional silence again)
    add_note(
        key="a11", title="John has gone dark — need another way in",
        when=ago(days=5), opportunity_id=opp, person_id=john,
        body=("John has gone dark. No response in 2 weeks across two follow-ups. Lisa also quiet "
              "since the proposal. Concerned this is slipping. The unresolved timeline concern is "
              "David's and I've never spoken to him directly. Need to find another way in — maybe "
              "reach out to David? Don't have his contact info though, only know he's VP Eng. "
              "Could try LinkedIn or ask John for an intro one more time."),
    )

    # --- Agent-derived profile (extraction already run) ---
    add_fact(key="a-f1", opportunity_id=opp, entity_type="person", entity_crm_id=john,
             fact_type="sentiment", fact_value="Initially highly enthusiastic; engagement has dropped to zero over the last 14 days",
             confidence=0.9, source_type="note", source_id=uid("note:a11"), extracted_at=ago(days=5))
    add_fact(key="a-f2", opportunity_id=opp, entity_type="person", entity_crm_id=john,
             fact_type="commitment", fact_value="Promised complete technical requirements doc; only a partial list delivered",
             confidence=0.85, source_type="email", source_id=uid("message:a6"), extracted_at=ago(weeks=3, days=1))
    add_fact(key="a-f3", opportunity_id=opp, entity_type="person", entity_crm_id=lisa,
             fact_type="process", fact_value="Procurement evaluation takes 2-3 weeks; quiet since proposal sent",
             confidence=0.8, source_type="email", source_id=uid("message:a4"), extracted_at=ago(weeks=4, days=1))
    david = add_shadow(key="a-david", opportunity_id=opp, name="David", email_address=None,
                       title_or_role="VP of Engineering", company_crm_id=company,
                       aliases=["David (VP Eng)", "VP of Engineering"], mention_count=4,
                       status="detected", first_seen_at=ago(weeks=5, hours=1), last_seen_at=ago(days=5))
    add_fact(key="a-f4", opportunity_id=opp, entity_type="shadow", entity_crm_id=david,
             fact_type="concern", fact_value="Concerned the 6-week integration timeline is too tight given a migration freeze",
             confidence=0.75, source_type="note", source_id=uid("note:a5"), extracted_at=ago(weeks=3))
    add_fact(key="a-f5", opportunity_id=opp, entity_type="shadow", entity_crm_id=david,
             fact_type="role", fact_value="Real technical decision-maker; hands-on with anything touching the core platform",
             confidence=0.8, source_type="email", source_id=uid("message:a2"), extracted_at=ago(weeks=5, hours=1))
    add_relationship(key="a-r1", opportunity_id=opp, from_id=john, to_id=david,
                     relationship_type="reports_to", description="John defers to David (VP Eng) on technical approval",
                     confidence=0.7, source_type="note", first_seen_at=ago(weeks=3))


# ===========================================================================
# SCENARIO B — Stripe — "New stakeholder entering" (owned by Sarah)
# ===========================================================================


def scenario_b() -> None:
    company = add_company("stripe", "Stripe", "stripe.com", 200, SARAH)
    alex = add_person("alex_rivera", "Alex", "Rivera", "alex.rivera@stripe.com",
                      "Head of Product", company)
    priya = add_person("priya_sharma", "Priya", "Sharma", "priya.sharma@stripe.com",
                       "Finance Manager", company)
    opp = add_opportunity("stripe_analytics", "Stripe — Analytics Suite",
                          "Discovery", 45_000, ahead(weeks=8), company, SARAH, alex)

    thread = add_thread("stripe", "Stripe — Analytics Suite", ago(weeks=3))

    add_email(
        key="b1", thread_id=thread, channel_owner=SARAH,
        subject="Stripe — Analytics Suite — recap from our call",
        sender=SARAH.email, to=["alex.rivera@stripe.com"], cc=None,
        when=ago(weeks=3, hours=2),
        body=(
            "Hi Alex,\n\nGreat speaking with you earlier! To recap what we covered: you're "
            "looking to give your product team self-serve access to funnel and retention "
            "analytics without having to file a ticket with data eng every time. The analytics "
            "dashboard we walked through is exactly built for that.\n\n"
            "I'll put together a short summary of the dashboard capabilities and the embedding "
            "options. Let me know what questions come up as you socialize this internally." + SIG_SARAH
        ),
    )
    add_email(
        key="b2", thread_id=thread, channel_owner=SARAH,
        subject="Re: Stripe — Analytics Suite — recap from our call",
        sender="alex.rivera@stripe.com", to=[SARAH.email], cc=None,
        when=ago(weeks=3, hours=1),
        body=(
            "Thanks Sarah — yes, the dashboard is the piece I'm most excited about. Two questions "
            "before I take this further:\n\n"
            "1. What are the API rate limits on the data export endpoints?\n"
            "2. What's your data retention policy — how far back can we query?\n\n"
            "Context: I need to convince our CTO, Raj, that this won't impact our production "
            "systems. He's protective of anything that hits our data infra, so the more concrete "
            "I can be the better.\n\nAlex\n\nAlex Rivera\nHead of Product, Stripe"
        ),
    )
    add_email(
        key="b3", thread_id=thread, channel_owner=SARAH,
        subject="Re: Stripe — Analytics Suite — API limits & retention",
        sender=SARAH.email, to=["alex.rivera@stripe.com"], cc=None,
        when=ago(weeks=2, days=2),
        body=(
            "Hi Alex,\n\nGreat questions — here are the specifics so you can reassure Raj:\n\n"
            "• Rate limits: the export API allows 600 requests/min per token, with burst up to "
            "1,000. Our reads run off a replica, so there's zero load on your production write "
            "path. Docs: https://docs.ourcompany.com/api/rate-limits\n"
            "• Data retention: fully configurable; default is 24 months of queryable history, "
            "extendable to 7 years on enterprise. Docs: https://docs.ourcompany.com/retention\n\n"
            "Happy to do a short technical call with Raj directly if that's easier than relaying." + SIG_SARAH
        ),
    )
    add_email(
        key="b4", thread_id=thread, channel_owner=SARAH,
        subject="Re: Stripe — Analytics Suite — API limits & retention",
        sender="alex.rivera@stripe.com", to=[SARAH.email], cc=None,
        when=ago(weeks=2, days=1),
        body=(
            "This looks great, thank you. Raj is a lot less worried now — the read-replica detail "
            "was exactly what he needed to hear. He wants a security review before we commit, so "
            "I'm going to loop in our security lead to run that process. Should move fast.\n\nAlex"
        ),
    )
    add_email(
        key="b5", thread_id=thread, channel_owner=SARAH,
        subject="Vendor security review — SOC 2 + questionnaire",
        sender="nadia.osei@stripe.com", to=[SARAH.email], cc=None,
        when=ago(days=10),
        sender_display="Nadia Osei",
        body=(
            "Hi Sarah,\n\nAlex asked me to reach out. I'm the Security Engineering Lead here at "
            "Stripe. Before we can proceed, we'll need your most recent SOC 2 report and a "
            "completed vendor security questionnaire — I've attached our template.\n\n"
            "Can you turn this around by next week? Raj wants to move fast on this one, so the "
            "sooner we clear the security gate the better.\n\n"
            "Thanks,\nNadia Osei\nSecurity Engineering Lead, Stripe"
        ),
    )
    add_email(
        key="b6", thread_id=thread, channel_owner=SARAH,
        subject="Re: Vendor security review — SOC 2 + questionnaire",
        sender=SARAH.email, to=["nadia.osei@stripe.com"], cc=["alex.rivera@stripe.com"],
        when=ago(days=9),
        recipient_display={"nadia.osei@stripe.com": "Nadia Osei"},
        body=(
            "Hi Nadia, great to meet you (cc Alex).\n\nAbsolutely — I'll have our SOC 2 Type II "
            "report and the completed questionnaire back to you by Thursday. If anything in the "
            "template needs a living document (e.g. our subprocessor list), I'll link the "
            "always-current version.\n\nLooking forward to working through this with you." + SIG_SARAH
        ),
    )
    add_note(
        key="b7", title="Sync with Alex — Raj actively pushing, Priya approved budget",
        when=ago(days=8), opportunity_id=opp, person_id=alex,
        body=("quick sync w/ Alex. Raj (CTO) is now ACTIVELY pushing this forward — wants it done "
              "before Q3 planning. budget already approved by Priya on the finance side. Nadia's "
              "security review is the last real gate. Alex is very confident, says it's ours to "
              "lose. need to nail the security questionnaire turnaround."),
    )
    add_email(
        key="b8", thread_id=thread, channel_owner=SARAH,
        subject="Re: Vendor security review — SOC 2 + questionnaire (completed)",
        sender=SARAH.email, to=["nadia.osei@stripe.com"],
        cc=["alex.rivera@stripe.com", "priya.sharma@stripe.com"],
        when=ago(days=5),
        recipient_display={"nadia.osei@stripe.com": "Nadia Osei"},
        body=(
            "Hi Nadia,\n\nAs promised — attached are our SOC 2 Type II report and the fully "
            "completed security questionnaire. I've also cc'd Priya so finance has visibility "
            "for the procurement file.\n\nHappy to walk through any answers live. Just say the "
            "word." + SIG_SARAH
        ),
    )
    add_email(
        key="b9", thread_id=thread, channel_owner=SARAH,
        subject="Re: Vendor security review — follow-up questions",
        sender="nadia.osei@stripe.com", to=[SARAH.email], cc=["alex.rivera@stripe.com"],
        when=ago(days=2),
        sender_display="Nadia Osei",
        body=(
            "Thanks Sarah. Reviewed the SOC 2 — looks solid, no concerns there. I do have a few "
            "follow-up questions on your data encryption at rest (key rotation cadence and "
            "whether keys are customer-managed). Can we schedule a 30-minute call this week to go "
            "through them? I'd like to include Raj as well so he can hear the answers directly.\n\n"
            "Nadia"
        ),
    )

    # --- Agent-derived profile ---
    raj = add_shadow(key="b-raj", opportunity_id=opp, name="Raj", email_address=None,
                     title_or_role="CTO", company_crm_id=company,
                     aliases=["Raj (CTO)", "our CTO"], mention_count=5,
                     status="detected", first_seen_at=ago(weeks=3, hours=1), last_seen_at=ago(days=2))
    nadia = add_shadow(key="b-nadia", opportunity_id=opp, name="Nadia Osei",
                       email_address="nadia.osei@stripe.com", title_or_role="Security Engineering Lead",
                       company_crm_id=company, aliases=["Nadia", "security lead"], mention_count=4,
                       status="pending_promotion", first_seen_at=ago(days=10), last_seen_at=ago(days=2))
    add_fact(key="b-f1", opportunity_id=opp, entity_type="shadow", entity_crm_id=raj,
             fact_type="buying_signal", fact_value="CTO actively pushing the deal forward; wants it closed before Q3 planning",
             confidence=0.85, source_type="note", source_id=uid("note:b7"), extracted_at=ago(days=8))
    add_fact(key="b-f2", opportunity_id=opp, entity_type="shadow", entity_crm_id=raj,
             fact_type="concern", fact_value="Initially worried about production-system impact; resolved by read-replica architecture",
             confidence=0.8, source_type="email", source_id=uid("message:b4"), extracted_at=ago(weeks=2, days=1))
    add_fact(key="b-f3", opportunity_id=opp, entity_type="shadow", entity_crm_id=nadia,
             fact_type="gate", fact_value="Owns the security review — the last gate before commit; open item is encryption-at-rest",
             confidence=0.9, source_type="email", source_id=uid("message:b9"), extracted_at=ago(days=2))
    add_fact(key="b-f4", opportunity_id=opp, entity_type="person", entity_crm_id=priya,
             fact_type="buying_signal", fact_value="Budget approved on the finance side",
             confidence=0.85, source_type="note", source_id=uid("note:b7"), extracted_at=ago(days=8))
    add_fact(key="b-f5", opportunity_id=opp, entity_type="person", entity_crm_id=alex,
             fact_type="sentiment", fact_value="Champion; very confident, calls it 'ours to lose'",
             confidence=0.85, source_type="note", source_id=uid("note:b7"), extracted_at=ago(days=8))
    add_relationship(key="b-r1", opportunity_id=opp, from_id=alex, to_id=raj,
                     relationship_type="reports_to", description="Alex must convince Raj (CTO) to proceed",
                     confidence=0.8, source_type="email", first_seen_at=ago(weeks=3, hours=1))
    add_relationship(key="b-r2", opportunity_id=opp, from_id=nadia, to_id=raj,
                     relationship_type="collaborates_with", description="Nadia runs security review, reports findings to Raj",
                     confidence=0.7, source_type="email", first_seen_at=ago(days=2))


# ===========================================================================
# SCENARIO C — Notion — "Competitor pressure" (owned by Marcus)
# ===========================================================================

SIG_MARCUS = "\n\nThanks,\nMarcus Webb\nAccount Executive | OurCompany\nmarcus.webb@ourcompany.com"


def scenario_c() -> None:
    company = add_company("notion", "Notion", "notion.com", 150, MARCUS)
    kevin = add_person("kevin_cho", "Kevin", "Cho", "kevin.cho@notion.com",
                       "Director of Operations", company)
    maria = add_person("maria_santos", "Maria", "Santos", "maria.santos@notion.com",
                       "Head of Finance", company)
    opp = add_opportunity("notion_workflow", "Notion — Workflow Automation",
                          "Evaluation", 32_000, ahead(weeks=5), company, MARCUS, kevin)

    thread = add_thread("notion", "Notion — Workflow Automation", ago(weeks=4))

    add_email(
        key="c1", thread_id=thread, channel_owner=MARCUS,
        subject="Notion — Workflow Automation — demo recap",
        sender=MARCUS.email, to=["kevin.cho@notion.com"], cc=None,
        when=ago(weeks=4, hours=2),
        body=(
            "Hi Kevin,\n\nThanks for the time today! Quick recap of the workflow automation piece "
            "we demoed: the visual workflow builder, the conditional branching, and the native "
            "approvals that triggered off your ticketing system. Those were the three areas you "
            "flagged as biggest time-sinks for your ops team.\n\nLet me know what you'd like to "
            "dig into next." + SIG_MARCUS
        ),
    )
    add_email(
        key="c2", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — demo recap",
        sender="kevin.cho@notion.com", to=[MARCUS.email], cc=None,
        when=ago(weeks=3, days=4),
        body=(
            "Hi Marcus,\n\nThe demo was solid, appreciated how concrete it was. I'll be upfront: "
            "we're also looking at Asana's enterprise tools, so I want to do a proper "
            "side-by-side comparison before we decide. Could you send over a detailed feature "
            "comparison? Especially around the workflow builder and pricing tiers.\n\nKevin\n\n"
            "Kevin Cho\nDirector of Operations, Notion"
        ),
    )
    add_email(
        key="c3", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — feature comparison attached",
        sender=MARCUS.email, to=["kevin.cho@notion.com"], cc=None,
        when=ago(weeks=3),
        body=(
            "Hi Kevin,\n\nAttached is a detailed feature comparison covering the workflow builder, "
            "branching logic, integrations, and admin controls. The short version: we're stronger "
            "on complex conditional workflows and native approvals.\n\nWould a second demo focused "
            "specifically on the areas where we differ from Asana be helpful? I can tailor it to "
            "your ops team's actual ticket flows." + SIG_MARCUS
        ),
    )
    add_email(
        key="c4", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — pricing question",
        sender="kevin.cho@notion.com", to=[MARCUS.email], cc=None,
        when=ago(weeks=2, days=3),
        body=(
            "Thanks Marcus. Maria and I reviewed the comparison. Your workflow builder is clearly "
            "stronger — no debate there. But Asana's pricing is more competitive for our team "
            "size, and that's weighing on Maria.\n\nCan we talk about pricing flexibility? We're "
            "a startup and honestly $32k is stretching our budget for this line item. If there's "
            "room to move, that would help me make the internal case.\n\nKevin"
        ),
    )
    add_note(
        key="c5", title="Call with Kevin — Maria pushing hard on price",
        when=ago(weeks=2), opportunity_id=opp, person_id=kevin,
        body=("called Kevin. he's genuinely sold on the product — workflow builder is the "
              "differentiator. but Maria (Head of Finance) is pushing HARD on price, wants us "
              "below $25k. Kevin basically hinted that if we can match Asana's price the deal is "
              "ours because the product is better. told him I'd check with my manager on what we "
              "can do. Asana is the live threat here, not the product fit."),
    )
    add_email(
        key="c6", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — pricing options",
        sender=MARCUS.email, to=["kevin.cho@notion.com"], cc=None,
        when=ago(weeks=2),
        body=(
            "Hi Kevin,\n\nI spoke with my team and we want to make this work. Two options:\n\n"
            "• $28k/year with a 2-year commitment, or\n• $30k for a 1-year annual\n\n"
            "Both reflect the fact that we genuinely think you'll get more out of the platform "
            "than the alternative. Would either of those work for you and Maria?" + SIG_MARCUS
        ),
    )
    add_email(
        key="c7", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — pricing options",
        sender="kevin.cho@notion.com", to=[MARCUS.email], cc=None,
        when=ago(days=10),
        body=(
            "Let me run this by Maria. I think the $28k with the 2-year could work, but she's "
            "hesitant about locking in for two years given how fast we're growing. Give us a few "
            "days to chew on it.\n\nKevin"
        ),
    )
    add_email(
        key="c8", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — any update?",
        sender=MARCUS.email, to=["kevin.cho@notion.com"], cc=None,
        when=ago(days=6),
        body=(
            "Hi Kevin, just circling back — any update from Maria on the pricing? Happy to jump "
            "on a quick call if it helps move things along." + SIG_MARCUS
        ),
    )
    add_email(
        key="c9", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Notion — Workflow Automation — any update?",
        sender="kevin.cho@notion.com", to=[MARCUS.email], cc=None,
        when=ago(days=3),
        body=(
            "Hey Marcus, sorry for the delay. Maria's traveling this week — she'll be back Monday "
            "and I know this is on her list. One thing I want to be transparent about: Asana came "
            "back to us with a first-year discount offer. I'm still pushing for you guys internally "
            "because the product is better, but the price gap is the sticking point. Anything you "
            "can do before Monday would help.\n\nKevin"
        ),
    )

    # --- Agent-derived profile ---
    add_fact(key="c-f1", opportunity_id=opp, entity_type="opportunity", entity_crm_id=opp,
             fact_type="competitor", fact_value="Asana (enterprise tools) is actively competing; offered Notion a first-year discount",
             confidence=0.9, source_type="email", source_id=uid("message:c9"), extracted_at=ago(days=3))
    add_fact(key="c-f2", opportunity_id=opp, entity_type="opportunity", entity_crm_id=opp,
             fact_type="objection", fact_value="Price objection: budget holder wants below $25k; current ask is $32k",
             confidence=0.85, source_type="note", source_id=uid("note:c5"), extracted_at=ago(weeks=2))
    add_fact(key="c-f3", opportunity_id=opp, entity_type="opportunity", entity_crm_id=opp,
             fact_type="objection", fact_value="Hesitant about a 2-year commitment given rapid company growth",
             confidence=0.8, source_type="email", source_id=uid("message:c7"), extracted_at=ago(days=10))
    add_fact(key="c-f4", opportunity_id=opp, entity_type="person", entity_crm_id=kevin,
             fact_type="sentiment", fact_value="Champion who prefers our product ('the deal is ours if we match Asana's price')",
             confidence=0.85, source_type="note", source_id=uid("note:c5"), extracted_at=ago(weeks=2))
    add_fact(key="c-f5", opportunity_id=opp, entity_type="person", entity_crm_id=maria,
             fact_type="role", fact_value="Budget authority; price-driven and not yet convinced; back from travel Monday",
             confidence=0.85, source_type="email", source_id=uid("message:c9"), extracted_at=ago(days=3))
    add_relationship(key="c-r1", opportunity_id=opp, from_id=kevin, to_id=maria,
                     relationship_type="needs_approval_from", description="Kevin needs Maria's budget sign-off to proceed",
                     confidence=0.85, source_type="note", first_seen_at=ago(weeks=2))


# ===========================================================================
# SCENARIO D — Figma — "Happy path with a meeting request" (owned by Sarah)
# No agent profile data is seeded (tests building a profile from scratch).
# ===========================================================================


def scenario_d() -> None:
    company = add_company("figma", "Figma", "figma.com", 300, SARAH)
    emma = add_person("emma_larsen", "Emma", "Larsen", "emma.larsen@figma.com",
                      "VP of Design", company)
    tyler = add_person("tyler_briggs", "Tyler", "Briggs", "tyler.briggs@figma.com",
                       "Design Systems Lead", company)
    opp = add_opportunity("figma_collab", "Figma — Design Collaboration Platform",
                          "Proposal", 62_000, ahead(weeks=3), company, SARAH, emma)

    thread = add_thread("figma", "Figma — Design Collaboration Platform", ago(weeks=3))

    add_email(
        key="d1", thread_id=thread, channel_owner=SARAH,
        subject="Figma — Design Collaboration Platform — proposal",
        sender=SARAH.email, to=["emma.larsen@figma.com"], cc=None,
        when=ago(weeks=3, hours=2),
        body=(
            "Hi Emma,\n\nCongratulations on getting through the evaluation — and thank you to "
            "Tyler and his team for being such thorough testers. As discussed, Tyler has signed "
            "off on the technical side, so I'm attaching the formal proposal for the Design "
            "Collaboration Platform at $62k ARR.\n\nIt includes the phased rollout we talked "
            "about and dedicated onboarding for Tyler's team. Let me know if you'd like any "
            "adjustments before it goes to legal." + SIG_SARAH
        ),
    )
    add_email(
        key="d2", thread_id=thread, channel_owner=SARAH,
        subject="Re: Figma — Design Collaboration Platform — proposal",
        sender="emma.larsen@figma.com", to=[SARAH.email], cc=None,
        when=ago(weeks=3, hours=1),
        body=(
            "Hi Sarah,\n\nThe proposal looks great. I'm sharing it with our legal team for "
            "contract review now — should be straightforward, nothing unusual jumped out. Tyler's "
            "team is genuinely eager to get started, which is a nice problem to have.\n\nMore "
            "soon,\nEmma\n\nEmma Larsen\nVP of Design, Figma"
        ),
    )
    add_note(
        key="d3", title="Call with Emma — legal proceeding, wants fast onboarding",
        when=ago(weeks=2), opportunity_id=opp, person_id=emma,
        body=("spoke w/ Emma. legal review proceeding, no red flags. she asked about onboarding "
              "timeline — wants to launch with Tyler's team in the first 2 weeks. told her we can "
              "do a phased rollout starting week 1. very healthy deal, everyone aligned."),
    )
    add_email(
        key="d4", thread_id=thread, channel_owner=SARAH,
        subject="Re: Figma — Design Collaboration Platform — implementation walkthrough?",
        sender="emma.larsen@figma.com", to=[SARAH.email], cc=None,
        when=ago(weeks=1),
        body=(
            "Hi Sarah,\n\nLegal is almost done — should have sign-off this week. One small ask "
            "before we sign: can we schedule a call next week with you, me, and Tyler to walk "
            "through the implementation plan? Tyler wants to make sure his team's workflow isn't "
            "disrupted during the migration.\n\nHow's Tuesday or Wednesday afternoon for you?\n\n"
            "Emma"
        ),
    )
    add_email(
        key="d5", thread_id=thread, channel_owner=SARAH,
        subject="Re: Figma — Design Collaboration Platform — implementation walkthrough?",
        sender=SARAH.email, to=["emma.larsen@figma.com"], cc=None,
        when=ago(days=5),
        body=(
            "Absolutely, Emma! Let me check my calendar and send over a couple of times that work "
            "for Tuesday/Wednesday afternoon. Looking forward to getting you all set up — and to "
            "making sure Tyler's migration is as boring and uneventful as possible :)" + SIG_SARAH
        ),
    )
    add_note(
        key="d6", title="TODO: send Emma meeting times (got sidetracked)",
        when=ago(days=4), opportunity_id=opp, person_id=emma,
        body=("need to actually send Emma those meeting times for the implementation walkthrough. "
              "got totally sidetracked by the Airbnb situation. do this first thing — don't let a "
              "clean deal stall on me."),
    )
    # The overdue scheduling task the agent should surface.
    add_task(
        key="d-sched", title="Send Emma + Tyler implementation-call time options",
        body=("Emma requested a Tue/Wed afternoon call with her + Tyler to walk through the "
              "implementation plan before signing. Check calendar and propose 2-3 slots."),
        due_at=ago(days=2), status="TODO", when=ago(days=5),
        assignee=SARAH, opportunity_id=opp, person_id=emma,
    )


# ===========================================================================
# SCENARIO E — Datadog — "Early stage, multiple signals" (owned by Marcus)
# ===========================================================================


def scenario_e() -> None:
    company = add_company("datadog", "Datadog", "datadoghq.com", 400, MARCUS)
    rachel = add_person("rachel_kim", "Rachel", "Kim", "rachel.kim@datadog.com",
                        "VP of Engineering", company)
    james = add_person("james_okonkwo", "James", "Okonkwo", "james.okonkwo@datadog.com",
                       "Staff Engineer", company)
    opp = add_opportunity("datadog_infra", "Datadog — Infrastructure Monitoring Add-on",
                          "Qualification", 55_000, ahead(weeks=10), company, MARCUS, rachel)

    thread = add_thread("datadog", "Datadog — Infrastructure Monitoring Add-on", ago(weeks=2))

    add_email(
        key="e1", thread_id=thread, channel_owner=MARCUS,
        subject="Infrastructure monitoring — intro",
        sender="rachel.kim@datadog.com", to=[MARCUS.email], cc=None,
        when=ago(weeks=2, hours=4),
        body=(
            "Hi Marcus,\n\nI came across your platform at KubeCon last month and your booth demo "
            "stuck with me. We're looking for something that integrates with our existing Datadog "
            "setup for deeper infrastructure monitoring — specifically around our Kubernetes "
            "clusters. Is that something you support?\n\nWe'd love to set up an intro call.\n\n"
            "Best,\nRachel Kim\nVP of Engineering, Datadog"
        ),
    )
    add_email(
        key="e2", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Infrastructure monitoring — intro",
        sender=MARCUS.email, to=["rachel.kim@datadog.com"], cc=None,
        when=ago(weeks=2, hours=2),
        body=(
            "Hi Rachel,\n\nThrilled you reached out — and yes, deep Kubernetes infra monitoring "
            "alongside an existing Datadog APM setup is one of our most common deployments.\n\n"
            "Would any of these work for an intro call: Thursday 10am, Thursday 2pm, or Friday "
            "11am (PT)? A couple of quick qualifiers so I can tailor it:\n\n"
            "• Roughly how many engineers are on the team?\n• What's your current stack for infra "
            "vs. APM?\n• Any specific clusters/regions driving this now?\n\nLooking forward to "
            "it!" + SIG_MARCUS
        ),
    )
    add_note(
        key="e3", title="Intro call with Rachel — strong fit, Grafana in the mix",
        when=ago(days=12), opportunity_id=opp, person_id=rachel,
        body=("great intro call w/ Rachel. ~40 engineers. current stack: Datadog for APM but they "
              "need deeper infra monitoring for their k8s clusters. Rachel has budget authority up "
              "to $60k WITHOUT needing VP approval — good. she's bringing in James (Staff Eng) to "
              "evaluate technical fit. RED FLAG: they're also evaluating Grafana Cloud, but Rachel "
              "prefers a commercial solution with real support. wants a technical deep-dive with "
              "James within 2 weeks."),
    )
    add_email(
        key="e4", thread_id=thread, channel_owner=MARCUS,
        subject="Technical deep-dive — scheduling + prep materials",
        sender=MARCUS.email, to=["rachel.kim@datadog.com", "james.okonkwo@datadog.com"], cc=None,
        when=ago(days=10),
        body=(
            "Hi Rachel and James,\n\nGreat to connect James. Let's get the technical deep-dive on "
            "the calendar — I've attached prep materials covering our Kubernetes operator, metric "
            "pipeline, and the Datadog + PagerDuty integrations.\n\nDoes early next week work? "
            "Happy to go as deep as you'd like on architecture." + SIG_MARCUS
        ),
    )
    add_email(
        key="e5", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Technical deep-dive — scheduling + prep materials",
        sender="james.okonkwo@datadog.com", to=[MARCUS.email], cc=["rachel.kim@datadog.com"],
        when=ago(days=9),
        body=(
            "Thanks Marcus. I've reviewed the docs. A few things I want to dig into on the call:\n\n"
            "1. Your Kubernetes operator vs. DaemonSet deployment model — tradeoffs at our scale\n"
            "2. Custom metric cardinality limits\n3. Integration with our existing PagerDuty "
            "alerting\n\nAlso FYI — I'll have Ben from our SRE team join. He manages the monitoring "
            "stack day-to-day, so his sign-off matters here.\n\nJames\n\nJames Okonkwo\nStaff "
            "Engineer, Datadog"
        ),
    )
    add_note(
        key="e6", title="Technical deep-dive — went great; pricing-at-scale is the concern",
        when=ago(days=7), opportunity_id=opp, person_id=james,
        body=("deep dive went great. James is sharp, was satisfied with our k8s operator approach "
              "over DaemonSet. Ben Asare (SRE, ben.asare@datadog.com) joined and was impressed "
              "with the PagerDuty integration — said it's noticeably better than Grafana Cloud's. "
              "only concern from James: custom metric PRICING at scale. wants to model their "
              "projected usage before committing. Rachel wasn't on this call. need Ben's contact "
              "into CRM, he's clearly a day-to-day stakeholder."),
    )
    add_email(
        key="e7", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Technical deep-dive — custom metric pricing question",
        sender="james.okonkwo@datadog.com", to=[MARCUS.email], cc=None,
        when=ago(days=5),
        body=(
            "Hey Marcus,\n\nRan the numbers on custom metrics. At our current growth rate we'd hit "
            "your 10k custom metric limit within about 6 months. What happens pricing-wise after "
            "that? This is honestly make-or-break for us — if the overage pricing is punitive it "
            "changes the whole calculus.\n\nJames"
        ),
    )
    add_email(
        key="e8", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Technical deep-dive — custom metric pricing question",
        sender=MARCUS.email, to=["james.okonkwo@datadog.com"], cc=None,
        when=ago(days=3),
        body=(
            "Hi James,\n\nGreat question, and a fair one to pressure-test. Beyond the 10k included "
            "custom metrics, overages are tiered — it steps DOWN per-metric as volume grows, so it "
            "doesn't balloon the way per-unit pricing can. Rather than quote you a generic number, "
            "let me build a custom pricing analysis against your actual projected usage.\n\nCould "
            "you share your metric growth data (current count + monthly growth rate)? I'll turn "
            "around a concrete 24-month cost model." + SIG_MARCUS
        ),
    )
    add_email(
        key="e9", thread_id=thread, channel_owner=MARCUS,
        subject="Re: Technical deep-dive — follow-up with the full team",
        sender="james.okonkwo@datadog.com", to=[MARCUS.email], cc=["rachel.kim@datadog.com"],
        when=ago(days=1),
        body=(
            "Shared the data with Rachel. She wants to loop back in — can we schedule a follow-up "
            "with all three of us (me, Rachel, and Ben) next week? She also mentioned she wants to "
            "discuss the contract timeline: we need monitoring in place before our Q3 "
            "infrastructure migration, so timing matters.\n\nJames"
        ),
    )

    # --- Agent-derived profile ---
    ben = add_shadow(key="e-ben", opportunity_id=opp, name="Ben Asare",
                     email_address="ben.asare@datadog.com", title_or_role="SRE",
                     company_crm_id=company, aliases=["Ben", "Ben from SRE"], mention_count=3,
                     status="pending_promotion", first_seen_at=ago(days=9), last_seen_at=ago(days=1))
    add_fact(key="e-f1", opportunity_id=opp, entity_type="person", entity_crm_id=rachel,
             fact_type="buying_signal", fact_value="Budget authority up to $60k without further VP approval",
             confidence=0.9, source_type="note", source_id=uid("note:e3"), extracted_at=ago(days=12))
    add_fact(key="e-f2", opportunity_id=opp, entity_type="opportunity", entity_crm_id=opp,
             fact_type="competitor", fact_value="Grafana Cloud being evaluated; our PagerDuty integration rated better",
             confidence=0.85, source_type="note", source_id=uid("note:e6"), extracted_at=ago(days=7))
    add_fact(key="e-f3", opportunity_id=opp, entity_type="opportunity", entity_crm_id=opp,
             fact_type="deadline", fact_value="Must have monitoring in place before Q3 infrastructure migration",
             confidence=0.85, source_type="email", source_id=uid("message:e9"), extracted_at=ago(days=1))
    add_fact(key="e-f4", opportunity_id=opp, entity_type="person", entity_crm_id=james,
             fact_type="concern", fact_value="Make-or-break: custom metric pricing at scale (projected to hit 10k limit in ~6 months)",
             confidence=0.9, source_type="email", source_id=uid("message:e7"), extracted_at=ago(days=5))
    add_fact(key="e-f5", opportunity_id=opp, entity_type="shadow", entity_crm_id=ben,
             fact_type="role", fact_value="Day-to-day owner of the monitoring stack; impressed by PagerDuty integration",
             confidence=0.8, source_type="note", source_id=uid("note:e6"), extracted_at=ago(days=7))
    add_relationship(key="e-r1", opportunity_id=opp, from_id=james, to_id=rachel,
                     relationship_type="reports_to", description="James evaluates technical fit for Rachel (VP Eng)",
                     confidence=0.8, source_type="email", first_seen_at=ago(days=9))
    add_relationship(key="e-r2", opportunity_id=opp, from_id=ben, to_id=james,
                     relationship_type="collaborates_with", description="Ben (SRE) supports James on the technical evaluation",
                     confidence=0.7, source_type="note", first_seen_at=ago(days=7))


# ===========================================================================
# Calendar — a realistic week for Sarah Chen (with a deliberate conflict)
# ===========================================================================


def build_calendar() -> None:
    # Anchor to Monday 00:00 of the current week (local-ish, in UTC).
    monday = (NOW - timedelta(days=NOW.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
        return monday + timedelta(days=day_offset, hours=hour, minutes=minute)

    sarah_att = (SARAH.email, None, SARAH.member_id)

    events = [
        # Monday
        ("mon-st007", "Weekly sales sync", at(0, 9, 30), at(0, 10, 0), [sarah_att, (MARCUS.email, None, MARCUS.member_id)], "Zoom"),
        ("mon-1on1", "1:1 with Sales Manager", at(0, 11, 0), at(0, 11, 30), [sarah_att], "Office - Room 4"),
        ("mon-lunch", "Lunch", at(0, 12, 0), at(0, 13, 0), [sarah_att], ""),
        # Tuesday — afternoon partly blocked (Emma proposed Tue/Wed afternoon for Figma).
        ("tue-pipeline", "Pipeline review (deep work block)", at(1, 13, 0), at(1, 15, 0), [sarah_att], ""),
        ("tue-airbnb-internal", "Internal: Airbnb deal strategy", at(1, 15, 30), at(1, 16, 0), [sarah_att], "Zoom"),
        # Wednesday — a hard conflict against any Wed 2pm proposal.
        ("wed-stripe-sec", "Stripe security review call (Nadia + Raj)", at(2, 14, 0), at(2, 14, 30),
         [sarah_att, ("nadia.osei@stripe.com", None, None), ("alex.rivera@stripe.com", uid("person:alex_rivera"), None)], "Google Meet"),
        ("wed-lunch", "Lunch", at(2, 12, 0), at(2, 13, 0), [sarah_att], ""),
        # Thursday
        ("thu-demo", "Prospect demo — Acme Corp", at(3, 10, 0), at(3, 11, 0), [sarah_att], "Zoom"),
        ("thu-focus", "Focus block: proposals", at(3, 13, 0), at(3, 15, 0), [sarah_att], ""),
        # Friday
        ("fri-forecast", "Forecast call", at(4, 9, 0), at(4, 9, 30), [sarah_att], "Zoom"),
        ("fri-lunch", "Lunch", at(4, 12, 0), at(4, 13, 0), [sarah_att], ""),
    ]
    for key, title, starts, ends, attendees, location in events:
        add_calendar_event(key=key, title=title, starts_at=starts, ends_at=ends,
                           attendees=attendees, location=location)


# ===========================================================================
# DDL for the Follow-Up Agent's own schema (same database, new schema)
# ===========================================================================

AGENT_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {AGENT_SCHEMA};

CREATE TABLE IF NOT EXISTS {AGENT_SCHEMA}.profile_facts (
    id uuid PRIMARY KEY,
    opportunity_id uuid NOT NULL,
    entity_type text NOT NULL,
    entity_crm_id uuid,
    fact_type text NOT NULL,
    fact_value text NOT NULL,
    confidence double precision NOT NULL,
    source_type text NOT NULL,
    source_id uuid,
    extracted_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS {AGENT_SCHEMA}.profile_relationships (
    id uuid PRIMARY KEY,
    opportunity_id uuid NOT NULL,
    from_entity_crm_id uuid,
    to_entity_crm_id uuid,
    relationship_type text NOT NULL,
    description text,
    confidence double precision NOT NULL,
    source_type text NOT NULL,
    first_seen_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS {AGENT_SCHEMA}.shadow_entities (
    id uuid PRIMARY KEY,
    opportunity_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    email_address text,
    title_or_role text,
    company_crm_id uuid,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    mention_count integer NOT NULL DEFAULT 0,
    status text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS {AGENT_SCHEMA}.risk_snapshots (
    id uuid PRIMARY KEY,
    opportunity_id uuid NOT NULL,
    score integer NOT NULL,
    factors jsonb NOT NULL,
    computed_at timestamptz NOT NULL
);
"""

# Columns that are jsonb and therefore must be encoded as JSON for asyncpg.
JSONB_COLUMNS = {
    "domainNameSecondaryLinks", "emailsAdditionalEmails", "phonesAdditionalPhones",
    "linkedinLinkSecondaryLinks", "xLinkSecondaryLinks", "conferenceLinkSecondaryLinks",
    "connectionParameters", "createdByContext", "updatedByContext",
    "aliases", "factors",
}


# ===========================================================================
# Build + insert
# ===========================================================================


def build_all() -> None:
    build_reps()
    register_rep_handles()
    build_channels()
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    build_calendar()


def _resolve_schema(schema: str, ws_schema: str) -> str:
    return ws_schema if schema == WS else schema


def _resolve_value(column: str, value: Any, workspace_id: uuid.UUID) -> Any:
    if value == WORKSPACE_ID_SENTINEL:
        value = workspace_id
    if column in JSONB_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value)
    return value


async def _discover_target(conn: asyncpg.Connection) -> tuple[str, uuid.UUID]:
    ws_schema = await conn.fetchval(
        "SELECT table_schema FROM information_schema.tables "
        "WHERE table_schema LIKE 'workspace\\_%' AND table_name = 'person' "
        "ORDER BY table_schema LIMIT 1"
    )
    if not ws_schema or not re.fullmatch(r"workspace_[a-z0-9]+", ws_schema):
        raise RuntimeError(f"Could not discover a workspace schema (got: {ws_schema!r}). Is Twenty initialized?")
    workspace_id = await conn.fetchval('SELECT id FROM core.workspace ORDER BY "createdAt" LIMIT 1')
    if workspace_id is None:
        raise RuntimeError("No workspace found in core.workspace.")
    return ws_schema, workspace_id


async def _build_reconciliation(
    conn: asyncpg.Connection, ws_schema: str
) -> tuple[dict[uuid.UUID, uuid.UUID], set[uuid.UUID]]:
    """Map our deterministic company/person ids onto rows that already exist in the
    demo workspace (matched by unique domain / email), so we don't violate Twenty's
    unique constraints and instead attach our activities to the existing records."""
    remap: dict[uuid.UUID, uuid.UUID] = {}
    skip_ids: set[uuid.UUID] = set()

    company_rows = await conn.fetch(
        f'SELECT id, "domainNamePrimaryLinkUrl" AS url FROM "{ws_schema}".company '
        'WHERE "domainNamePrimaryLinkUrl" IS NOT NULL AND "deletedAt" IS NULL'
    )
    url_to_id: dict[str, uuid.UUID] = {}
    for r in company_rows:
        url_to_id.setdefault(r["url"], r["id"])  # first match wins (stable)
    for row in ROWS.get((WS, "company"), []):
        existing = url_to_id.get(row.get("domainNamePrimaryLinkUrl"))
        if existing and existing != row["id"]:
            remap[row["id"]] = existing
            skip_ids.add(row["id"])

    person_rows = await conn.fetch(
        f'SELECT id, "emailsPrimaryEmail" AS email FROM "{ws_schema}".person '
        'WHERE "emailsPrimaryEmail" IS NOT NULL AND "deletedAt" IS NULL'
    )
    email_to_id = {r["email"]: r["id"] for r in person_rows}
    for row in ROWS.get((WS, "person"), []):
        existing = email_to_id.get(row.get("emailsPrimaryEmail"))
        if existing and existing != row["id"]:
            remap[row["id"]] = existing
            skip_ids.add(row["id"])

    return remap, skip_ids


async def seed_database(dsn: Optional[str] = None) -> None:
    """Insert all seed data into the live Twenty database (idempotent)."""
    dsn = dsn or os.environ.get("PG_DATABASE_URL", "postgres://postgres:postgres@localhost:5432/default")

    build_all()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.set_type_codec("jsonb", encoder=lambda v: v, decoder=json.loads, schema="pg_catalog")

        ws_schema, workspace_id = await _discover_target(conn)
        print(f"Target workspace schema: {ws_schema}")
        print(f"Target workspace id:     {workspace_id}")

        # Create the Follow-Up Agent's own schema/tables in the same database.
        await conn.execute(AGENT_DDL)

        # Reconcile with Twenty's demo data: companies (unique domain) and people
        # (unique email) may already exist. Reuse the existing rows and remap our
        # deterministic ids onto them everywhere they're referenced.
        remap, skip_ids = await _build_reconciliation(conn, ws_schema)
        if remap:
            print(f"Reusing {len(remap)} existing records (companies/people) by domain/email.")

        def remap_id(value: Any) -> Any:
            if isinstance(value, uuid.UUID) and value in remap:
                return remap[value]
            return value

        total = 0
        async with conn.transaction():
            for schema, table in INSERT_ORDER:
                rows = ROWS.get((schema, table), [])
                if not rows:
                    continue
                real_schema = _resolve_schema(schema, ws_schema)
                columns = list(rows[0].keys())
                col_sql = ", ".join(f'"{c}"' for c in columns)
                placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
                query = (
                    f'INSERT INTO "{real_schema}"."{table}" ({col_sql}) '
                    f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
                )
                records = []
                for row in rows:
                    if row["id"] in skip_ids:
                        continue
                    records.append(tuple(
                        _resolve_value(c, remap_id(row.get(c)), workspace_id) for c in columns
                    ))
                if not records:
                    print(f"  {real_schema}.{table}: 0 rows (all reused)")
                    continue
                await conn.executemany(query, records)
                total += len(records)
                print(f"  {real_schema}.{table}: {len(records)} rows")

        print(f"\nDone. {total} rows processed across {len([k for k in ROWS if ROWS[k]])} tables.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_database())
