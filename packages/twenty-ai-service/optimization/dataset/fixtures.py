"""Seed prerequisite CRM records for the Writer optimization dataset.

The Writer is **write-only** — it never searches. So any case that references an
existing record (update/delete a person, create a person *at a company*, advance a
deal, …) must point at a **real record id** that already exists in the workspace.
This module creates those prerequisites up front via the bridge (system-level, no
LLM, no WritePolicy), captures their real UUIDs, and writes ``fixtures.json``.
``generate_cases.py`` then bakes those ids straight into the orchestrator→writer
instructions — mirroring production, where the orchestrator resolves names→ids and
hands the writer ids, never names.

Usage::

    .venv/bin/python optimization/dataset/fixtures.py --seed       # create + write fixtures.json
    .venv/bin/python optimization/dataset/fixtures.py --teardown   # delete everything in fixtures.json

Schemas (from the live bridge) require ``position`` on every create, plus
``idealCustomerProfile`` on companies and ``stage`` on opportunities.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

# sys.path bootstrap so `agent.*` / `bridge_client` import when run directly.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from optimization.harness import config  # noqa: E402
from bridge_client import forward  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures.json"

# Seed material — small, fixed, realistic.
_COMPANY_SEEDS = [
    {"name": "Northwind Trading", "city": "Dubai"},
    {"name": "Helios Robotics", "city": "Riyadh"},
    {"name": "Atlas Freight", "city": "Amman"},
    {"name": "Verdant Foods", "city": "Cairo"},
]
_PERSON_SEEDS = [
    ("Maya", "Haddad", "Head of Operations"),
    ("Omar", "Saleh", "Procurement Lead"),
    ("Lena", "Fischer", "VP Sales"),
    ("Tariq", "Nasser", "CTO"),
    ("Priya", "Menon", "Account Manager"),
    ("Sven", "Larsson", "Finance Director"),
]
_OPPORTUNITY_SEEDS = [
    ("Northwind Q3 Expansion", 45000),
    ("Helios Pilot Program", 120000),
    ("Atlas Renewal", 30000),
]
_NOTE_SEEDS = ["Kickoff summary", "Discovery call recap"]
_TASK_SEEDS = ["Send proposal", "Schedule follow-up"]


def _identity() -> dict[str, str]:
    ws = os.environ.get("TWENTY_WORKSPACE_ID")
    user = os.environ.get("TWENTY_USER_ID")
    role = os.environ.get("TWENTY_WRITER_ROLE_ID") or os.environ.get("TWENTY_ROLE_ID")
    return {"workspaceId": ws, "roleId": role, "userId": user}


async def _create(tool: str, args: dict[str, Any]) -> str | None:
    envelope = await forward("execute", {"tool": tool, "args": args, **_identity()})
    if not (isinstance(envelope, dict) and envelope.get("ok")):
        print(f"  ! {tool} failed: {(envelope or {}).get('error')}")
        return None
    record_id = _first_id(envelope.get("data"))
    print(f"  + {tool} -> {record_id}")
    return record_id


async def _delete(tool: str, record_id: str) -> bool:
    envelope = await forward("execute", {"tool": tool, "args": {"id": record_id}, **_identity()})
    return bool(isinstance(envelope, dict) and envelope.get("ok"))


def _first_id(data: Any, depth: int = 0) -> str | None:
    if depth > 3 or data is None:
        return None
    if isinstance(data, dict):
        if isinstance(data.get("id"), str):
            return data["id"]
        for value in data.values():
            found = _first_id(value, depth + 1)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _first_id(item, depth + 1)
            if found:
                return found
    return None


async def _seed() -> dict[str, list[dict[str, str]]]:
    fixtures: dict[str, list[dict[str, str]]] = {
        "companies": [], "persons": [], "opportunities": [], "notes": [], "tasks": [],
    }

    print("Seeding companies ...")
    for seed in _COMPANY_SEEDS:
        cid = await _create("create_company", {
            "name": seed["name"], "idealCustomerProfile": True, "position": "last",
            "address": {"addressCity": seed["city"]},
        })
        if cid:
            fixtures["companies"].append({"id": cid, "name": seed["name"], "city": seed["city"]})

    company_ids = [c["id"] for c in fixtures["companies"]] or [None]

    print("Seeding persons ...")
    for index, (first, last, title) in enumerate(_PERSON_SEEDS):
        pid = await _create("create_person", {
            "name": {"firstName": first, "lastName": last},
            "jobTitle": title,
            "companyId": company_ids[index % len(company_ids)],
            "position": "last",
        })
        if pid:
            fixtures["persons"].append({
                "id": pid, "name": f"{first} {last}", "title": title,
                "companyId": company_ids[index % len(company_ids)],
            })

    print("Seeding opportunities ...")
    for index, (name, amount) in enumerate(_OPPORTUNITY_SEEDS):
        oid = await _create("create_opportunity", {
            "name": name, "stage": "NEW", "position": "last",
            "companyId": company_ids[index % len(company_ids)],
            "amount": {"amountMicros": amount * 1_000_000, "currencyCode": "USD"},
        })
        if oid:
            fixtures["opportunities"].append({"id": oid, "name": name})

    print("Seeding notes ...")
    for title in _NOTE_SEEDS:
        nid = await _create("create_note", {"title": title, "position": "last"})
        if nid:
            fixtures["notes"].append({"id": nid, "title": title})

    print("Seeding tasks ...")
    for title in _TASK_SEEDS:
        tid = await _create("create_task", {"title": title, "position": "last"})
        if tid:
            fixtures["tasks"].append({"id": tid, "title": title})

    return fixtures


async def _teardown(fixtures: dict[str, list[dict[str, str]]]) -> None:
    plan = [
        ("opportunities", "delete_opportunity"),
        ("persons", "delete_person"),
        ("companies", "delete_company"),
        ("notes", "delete_note"),
        ("tasks", "delete_task"),
    ]
    for key, tool in plan:
        for record in fixtures.get(key, []):
            ok = await _delete(tool, record["id"])
            print(f"  {'-' if ok else '!'} {tool} {record['id']} {'deleted' if ok else 'FAILED'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed/teardown CRM fixtures for the writer dataset.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed", action="store_true", help="create fixtures + write fixtures.json")
    group.add_argument("--teardown", action="store_true", help="delete records listed in fixtures.json")
    args = parser.parse_args()

    config.load_env()

    if args.seed:
        fixtures = asyncio.run(_seed())
        _FIXTURES.write_text(json.dumps(fixtures, indent=2), encoding="utf-8")
        counts = {k: len(v) for k, v in fixtures.items()}
        print(f"\nWrote {_FIXTURES.name}: {counts}")
        if any(len(v) == 0 for v in fixtures.values()):
            print("WARNING: some fixture groups are empty — check bridge errors above.")
    else:
        if not _FIXTURES.exists():
            print("No fixtures.json to tear down.")
            return
        fixtures = json.loads(_FIXTURES.read_text(encoding="utf-8"))
        asyncio.run(_teardown(fixtures))
        print("Teardown complete.")


if __name__ == "__main__":
    main()
