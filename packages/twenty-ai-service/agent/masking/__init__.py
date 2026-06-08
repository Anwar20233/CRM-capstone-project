"""PII masking layer — transient token ↔ raw-value translation for agents.

The masking layer is a thin, framework-agnostic hook that any agent loop wraps
around its LLM boundary:

- inbound (user prompt, tool results, recalled memory) is **masked** — PII is
  replaced with sequential tokens before the model sees it;
- outbound (tool-call arguments, the final answer, memory to be stored) is
  **unmasked** — tokens are translated back to real values.

The whole state for a session is a single ``PIISessionMap``.
"""

from agent.masking.session_map import (
    DEFAULT_MASKABLE_LABELS,
    PIISessionMap,
)

__all__ = ["PIISessionMap", "DEFAULT_MASKABLE_LABELS"]
