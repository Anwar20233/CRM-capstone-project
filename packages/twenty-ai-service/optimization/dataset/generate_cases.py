"""Generate the labelled Writer-agent optimization dataset.

Produces ``writer_cases.json`` — 100 orchestrator->writer instructions, each with
a *gold* label describing the optimal tool trajectory and outcome. The cases are
seeded with **real entities** (names, companies, emails, phones, dates, money)
pulled from ``notebooks/cases_data.json`` so the prompts look like genuine
orchestrator hand-offs and exercise the PII masking layer.

Deterministic: a fixed seed makes regeneration reproducible, and the train/val/
test split is stratified by category so every split sees every case type. The
held-out **test** split (20) is never shown to the optimizer.

Each case::

    {
      "id": "W-CREATE_PERSON-03",
      "category": "create_person",
      "request": "Create a person ...",
      "gold": {
        "outcome": "executed",              # executed|confirmation_required|
                                            #   rejected_out_of_scope|clarification_needed
        "primary_action": "create_person",  # the tool inside execute_tool, or null
        "required_tools": ["learn_tools", "execute_tool"],
        "needs_resolve_date": false,
        "min_tool_calls": 2                 # parsimony target (fewest calls that work)
      },
      "split": "train"                       # train|val|test
    }

Run::

    .venv/bin/python optimization/dataset/generate_cases.py
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SERVICE_ROOT = _HERE.parent.parent  # packages/twenty-ai-service
_CASES_DATA = _SERVICE_ROOT / "notebooks" / "cases_data.json"
_OUTPUT = _HERE / "writer_cases.json"

_SEED = 20260610


# ---------------------------------------------------------------------------
# Entity pools — pulled from the NER cases so prompts use realistic PII
# ---------------------------------------------------------------------------

def _load_entity_pools() -> dict[str, list[str]]:
    """Collect de-duplicated entity values by type from cases_data.json."""
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

    # Fallbacks so the generator still works if a label is sparse.
    pools.setdefault("person", []).extend(["Sarah Connor", "James Whitfield"])
    pools.setdefault("company", []).extend(["NovaTech Solutions", "Cyberdyne Systems"])
    pools.setdefault("job title", []).extend(["Head of Operations", "CTO"])
    pools.setdefault("location", []).extend(["Dubai", "Riyadh"])
    pools.setdefault("money", []).extend(["$45,000", "$120,000"])
    pools.setdefault("email address", []).extend(["james@novatech.ae"])
    pools.setdefault("phone number", []).extend(["+971 50 123 4567"])
    return pools


# Relative-date phrases the writer's resolve_date tool actually understands.
_RELATIVE_DATES = [
    "next Friday",
    "next Monday",
    "in 2 weeks",
    "end of month",
    "tomorrow",
    "next week",
]

_PIPELINE_STAGES = ["Discovery", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]


# ---------------------------------------------------------------------------
# Gold builders
# ---------------------------------------------------------------------------

def _gold(
    outcome: str,
    primary_action: str | None,
    *,
    required_tools: list[str] | None = None,
    needs_resolve_date: bool = False,
    min_tool_calls: int = 2,
) -> dict:
    return {
        "outcome": outcome,
        "primary_action": primary_action,
        # learn-before-execute is the non-negotiable core; get_tool_catalog is
        # intentionally NOT required so the optimizer can learn to skip browsing
        # when the action is already known (fewer calls).
        "required_tools": required_tools
        if required_tools is not None
        else (["learn_tools", "execute_tool"] if primary_action else []),
        "needs_resolve_date": needs_resolve_date,
        "min_tool_calls": min_tool_calls,
    }


# ---------------------------------------------------------------------------
# Per-category case factories. Each returns (request, gold).
# ---------------------------------------------------------------------------

def _make_cases(rng: random.Random, pools: dict[str, list[str]]) -> list[dict]:
    def pick(label: str) -> str:
        return rng.choice(pools[label])

    cases: list[tuple[str, str, dict]] = []  # (category, request, gold)

    # -- create_person (16) ------------------------------------------------
    for _ in range(16):
        person, company = pick("person"), pick("company")
        title = pick("job title")
        cases.append((
            "create_person",
            f"Create a person record for {person}, {title} at {company}.",
            _gold("executed", "create_person", min_tool_calls=2),
        ))

    # -- create_company (10) ----------------------------------------------
    for _ in range(10):
        company, location = pick("company"), pick("location")
        cases.append((
            "create_company",
            f"Add a new company {company} based in {location} to the CRM.",
            _gold("executed", "create_company", min_tool_calls=2),
        ))

    # -- create_opportunity (8) -------------------------------------------
    for _ in range(8):
        company, amount = pick("company"), pick("money")
        cases.append((
            "create_opportunity",
            f"Create an opportunity for {company} worth {amount} at the "
            f"{rng.choice(_PIPELINE_STAGES[:3])} stage.",
            _gold("executed", "create_opportunity", min_tool_calls=2),
        ))

    # -- update_person / update_company / update_opportunity (8) ----------
    for _ in range(4):
        person, email = pick("person"), pick("email address")
        cases.append((
            "update_person",
            f"Update {person}'s email address to {email}.",
            _gold("executed", "update_person", min_tool_calls=2),
        ))
    for _ in range(2):
        company, location = pick("company"), pick("location")
        cases.append((
            "update_company",
            f"Update the address of {company} to {location}.",
            _gold("executed", "update_company", min_tool_calls=2),
        ))
    for _ in range(2):
        company = pick("company")
        cases.append((
            "update_opportunity",
            f"Update the opportunity for {company} to the "
            f"{rng.choice(_PIPELINE_STAGES[:3])} stage.",
            _gold("executed", "update_opportunity", min_tool_calls=2),
        ))

    # -- note / task (10) — Tier 1 ----------------------------------------
    for _ in range(5):
        person = pick("person")
        cases.append((
            "note_task",
            f"Add a note to {person} saying they confirmed interest in a demo.",
            _gold("executed", "create_note", min_tool_calls=2),
        ))
    for _ in range(5):
        person = pick("person")
        cases.append((
            "note_task",
            f"Create a follow-up task to call {person} about the proposal.",
            _gold("executed", "create_task", min_tool_calls=2),
        ))

    # -- relative_date (10) — must call resolve_date first -----------------
    for _ in range(6):
        person, phrase = pick("person"), rng.choice(_RELATIVE_DATES)
        cases.append((
            "relative_date",
            f"Create a task to follow up with {person} {phrase}.",
            _gold(
                "executed", "create_task",
                required_tools=["resolve_date", "learn_tools", "execute_tool"],
                needs_resolve_date=True, min_tool_calls=3,
            ),
        ))
    for _ in range(4):
        company, phrase, amount = pick("company"), rng.choice(_RELATIVE_DATES), pick("money")
        cases.append((
            "relative_date",
            f"Create an opportunity for {company} worth {amount} closing {phrase}.",
            _gold(
                "executed", "create_opportunity",
                required_tools=["resolve_date", "learn_tools", "execute_tool"],
                needs_resolve_date=True, min_tool_calls=3,
            ),
        ))

    # -- tier3_confirm: deletes (10) --------------------------------------
    for _ in range(5):
        person = pick("person")
        cases.append((
            "tier3_confirm",
            f"Delete the person record for {person}.",
            _gold("confirmation_required", "delete_person", min_tool_calls=2),
        ))
    for _ in range(3):
        company = pick("company")
        cases.append((
            "tier3_confirm",
            f"Delete the company {company} from the CRM.",
            _gold("confirmation_required", "delete_company", min_tool_calls=2),
        ))
    for _ in range(2):
        company = pick("company")
        cases.append((
            "tier3_confirm",
            f"Remove the opportunity associated with {company}.",
            _gold("confirmation_required", "delete_opportunity", min_tool_calls=2),
        ))

    # -- tier3_confirm: advance stage (3) ---------------------------------
    for _ in range(3):
        company = pick("company")
        cases.append((
            "tier3_confirm",
            f"Advance the {company} deal to Closed Won.",
            _gold("confirmation_required", "advance_deal_stage", min_tool_calls=2),
        ))

    # -- tier3_confirm: bulk update (3) -----------------------------------
    for _ in range(3):
        stage = rng.choice(_PIPELINE_STAGES[:3])
        cases.append((
            "tier3_confirm",
            f"Move all opportunities in the {stage} stage to Negotiation.",
            _gold("confirmation_required", "update_many_opportunities", min_tool_calls=2),
        ))

    # -- bulk create_many (4) ---------------------------------------------
    for _ in range(4):
        p1, p2, company = pick("person"), pick("person"), pick("company")
        cases.append((
            "bulk",
            f"Create people records for {p1} and {p2}, both at {company}.",
            _gold("executed", "create_many_people", min_tool_calls=2),
        ))

    # -- read_rejection (10) — writer must refuse, NOT search --------------
    read_requests = [
        ("Find all people at {company}.", "company"),
        ("Search for {person} and tell me their email.", "person"),
        ("List every opportunity in the {stage} stage.", "stage"),
        ("How many companies are based in {location}?", "location"),
        ("Look up the phone number for {person}.", "person"),
        ("Show me the most recent notes on {company}.", "company"),
        ("Who is the primary contact at {company}?", "company"),
        ("Get the details of the {company} opportunity.", "company"),
        ("Find {person}'s record and summarise their activity.", "person"),
        ("Retrieve all tasks due this week.", None),
    ]
    for template, kind in read_requests:
        if kind == "company":
            text = template.format(company=pick("company"))
        elif kind == "person":
            text = template.format(person=pick("person"))
        elif kind == "location":
            text = template.format(location=pick("location"))
        elif kind == "stage":
            text = template.format(stage=rng.choice(_PIPELINE_STAGES))
        else:
            text = template
        cases.append((
            "read_rejection",
            text,
            _gold("rejected_out_of_scope", None, required_tools=[], min_tool_calls=0),
        ))

    # -- clarify (6) — missing required info, must ask not fabricate -------
    clarify_requests = [
        "Create a new person.",
        "Update the opportunity.",
        "Add a note.",
        "Create a company.",
        "Update that person's title.",
        "Delete the record.",
    ]
    for text in clarify_requests:
        cases.append((
            "clarify",
            text,
            _gold("clarification_needed", None, required_tools=[], min_tool_calls=0),
        ))

    # -- out_of_scope (3) — non-CRM / denied capability -------------------
    out_of_scope_requests = [
        "Run a Python script to calculate our quarterly churn rate.",
        "Send an HTTP request to our analytics API and import the results.",
        "Post an announcement about this deal to our company Slack channel.",
    ]
    for text in out_of_scope_requests:
        cases.append((
            "out_of_scope",
            text,
            _gold("rejected_out_of_scope", None, required_tools=[], min_tool_calls=0),
        ))

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
    """Stratify by category into 60/20/20 train/val/test (mutates in place)."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_category[case["category"]].append(case)

    for _category, group in by_category.items():
        rng.shuffle(group)
        n = len(group)
        n_test = max(1, round(n * 0.2))
        n_val = max(1, round(n * 0.2)) if n >= 3 else 0
        for index, case in enumerate(group):
            if index < n_test:
                case["split"] = "test"
            elif index < n_test + n_val:
                case["split"] = "val"
            else:
                case["split"] = "train"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    rng = random.Random(_SEED)
    pools = _load_entity_pools()
    cases = _make_cases(rng, pools)
    _assign_splits(random.Random(_SEED + 1), cases)

    _OUTPUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")

    # Summary.
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
