"""PII masking layer — entity → structured-handle translation for agents.

The masking layer is a thin hook any agent loop wraps around its LLM boundary:

- inbound (user prompt, tool results, recalled memory) is **masked** — real
  entities are replaced with structured handles (``person001``, ``company002``)
  before the model sees them;
- outbound (tool-call arguments, the final answer) is **unmasked** — handle
  references (including dotted fields like ``person001.id``) are translated back
  to real values.

The whole state for a session is a single :class:`EntityHandleMap`. Resolving a
name to a CRM record (and disambiguating when there are several) is done by
:class:`CRMResolver`, driven by the orchestrator.
"""

from agent.masking.handle_map import (
    DEFAULT_LABEL_TO_TYPE,
    RESOLVABLE_TYPES,
    EntityHandleMap,
    Handle,
)
from agent.masking.resolver import CRMResolver, Resolution, build_bridge_search

__all__ = [
    "EntityHandleMap",
    "Handle",
    "DEFAULT_LABEL_TO_TYPE",
    "RESOLVABLE_TYPES",
    "CRMResolver",
    "Resolution",
    "build_bridge_search",
]
