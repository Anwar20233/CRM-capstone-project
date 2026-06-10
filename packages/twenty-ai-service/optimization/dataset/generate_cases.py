"""Generate the labelled Writer-agent optimization dataset.

Produces ``writer_cases.json`` — orchestrator→writer instructions, each with a
*gold* label describing the optimal tool trajectory and outcome.

**Id-based by construction.** The Writer is write-only and never searches, so any
case that touches an existing record references a **real fixture id** (see
``fixtures.py``), exactly as the orchestrator would hand the writer resolved ids
in production. New-record creates (torn down per rollout) use fresh names from the
NER ``cases_data.json`` entity pools so masking still has real PII to mask.

Run order (ids are workspace-specific, so seed first)::

    .venv/bin/python optimization/dataset/fixtures.py --seed
    .venv/bin/python optimization/dataset/generate_cases.py

Each case::

    {"id", "category", "request", "gold": {outcome, primary_action, required_tools,
     needs_resolve_date, min_tool_calls}, "split"}
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVICE_ROOT = _HERE.parent.parent
_CASES_DATA = _SERVICE_ROOT / "notebooks" / "cases_data.json"
_FIXTURES = _HERE / "fixtures.json"
_OUTPUT = _HERE / "writer_cases.json"

_SEED = 20260610

_RELATIVE_DATES = ["next Friday", "next Monday", "in 2 weeks", "end of month", "tomorrow", "next week"]
_STAGES = ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _load_entity_pools() -> dict[str, list[str]]:
    pools: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    raw = json.loads(_CASES_DATA.read_text(encoding="utf-8"))
    for case in raw:
        for label, values in case.get("expected", {}).items():
            for value in values:
                value = value.strip()
                if value and value not in seen[label]:
                    seen[label].add(value)
                    pools[label].append(value)
    pools.setdefault("person", []).extend(["Sarah Connor", "James Whitfield"])
    pools.setdefault("company", []).extend(["NovaTech Solutions", "Cyberdyne Systems"])
    pools.setdefault("job title", []).extend(["Head of Operations", "CTO"])
    pools.setdefault("location", []).extend(["Dubai", "Riyadh"])
    pools.setdefault("money", []).extend(["$45,000", "$120,000"])
    pools.setdefault("email address", []).extend(["james@novatech.ae"])
    return pools


def _load_fixtures() -> dict[str, list[dict]]:
    if not _FIXTURES.exists():
        raise SystemExit(
            "fixtures.json not found. Run: .venv/bin/python optimization/dataset/fixtures.py --seed"
        )
    fixtures = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    for key in ("companies", "persons", "opportunities", "notes", "tasks"):
        if not fixtures.get(key):
            raise SystemExit(f"fixtures.json has no '{key}' — re-seed fixtures.")
    return fixtures


# ---------------------------------------------------------------------------
# Gold builder
# ---------------------------------------------------------------------------

def _gold(outcome, primary_action, *, required_tools=None, needs_resolve_date=False, min_tool_calls=3):
    return {
        "outcome": outcome,
        "primary_action": primary_action,
        "required_tools": required_tools
        if required_tools is not None
        else (["get_tool_catalog", "learn_tools", "execute_tool"] if primary_action else []),
        "needs_resolve_date": needs_resolve_date,
        "min_tool_calls": min_tool_calls if primary_action else 0,
    }


# ---------------------------------------------------------------------------
# Case factory
# ---------------------------------------------------------------------------

def _make_cases(rng: random.Random, pools: dict[str, list[str]], fx: dict[str, list[dict]]) -> list[dict]:
    companies = fx["companies"]
    persons = fx["persons"]
    opportunities = fx["opportunities"]
    notes = fx["notes"]
    tasks = fx["tasks"]

    def pick(label):
        return rng.choice(pools[label])

    def first_last(full_name: str) -> tuple[str, str]:
        parts = full_name.split()
        return (parts[0], parts[-1] if len(parts) > 1 else "")

    def company_id():
        return rng.choice(companies)["id"]

    cases: list[tuple[str, str, dict]] = []

    # -- create_person (16) — at an existing company id --------------------
    for _ in range(16):
        first, last = first_last(pick("person"))
        title, cid = pick("job title"), company_id()
        cases.append((
            "create_person",
            f"Create a person named {first} {last}, {title}, at the company with id {cid}.",
            _gold("executed", "create_person"),
        ))

    # -- create_company (10) — no prerequisite -----------------------------
    for _ in range(10):
        company, city = pick("company"), pick("location")
        cases.append((
            "create_company",
            f"Add a new company called {company} based in {city}; mark it as an ideal customer profile.",
            _gold("executed", "create_company"),
        ))

    # -- create_opportunity (8) — for an existing company id ---------------
    for _ in range(8):
        cid, amount = company_id(), pick("money")
        name = f"{pick('company')} Deal"
        cases.append((
            "create_opportunity",
            f"Create an opportunity named '{name}' for the company with id {cid}, "
            f"amount {amount}, at the NEW stage.",
            _gold("executed", "create_opportunity"),
        ))

    # -- update_person / company / opportunity (8) — by id -----------------
    for _ in range(4):
        person, email = rng.choice(persons), pick("email address")
        cases.append((
            "update_person",
            f"Update the person with id {person['id']}: set their primary email to {email}.",
            _gold("executed", "update_person"),
        ))
    for _ in range(2):
        company, city = rng.choice(companies), pick("location")
        cases.append((
            "update_company",
            f"Update the company with id {company['id']}: set its city to {city}.",
            _gold("executed", "update_company"),
        ))
    for _ in range(2):
        opp = rng.choice(opportunities)
        cases.append((
            "update_opportunity",
            f"Update the opportunity with id {opp['id']}: move it to the MEETING stage.",
            _gold("executed", "update_opportunity"),
        ))

    # -- note / task (10) — standalone (no entity link needed) -------------
    for _ in range(5):
        person = rng.choice(persons)
        cases.append((
            "note_task",
            f"Create a note titled 'Demo interest — {person['name']}' "
            f"with a short body noting they confirmed interest in a demo.",
            _gold("executed", "create_note"),
        ))
    for _ in range(5):
        person = rng.choice(persons)
        cases.append((
            "note_task",
            f"Create a task titled 'Call {person['name']} about the proposal'.",
            _gold("executed", "create_task"),
        ))

    # -- relative_date (10) — resolve_date before the write ----------------
    for _ in range(6):
        person, phrase = rng.choice(persons), rng.choice(_RELATIVE_DATES)
        cases.append((
            "relative_date",
            f"Create a task titled 'Follow up with {person['name']}' due {phrase}.",
            _gold("executed", "create_task",
                  required_tools=["resolve_date", "get_tool_catalog", "learn_tools", "execute_tool"],
                  needs_resolve_date=True, min_tool_calls=4),
        ))
    for _ in range(4):
        cid, phrase, amount = company_id(), rng.choice(_RELATIVE_DATES), pick("money")
        name = f"{pick('company')} Deal"
        cases.append((
            "relative_date",
            f"Create an opportunity named '{name}' for the company with id {cid}, "
            f"amount {amount}, at the NEW stage, closing {phrase}.",
            _gold("executed", "create_opportunity",
                  required_tools=["resolve_date", "get_tool_catalog", "learn_tools", "execute_tool"],
                  needs_resolve_date=True, min_tool_calls=4),
        ))

    # -- tier3_confirm: deletes by id (10) ---------------------------------
    for _ in range(5):
        person = rng.choice(persons)
        cases.append((
            "tier3_confirm",
            f"Delete the person with id {person['id']}.",
            _gold("confirmation_required", "delete_person"),
        ))
    for _ in range(3):
        company = rng.choice(companies)
        cases.append((
            "tier3_confirm",
            f"Delete the company with id {company['id']}.",
            _gold("confirmation_required", "delete_company"),
        ))
    for _ in range(2):
        opp = rng.choice(opportunities)
        cases.append((
            "tier3_confirm",
            f"Delete the opportunity with id {opp['id']}.",
            _gold("confirmation_required", "delete_opportunity"),
        ))
    cases.append((
        "tier3_confirm",
        f"Delete the note with id {rng.choice(notes)['id']}.",
        _gold("confirmation_required", "delete_note"),
    ))
    cases.append((
        "tier3_confirm",
        f"Delete the task with id {rng.choice(tasks)['id']}.",
        _gold("confirmation_required", "delete_task"),
    ))

    # -- tier3_confirm: advance stage by deal id (3) -----------------------
    for _ in range(3):
        opp = rng.choice(opportunities)
        cases.append((
            "tier3_confirm",
            f"Advance the deal with id {opp['id']} to the CUSTOMER stage.",
            _gold("confirmation_required", "advance_deal_stage"),
        ))

    # -- tier3_confirm: bulk update (filter-based, returns confirm) (3) ----
    for _ in range(3):
        cases.append((
            "tier3_confirm",
            "Move all opportunities currently in the NEW stage to the MEETING stage.",
            _gold("confirmation_required", "update_many_opportunities"),
        ))

    # -- bulk create_many_people (4) — at an existing company id -----------
    for _ in range(4):
        f1, l1 = first_last(pick("person"))
        f2, l2 = first_last(pick("person"))
        cid = company_id()
        cases.append((
            "bulk",
            f"Create two people at the company with id {cid}: {f1} {l1} and {f2} {l2}.",
            _gold("executed", "create_many_people"),
        ))

    # -- read_rejection (10) — writer must refuse, never search ------------
    read_requests = [
        f"Find all people at {pick('company')}.",
        f"Search for {pick('person')} and tell me their email.",
        f"List every opportunity in the {rng.choice(_STAGES)} stage.",
        f"How many companies are based in {pick('location')}?",
        f"Look up the phone number for {pick('person')}.",
        f"Show me the most recent notes on {pick('company')}.",
        f"Who is the primary contact at {pick('company')}?",
        f"Get the details of the {pick('company')} opportunity.",
        f"Find {pick('person')}'s record and summarise their activity.",
        "Retrieve all tasks due this week.",
    ]
    for text in read_requests:
        cases.append(("read_rejection", text,
                      _gold("rejected_out_of_scope", None, required_tools=[], min_tool_calls=0)))

    # -- clarify (6) — missing required info, must ask not fabricate -------
    for text in ["Create a new person.", "Update the opportunity.", "Add a note.",
                 "Create a company.", "Update that person's title.", "Delete the record."]:
        cases.append(("clarify", text,
                      _gold("clarification_needed", None, required_tools=[], min_tool_calls=0)))

    # -- out_of_scope (3) — non-CRM / denied capability --------------------
    for text in [
        "Run a Python script to calculate our quarterly churn rate.",
        "Send an HTTP request to our analytics API and import the results.",
        "Post an announcement about this deal to our company Slack channel.",
    ]:
        cases.append(("out_of_scope", text,
                      _gold("rejected_out_of_scope", None, required_tools=[], min_tool_calls=0)))

    # Assign stable ids per category.
    per_category_index: dict[str, int] = defaultdict(int)
    out: list[dict] = []
    for category, request, gold in cases:
        per_category_index[category] += 1
        out.append({
            "id": f"W-{category.upper()}-{per_category_index[category]:02d}",
            "category": category,
            "request": request,
            "gold": gold,
        })
    return out


# ---------------------------------------------------------------------------
# Stratified split
# ---------------------------------------------------------------------------

def _assign_splits(rng: random.Random, cases: list[dict]) -> None:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_category[case["category"]].append(case)
    for group in by_category.values():
        rng.shuffle(group)
        n = len(group)
        n_test = max(1, round(n * 0.2))
        n_val = max(1, round(n * 0.2)) if n >= 3 else 0
        for index, case in enumerate(group):
            case["split"] = "test" if index < n_test else "val" if index < n_test + n_val else "train"


def main() -> None:
    pools = _load_entity_pools()
    fixtures = _load_fixtures()
    cases = _make_cases(random.Random(_SEED), pools, fixtures)
    _assign_splits(random.Random(_SEED + 1), cases)
    _OUTPUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")

    by_split: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    for case in cases:
        by_split[case["split"]] += 1
        by_category[case["category"]] += 1
    print(f"Wrote {len(cases)} cases to {_OUTPUT.relative_to(_SERVICE_ROOT)}")
    print("Split:", dict(by_split))
    print("Categories:", dict(sorted(by_category.items())))


if __name__ == "__main__":
    main()
