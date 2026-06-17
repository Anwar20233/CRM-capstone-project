"""FastAPI dependency injection for the Follow-Up REST API.

Provides a shared ``Database`` pool on app lifespan, a cached
``OrchestratorDeps`` instance, and the compiled follow-up / accept graphs.
Endpoints inject these via ``Depends(get_deps)`` / ``Depends(get_followup_graph)``
/ ``Depends(get_accept_graph)``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.graph import build_followup_graph
from followup.store.repositories import Database

logger = logging.getLogger(__name__)

# Module-level singletons — initialized by the lifespan context.
_db: Optional[Database] = None
_deps: Optional[OrchestratorDeps] = None
_followup_graph: Any = None
_accept_graph: Any = None


async def startup() -> None:
    """Initialize the shared database pool and compile graphs.

    Called from the FastAPI lifespan handler in ``main.py``.
    """
    global _db, _deps, _followup_graph, _accept_graph

    _db = await Database.connect()
    await _db.apply_migrations()

    _deps = OrchestratorDeps.create(_db)

    _followup_graph = build_followup_graph(_deps)

    from followup.orchestrator.graph import build_accept_graph

    _accept_graph = build_accept_graph(_deps)

    logger.info("Follow-up API dependencies initialized")


async def shutdown() -> None:
    """Tear down the database pool."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def get_db() -> Database:
    """FastAPI dependency: the shared database pool."""
    if _db is None:
        raise RuntimeError("Follow-up DB pool not initialized — call startup() first")
    return _db


def get_deps() -> OrchestratorDeps:
    """FastAPI dependency: the orchestrator dependency bundle."""
    if _deps is None:
        raise RuntimeError("Follow-up deps not initialized — call startup() first")
    return _deps


def get_followup_graph() -> Any:
    """FastAPI dependency: the compiled follow-up StateGraph."""
    if _followup_graph is None:
        raise RuntimeError("Follow-up graph not compiled — call startup() first")
    return _followup_graph


def get_accept_graph() -> Any:
    """FastAPI dependency: the compiled accept StateGraph."""
    if _accept_graph is None:
        raise RuntimeError("Accept graph not compiled — call startup() first")
    return _accept_graph


__all__ = [
    "startup",
    "shutdown",
    "get_db",
    "get_deps",
    "get_followup_graph",
    "get_accept_graph",
]
