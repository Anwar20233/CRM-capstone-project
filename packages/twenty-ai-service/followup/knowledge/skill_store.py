"""Read company-editable agent knowledge from Twenty's ``core.skill`` table.

Twenty already ships a first-class *Skill* concept: a workspace-scoped row with
``name`` / ``label`` / ``content`` (markdown), edited through the Skills UI and
created via the ``createSkill`` metadata mutation (see ``scripts/provision_skills.py``).
We store the Follow-Up planning knowledge and email/proposal templates as skills
under a stable naming convention, and read their ``content`` here.

Reads only — the agents never write skills. ``core.skill`` is the source of
truth (the metadata API keeps a read cache on top of it, but committed rows are
visible here immediately). Any failure (DB down, table missing, no row) returns
``None``/empty so callers transparently fall back to the bundled markdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import Awaitable, Optional, TypeVar

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgres://postgres:postgres@localhost:5432/default"

# ---------------------------------------------------------------------------
# Naming convention — the single contract shared with provision_skills.py.
# A skill name encodes its category (and key) so we can fetch by exact name or
# enumerate a category by prefix without a dedicated category column.
# ---------------------------------------------------------------------------

# General planning skills the user authors ("tips for planning any situation").
PLANNER_PREFIX = "followup-planner-"
# Seeded defaults the planner also discovers (stage playbooks + frameworks).
PLAYBOOK_PREFIX = "followup-playbook-"
EMAIL_TEMPLATE_PREFIX = "followup-email-template-"
PROPOSAL_TEMPLATE_PREFIX = "followup-proposal-template-"
# Emailer catalogs the drafter retrieves to ground proposals in real offerings.
PRODUCT_CATALOG_PREFIX = "followup-product-catalog-"
SERVICE_CATALOG_PREFIX = "followup-service-catalog-"
BANT_SKILL_NAME = "followup-bant"
BEST_PRACTICES_SKILL_NAME = "followup-best-practices"

# Every name the next-step planner discovers as a planning skill. The agent
# lists these at run time and loads the ones relevant to the deal — it does NOT
# assume a skill exists for the exact pipeline-stage name.
PLANNER_DB_PREFIXES = (
    PLANNER_PREFIX,
    PLAYBOOK_PREFIX,
    BANT_SKILL_NAME,
    BEST_PRACTICES_SKILL_NAME,
)


def playbook_skill_name(stage: str) -> str:
    return f"{PLAYBOOK_PREFIX}{stage.strip().lower()}"


def email_template_skill_name(key: str) -> str:
    return f"{EMAIL_TEMPLATE_PREFIX}{key.strip().lower()}"


def proposal_template_skill_name(key: str) -> str:
    return f"{PROPOSAL_TEMPLATE_PREFIX}{key.strip().lower()}"


def product_catalog_skill_name(key: str) -> str:
    return f"{PRODUCT_CATALOG_PREFIX}{key.strip().lower()}"


def service_catalog_skill_name(key: str) -> str:
    return f"{SERVICE_CATALOG_PREFIX}{key.strip().lower()}"


@dataclass(frozen=True)
class SkillRow:
    name: str
    label: str
    content: str
    description: str | None = None

    @property
    def key(self) -> str:
        """The trailing segment after the category prefix (e.g. ``discovery``)."""
        for prefix in (
            PLAYBOOK_PREFIX,
            EMAIL_TEMPLATE_PREFIX,
            PROPOSAL_TEMPLATE_PREFIX,
            PRODUCT_CATALOG_PREFIX,
            SERVICE_CATALOG_PREFIX,
        ):
            if self.name.startswith(prefix):
                return self.name[len(prefix):]
        return self.name


def _dsn(dsn: Optional[str] = None) -> str:
    return dsn or os.environ.get("PG_DATABASE_URL", DEFAULT_DSN)


# ---------------------------------------------------------------------------
# Async readers
# ---------------------------------------------------------------------------


async def fetch_skill_content(name: str, *, dsn: Optional[str] = None) -> Optional[str]:
    """Return the markdown content of the active skill named ``name``, or None.

    Never raises: a missing table, unreachable DB, or absent row all yield None
    so the caller falls back to the bundled markdown.
    """
    try:
        conn = await asyncpg.connect(_dsn(dsn))
    except Exception as err:  # noqa: BLE001
        logger.debug("skill_store: could not connect (%s); falling back to files", err)
        return None
    try:
        return await conn.fetchval(
            'SELECT content FROM core.skill '
            'WHERE name = $1 AND "isActive" = true '
            'ORDER BY "createdAt" LIMIT 1',
            name,
        )
    except Exception as err:  # noqa: BLE001
        logger.debug("skill_store: read failed for %s (%s)", name, err)
        return None
    finally:
        await conn.close()


async def fetch_skills_by_prefix(
    prefix: str, *, dsn: Optional[str] = None
) -> list[SkillRow]:
    """Return all active skills whose name starts with ``prefix`` (empty on error)."""
    try:
        conn = await asyncpg.connect(_dsn(dsn))
    except Exception as err:  # noqa: BLE001
        logger.debug("skill_store: could not connect (%s); falling back to files", err)
        return []
    try:
        rows = await conn.fetch(
            'SELECT name, label, description, content FROM core.skill '
            'WHERE name LIKE $1 AND "isActive" = true '
            'ORDER BY name',
            f"{prefix}%",
        )
        return [
            SkillRow(
                name=r["name"],
                label=r["label"],
                content=r["content"],
                description=r["description"],
            )
            for r in rows
        ]
    except Exception as err:  # noqa: BLE001
        logger.debug("skill_store: prefix read failed for %s (%s)", prefix, err)
        return []
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Sync wrappers — the next-step planner's @tool functions are synchronous AND
# run inside the planner's running event loop, so we execute the coroutine on a
# dedicated thread with its own loop. (When there's no running loop we just run
# it directly.)
# ---------------------------------------------------------------------------

T = TypeVar("T")


def _run_sync(coro: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]

    box: dict[str, object] = {}

    def _runner() -> None:
        box["value"] = asyncio.run(coro)  # type: ignore[arg-type]

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    return box["value"]  # type: ignore[return-value]


def get_skill_content(name: str) -> Optional[str]:
    """Synchronous ``fetch_skill_content`` for use inside sync LangChain tools."""
    return _run_sync(fetch_skill_content(name))


def list_skill_keys_by_prefix(prefix: str) -> list[str]:
    """Synchronous list of ``SkillRow.key`` for skills under ``prefix``."""
    return [row.key for row in _run_sync(fetch_skills_by_prefix(prefix))]
