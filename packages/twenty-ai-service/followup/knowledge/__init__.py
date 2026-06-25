"""Shared, DB-backed knowledge store for the Follow-Up agents.

The next-step planner and the email drafter no longer learn solely from the
markdown files bundled in the repo: a company can edit how planning works and
teach the drafter its house style through Twenty's **Skills** UI. Those edits
live in Twenty's ``core.skill`` table; this package reads them at run time and
falls back to the bundled markdown when a skill is absent (un-provisioned envs
and tests keep working unchanged).
"""

from followup.knowledge.skill_store import (
    BANT_SKILL_NAME,
    BEST_PRACTICES_SKILL_NAME,
    EMAIL_TEMPLATE_PREFIX,
    PLANNER_DB_PREFIXES,
    PLANNER_PREFIX,
    PLAYBOOK_PREFIX,
    PROPOSAL_TEMPLATE_PREFIX,
    SkillRow,
    email_template_skill_name,
    fetch_skill_content,
    fetch_skills_by_prefix,
    get_skill_content,
    list_skill_keys_by_prefix,
    playbook_skill_name,
    proposal_template_skill_name,
)

__all__ = [
    "BANT_SKILL_NAME",
    "BEST_PRACTICES_SKILL_NAME",
    "EMAIL_TEMPLATE_PREFIX",
    "PLANNER_DB_PREFIXES",
    "PLANNER_PREFIX",
    "PLAYBOOK_PREFIX",
    "PROPOSAL_TEMPLATE_PREFIX",
    "SkillRow",
    "email_template_skill_name",
    "fetch_skill_content",
    "fetch_skills_by_prefix",
    "get_skill_content",
    "list_skill_keys_by_prefix",
    "playbook_skill_name",
    "proposal_template_skill_name",
]
