"""Session state store for the CRM agent layer.

Keeps per-session state needed by the Orchestrator, Writer, and security layer:

    topic          — the current conversation topic/intent (set by orchestrator)
    write_log      — append-only audit log of every write operation
    duplicate check — compare an intended write against the log to detect repeats

Design
~~~~~~
In-memory for development (the ``SessionStore`` class is a plain dict wrapper).
Redis-ready: set ``REDIS_URL`` in the environment and the store transparently
switches to a Redis backend (``aioredis`` / ``redis.asyncio``).  The same async
API works either way — callers do not need to change.

The public module-level functions (``session_set_topic``, ``session_log_write``,
…) delegate to a module-level ``_store`` singleton.  Workers and tests import
the functions directly; the FastAPI router (``agent.session.endpoints``) also
uses the functions so the HTTP layer and the Python layer share one store.

Write log entry schema::

    {
        "id":         str,        # unique entry id (uuid4)
        "ts":         str,        # ISO-8601 UTC timestamp
        "tool":       str,        # bridge tool name, e.g. "create_person"
        "args":       dict,       # arguments passed to the tool
        "old_value":  Any | None, # value before the write (None if not captured)
        "new_value":  Any | None, # value after the write (result from bridge)
        "session_id": str,
    }

Duplicate check
~~~~~~~~~~~~~~~
``session_check_duplicate(session_id, tool, args)`` scans the write log for a
recent entry (within ``_DUP_WINDOW_SECONDS``) with the same ``tool`` and
structurally identical ``args`` (deep-equality after normalising key order).  If
found, it returns ``{ ok: True, data: { duplicate: True, entry: {...} } }``; if
not found, ``{ ok: True, data: { duplicate: False } }``.

The orchestrator calls this before handing a write instruction to the writer
so the same mutation doesn't fire twice during retries or ambiguous user
re-confirmations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# How far back (in seconds) to scan for duplicate writes.
_DUP_WINDOW_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------

class _InMemorySessionData:
    """State for a single session."""
    __slots__ = ("topic", "write_log")

    def __init__(self) -> None:
        self.topic: str | None = None
        self.write_log: list[dict] = []


class SessionStore:
    """Thread/coroutine-safe in-memory session store.

    All mutating operations acquire ``_lock`` so the store is safe under
    concurrent asyncio tasks (the writer and reader workers may run in the same
    event loop).

    Redis integration: if ``REDIS_URL`` is set the store uses Redis via
    ``redis.asyncio`` (install with ``pip install redis[asyncio]``).  The Redis
    backend encodes all values as JSON and uses a ``crm:session:{id}:*`` key
    namespace with a 24-hour TTL.
    """

    def __init__(self) -> None:
        self._data: dict[str, _InMemorySessionData] = defaultdict(_InMemorySessionData)
        self._lock = asyncio.Lock()
        self._redis_url: str | None = os.environ.get("REDIS_URL")
        self._redis: Any = None  # lazy redis.asyncio.Redis client

    # -- Redis bootstrap ------------------------------------------------

    async def _get_redis(self):
        """Lazy-init Redis client.  Returns None if redis is not installed/configured."""
        if not self._redis_url:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis  # type: ignore[import]
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            return self._redis
        except Exception:
            # Fall back to in-memory silently.
            self._redis_url = None
            return None

    # -- Helpers --------------------------------------------------------

    def _session(self, session_id: str) -> _InMemorySessionData:
        return self._data[session_id]

    async def _redis_get(self, session_id: str) -> dict | None:
        r = await self._get_redis()
        if r is None:
            return None
        raw = await r.get(f"crm:session:{session_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def _redis_set(self, session_id: str, data: dict) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.set(f"crm:session:{session_id}", json.dumps(data), ex=86400)

    # -- Topic ----------------------------------------------------------

    async def set_topic(self, session_id: str, topic: str) -> dict:
        """Set the current conversation topic for a session."""
        async with self._lock:
            redis_data = await self._redis_get(session_id)
            if redis_data is not None:
                redis_data["topic"] = topic
                await self._redis_set(session_id, redis_data)
            else:
                self._session(session_id).topic = topic
        return {"ok": True, "data": {"session_id": session_id, "topic": topic}}

    async def get_topic(self, session_id: str) -> dict:
        """Return the current topic for a session (None if not set)."""
        redis_data = await self._redis_get(session_id)
        if redis_data is not None:
            topic = redis_data.get("topic")
        else:
            topic = self._session(session_id).topic
        return {"ok": True, "data": {"session_id": session_id, "topic": topic}}

    # -- Write log ------------------------------------------------------

    async def log_write(
        self,
        session_id: str,
        tool: str,
        args: dict,
        old_value: Any = None,
        new_value: Any = None,
    ) -> dict:
        """Append a write operation to the session's audit log.

        Returns the created log entry (with id and timestamp).
        """
        entry = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": args,
            "old_value": old_value,
            "new_value": new_value,
            "session_id": session_id,
        }

        async with self._lock:
            redis_data = await self._redis_get(session_id)
            if redis_data is not None:
                redis_data.setdefault("write_log", []).append(entry)
                await self._redis_set(session_id, redis_data)
            else:
                self._session(session_id).write_log.append(entry)

        return {"ok": True, "data": {"entry": entry}}

    async def get_write_log(self, session_id: str) -> dict:
        """Return the full write log for a session."""
        redis_data = await self._redis_get(session_id)
        if redis_data is not None:
            log = redis_data.get("write_log", [])
        else:
            log = list(self._session(session_id).write_log)
        return {"ok": True, "data": {"session_id": session_id, "entries": log, "count": len(log)}}

    # -- Duplicate detection -------------------------------------------

    @staticmethod
    def _args_hash(args: dict) -> str:
        """Stable hash of an args dict for quick duplicate comparison."""
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def check_duplicate(self, session_id: str, tool: str, args: dict) -> dict:
        """Check whether a write with the same tool + args was recently logged.

        Scans the last ``_DUP_WINDOW_SECONDS`` of the write log.  Returns::

            { ok: True, data: { duplicate: True,  entry: {...} } }
            { ok: True, data: { duplicate: False } }

        The orchestrator calls this before handing a write to the WriterWorker.
        """
        target_hash = self._args_hash(args)
        cutoff = datetime.now(timezone.utc).timestamp() - _DUP_WINDOW_SECONDS

        redis_data = await self._redis_get(session_id)
        log: list[dict] = redis_data.get("write_log", []) if redis_data is not None else list(self._session(session_id).write_log)

        for entry in reversed(log):
            # Parse timestamp
            try:
                entry_ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError):
                continue
            if entry_ts < cutoff:
                break  # log is append-only, so everything before this is too old

            if entry.get("tool") == tool and self._args_hash(entry.get("args", {})) == target_hash:
                return {"ok": True, "data": {"duplicate": True, "entry": entry}}

        return {"ok": True, "data": {"duplicate": False}}

    # -- Clear ----------------------------------------------------------

    async def clear(self, session_id: str) -> dict:
        """Wipe all state for a session (topic + write log)."""
        async with self._lock:
            redis_data = await self._redis_get(session_id)
            if redis_data is not None:
                r = await self._get_redis()
                if r:
                    await r.delete(f"crm:session:{session_id}")
            elif session_id in self._data:
                del self._data[session_id]
        return {"ok": True, "data": {"session_id": session_id, "cleared": True}}


# ---------------------------------------------------------------------------
# Module-level singleton + convenience wrappers
# ---------------------------------------------------------------------------

_store = SessionStore()


async def session_set_topic(session_id: str, topic: str) -> dict:
    """Set the conversation topic for *session_id*."""
    return await _store.set_topic(session_id, topic)


async def session_get_topic(session_id: str) -> dict:
    """Get the conversation topic for *session_id*."""
    return await _store.get_topic(session_id)


async def session_log_write(
    session_id: str,
    tool: str,
    args: dict,
    old_value: Any = None,
    new_value: Any = None,
) -> dict:
    """Append a write entry to the session log.

    Call after every successful bridge write so the orchestrator's duplicate
    checker and conflict resolver have a full audit trail.
    """
    return await _store.log_write(session_id, tool, args, old_value, new_value)


async def session_get_write_log(session_id: str) -> dict:
    """Return the full ordered write log for *session_id*."""
    return await _store.get_write_log(session_id)


async def session_check_duplicate(session_id: str, tool: str, args: dict) -> dict:
    """Return whether a matching write was already logged in the last 5 minutes."""
    return await _store.check_duplicate(session_id, tool, args)


async def session_clear(session_id: str) -> dict:
    """Wipe all session state (topic + log) for *session_id*."""
    return await _store.clear(session_id)
