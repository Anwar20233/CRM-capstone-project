"""LangSmith tracing configuration for the entire twenty-ai-service platform.

Activates automatically when ``LANGSMITH_API_KEY`` is present in the
environment. All three tracing surfaces are covered:

1. **LangGraph / LangChain** (extraction pipeline, followup orchestrator,
   writer graph) — auto-traced when ``LANGCHAIN_TRACING_V2=true`` (set here).
2. **Raw OpenAI SDK calls** (``BaseWorker.run()``, ``writer_graph.llm_node``)
   — traced via ``langsmith.wrappers.wrap_openai``.
3. **Custom spans** — key orchestration functions decorated with
   ``@traceable`` for a clean span hierarchy in the LangSmith dashboard.

Usage::

    # At app startup (main.py lifespan or top of a script):
    from tracing import configure_tracing
    configure_tracing()

    # To wrap a raw OpenAI client:
    from tracing import wrap_client
    client = wrap_client(OpenAI(...))

    # To add custom spans:
    from tracing import traceable
    @traceable(name="my_function", run_type="chain")
    async def my_function(...): ...
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CONFIGURED = False


def configure_tracing(
    *,
    project_name: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Set LangSmith env vars and return True if tracing is active.

    Safe to call multiple times — only the first call configures.

    Parameters
    ----------
    project_name:
        Override the ``LANGCHAIN_PROJECT`` env var.  Defaults to
        ``"twenty-ai-service"``.
    enabled:
        Force tracing on/off.  ``None`` (default) → auto-detect from
        ``LANGSMITH_API_KEY`` presence.
    """
    global _CONFIGURED  # noqa: PLW0603
    if _CONFIGURED:
        return os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"

    api_key = os.environ.get("LANGSMITH_API_KEY", "")
    if enabled is None:
        enabled = bool(api_key)

    if not enabled:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
        logger.info("LangSmith tracing DISABLED (no LANGSMITH_API_KEY)")
        _CONFIGURED = True
        return False

    # LangSmith / LangChain env vars ── one canonical source of truth.
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    os.environ.setdefault("LANGCHAIN_PROJECT", project_name or "twenty-ai-service")
    # LangSmith SDK reads both LANGSMITH_API_KEY and LANGCHAIN_API_KEY;
    # mirror so both work.
    if api_key:
        os.environ.setdefault("LANGCHAIN_API_KEY", api_key)

    logger.info(
        "LangSmith tracing ENABLED → project=%s endpoint=%s",
        os.environ.get("LANGCHAIN_PROJECT"),
        os.environ.get("LANGCHAIN_ENDPOINT"),
    )
    _CONFIGURED = True
    return True


def wrap_client(client: Any) -> Any:
    """Wrap a raw ``openai.OpenAI`` client for LangSmith auto-tracing.

    Returns the client unchanged when tracing is disabled or langsmith is not
    installed, so call sites are always safe.
    """
    if os.environ.get("LANGCHAIN_TRACING_V2", "").lower() != "true":
        return client
    try:
        from langsmith.wrappers import wrap_openai
        return wrap_openai(client)
    except Exception:  # noqa: BLE001
        logger.debug("langsmith.wrappers.wrap_openai unavailable; skipping")
        return client


def get_traceable():
    """Return the ``@traceable`` decorator, or a no-op passthrough.

    This avoids a hard dependency on langsmith for environments where tracing
    is disabled.
    """
    if os.environ.get("LANGCHAIN_TRACING_V2", "").lower() != "true":
        def _noop_decorator(*args: Any, **kwargs: Any):
            """No-op: tracing disabled."""
            if args and callable(args[0]):
                return args[0]
            def wrapper(fn: Any) -> Any:
                return fn
            return wrapper
        return _noop_decorator
    try:
        from langsmith import traceable as _traceable
        return _traceable
    except ImportError:
        def _noop_decorator(*args: Any, **kwargs: Any):
            if args and callable(args[0]):
                return args[0]
            def wrapper(fn: Any) -> Any:
                return fn
            return wrapper
        return _noop_decorator


# Convenience: importable decorator that is always safe.
traceable = get_traceable()
