"""Seed the Follow-Up agent's knowledge into Twenty's editable **Skills**.

The next-step planner and email drafter read their knowledge from Twenty's
``core.skill`` table (see ``followup/knowledge/skill_store.py``), falling back to
the markdown bundled in the repo. This script ports those bundled defaults into
editable skill records so a company can tune how planning works and teach the
drafter its house style through the Skills UI.

It creates one skill per knowledge file under a stable naming convention
(``followup-playbook-*``, ``followup-bant``, ``followup-best-practices``,
``followup-email-template-*``, ``followup-proposal-template-*``) and is
idempotent: skills that already exist (by name) are left untouched.

Skills are workspace metadata created through the guarded ``createSkill``
mutation, so this goes through Twenty's metadata GraphQL API (not raw SQL —
that would bypass the migration + flat-entity cache the API maintains).

Usage:
    TWENTY_API_KEY=<key> [TWENTY_BASE_URL=http://localhost:3000] \\
        python scripts/provision_skills.py

Create the API key in Twenty under Settings -> APIs & Webhooks; it must belong to
a role with the "AI" settings permission (e.g. Admin).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

# Allow ``python scripts/provision_skills.py`` from the package root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from followup.knowledge import skill_store  # noqa: E402

_FOLLOWUP = Path(__file__).resolve().parent.parent / "followup"
_NEXT_STEP_KNOWLEDGE = _FOLLOWUP / "next_step" / "knowledge"
_EMAILER_KNOWLEDGE = _FOLLOWUP / "emailer" / "knowledge"


@dataclass(frozen=True)
class SkillDef:
    name: str
    label: str
    description: str
    icon: str
    content: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_skill_defs() -> list[SkillDef]:
    """Map every bundled knowledge file onto a SkillDef (skipping empty files)."""
    defs: list[SkillDef] = []

    # Next-step planning playbooks (one per pipeline stage).
    playbooks_dir = _NEXT_STEP_KNOWLEDGE / "playbooks"
    for path in sorted(playbooks_dir.glob("*.md")):
        stage = path.stem
        defs.append(
            SkillDef(
                name=skill_store.playbook_skill_name(stage),
                label=f"Follow-Up Playbook: {stage}",
                description=f"How the agent plans next steps during the {stage} stage.",
                icon="IconBook",
                content=_read(path),
            )
        )

    # BANT qualification framework.
    bant_path = _NEXT_STEP_KNOWLEDGE / "bant.md"
    if bant_path.exists():
        defs.append(
            SkillDef(
                name=skill_store.BANT_SKILL_NAME,
                label="Follow-Up: BANT Qualification",
                description="Budget/Authority/Need/Timeline gaps the planner reasons from.",
                icon="IconChecklist",
                content=_read(bant_path),
            )
        )

    # General sales best practices.
    bp_path = _NEXT_STEP_KNOWLEDGE / "best_practices.md"
    if bp_path.exists():
        defs.append(
            SkillDef(
                name=skill_store.BEST_PRACTICES_SKILL_NAME,
                label="Follow-Up: Sales Best Practices",
                description="Engagement cadence, multi-threading and task-hygiene guidance.",
                icon="IconBulb",
                content=_read(bp_path),
            )
        )

    # Email drafter templates (house style per draft type).
    for path in sorted((_EMAILER_KNOWLEDGE / "email_templates").glob("*.md")):
        key = path.stem
        defs.append(
            SkillDef(
                name=skill_store.email_template_skill_name(key),
                label=f"Follow-Up Email Template: {key}",
                description=f"Email drafter style/template for '{key}'.",
                icon="IconMail",
                content=_read(path),
            )
        )

    # Proposal drafter templates.
    for path in sorted((_EMAILER_KNOWLEDGE / "proposal_templates").glob("*.md")):
        key = path.stem
        defs.append(
            SkillDef(
                name=skill_store.proposal_template_skill_name(key),
                label=f"Follow-Up Proposal Template: {key}",
                description=f"Proposal drafter style/template for '{key}'.",
                icon="IconFileText",
                content=_read(path),
            )
        )

    return defs


_SKILLS_QUERY = "query Skills { skills { name } }"
_CREATE_SKILL = """
mutation CreateSkill($input: CreateSkillInput!) {
  createSkill(input: $input) { id name }
}
"""


def _graphql(client: httpx.Client, url: str, query: str, variables: dict | None = None) -> dict:
    response = client.post(url, json={"query": query, "variables": variables or {}})
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL error: {body['errors']}")
    return body["data"]


def main() -> int:
    api_key = os.environ.get("TWENTY_API_KEY")
    if not api_key:
        print("ERROR: set TWENTY_API_KEY (Settings -> APIs & Webhooks, Admin role).")
        return 1

    base_url = os.environ.get("TWENTY_BASE_URL", "http://localhost:3000").rstrip("/")
    metadata_url = f"{base_url}/metadata"

    defs = build_skill_defs()
    print(f"Built {len(defs)} skill definition(s) from bundled knowledge.")

    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        existing = {s["name"] for s in _graphql(client, metadata_url, _SKILLS_QUERY)["skills"]}

        created = 0
        for skill in defs:
            if skill.name in existing:
                print(f"  skip (exists): {skill.name}")
                continue
            _graphql(
                client,
                metadata_url,
                _CREATE_SKILL,
                {
                    "input": {
                        "name": skill.name,
                        "label": skill.label,
                        "description": skill.description,
                        "icon": skill.icon,
                        "content": skill.content,
                    }
                },
            )
            print(f"  created: {skill.name}")
            created += 1

    print(f"\nDone. Created {created} new skill(s); {len(defs) - created} already existed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
