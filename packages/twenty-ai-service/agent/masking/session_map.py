"""PIISessionMap — the transient token ↔ raw-value mapping for one session.

This is the single light object behind the "Translate-on-Storage" masking
strategy (Option C in the PII architecture plan).  It holds a bidirectional,
session-scoped dictionary:

    raw value  ──►  token     "John Doe"   ──►  "[PERSON_1]"
    token      ──►  raw value "[PERSON_1]" ──►  "John Doe"

Tokens are sequential and human-readable (``[PERSON_1]``, ``[COMPANY_2]``) so
the LLM context stays clean.  The map is **never persisted** — it is rebuilt on
demand from the chat's own stored history (which Twenty already keeps unmasked,
with real values).  When a chat is reopened, ``prime`` runs one NER pass over
that history to reconstruct the exact same tokens it had before, then new
messages reuse the in-memory map.  Determinism (a deterministic extractor +
registration in order of first appearance) is what guarantees ``Joe`` comes
back as ``[PERSON_1]`` every time, with no token table to store or to keep in
sync.

Two directions, four entry points:

- ``mask_text`` / ``mask_value`` — inbound: discover PII (via the NER pipeline)
  and replace raw values with tokens before anything reaches the LLM.  Used for
  the user prompt, tool results, and recalled memories.
- ``unmask_text`` / ``unmask_value`` — outbound: replace tokens with their raw
  values before anything leaves the LLM boundary.  Used for tool-call arguments
  (so the real CRM is hit) and the final response shown to the user.

Design goals: **light** (one NER pass per masked payload, plain-string
replacement for everything already known) and **decoupled** (the NER extractor
is injected, so this class has no hard dependency on GLiNER and is trivial to
test with a stub).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

# An extractor maps raw text to a list of entity dicts: {label, text, ...}.
# This is exactly the shape ``pipelines.extract`` returns.
Extractor = Callable[[str], list[dict[str, Any]]]

# Matches any token we emit, e.g. "[PERSON_1]", "[EMAIL_12]".
_TOKEN_RE = re.compile(r"\[[A-Z]+_\d+\]")

# NER label → token prefix.  Only labels listed here are treated as PII and
# masked; everything else (date, money, job title, product, …) is left visible
# so the agent can still reason about deals and timelines.
DEFAULT_MASKABLE_LABELS: dict[str, str] = {
    "person": "PERSON",
    "company": "COMPANY",
    "email address": "EMAIL",
    "phone number": "PHONE",
    "location": "LOCATION",
}


def _default_extractor(text: str) -> list[dict[str, Any]]:
    """Run the shared GLiNER + regex pipeline, degrading gracefully.

    If the models have not been loaded (e.g. a unit-test process that never
    starts the service), we skip NER rather than raise — known values are still
    masked by the plain-string pass, the architecture just can't discover new
    entities until the models are up.
    """
    from pipelines import extract, models_loaded

    if not models_loaded():
        return []
    return extract(text)


class PIISessionMap:
    """Bidirectional, session-scoped map between raw PII and sequential tokens.

    Parameters
    ----------
    extractor:
        Callable that returns NER entities for a string.  Defaults to the
        shared ``pipelines.extract`` pipeline (lazy-imported).  Inject a stub
        in tests to avoid loading models.
    maskable_labels:
        NER label → token prefix mapping.  Defaults to
        ``DEFAULT_MASKABLE_LABELS``.
    """

    def __init__(
        self,
        *,
        extractor: Extractor | None = None,
        maskable_labels: dict[str, str] | None = None,
    ) -> None:
        self._extractor = extractor or _default_extractor
        self._maskable_labels = maskable_labels or dict(DEFAULT_MASKABLE_LABELS)

        self._forward: dict[str, str] = {}  # raw value → token
        self._reverse: dict[str, str] = {}  # token → raw value
        self._counters: dict[str, int] = {}  # token prefix → next index

    # -- Introspection ---------------------------------------------------

    @property
    def mapping(self) -> dict[str, str]:
        """A copy of the token → raw-value mapping (read-only)."""
        return dict(self._reverse)

    def __len__(self) -> int:
        return len(self._reverse)

    # -- Priming (rebuild the map from stored chat history) -------------

    def prime(self, texts: Iterable[str]) -> None:
        """Rebuild the map from a chat's stored (unmasked) message history.

        Runs a single NER pass over the whole history so a reopened chat
        recovers the same tokens it had before — nothing needs to be persisted,
        the mapping is derived from the messages themselves. Call once when a
        chat is loaded, before handling new messages.
        """
        blob = "\n".join(text for text in texts if isinstance(text, str) and text)
        if blob.strip():
            self._discover(blob)

    # -- Registration ----------------------------------------------------

    def register(self, label: str, raw_value: str) -> str | None:
        """Get-or-create the token for a raw value of a given NER label.

        Returns the token, or ``None`` if the label is not maskable or the
        value is empty.  Registration is idempotent — the same raw value always
        maps to the same token within a session.
        """
        prefix = self._maskable_labels.get(label)
        if prefix is None:
            return None

        raw_value = raw_value.strip()
        if not raw_value:
            return None

        existing = self._forward.get(raw_value)
        if existing is not None:
            return existing

        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        token = f"[{prefix}_{self._counters[prefix]}]"
        self._forward[raw_value] = token
        self._reverse[token] = raw_value
        return token

    # -- Masking (inbound: raw → token) ---------------------------------

    def mask_text(self, text: str) -> str:
        """Mask a single string: discover new PII, then apply all known tokens."""
        if not isinstance(text, str) or not text:
            return text
        self._discover(text)
        return self._apply_forward(text)

    def mask_value(self, value: Any) -> Any:
        """Mask every string leaf inside a nested dict/list (e.g. a tool result).

        Runs the NER pipeline **once** over all string content for efficiency,
        then rewrites each leaf with plain-string replacement.
        """
        blob = "\n".join(self._iter_strings(value))
        if blob.strip():
            self._discover(blob)
        return self._map_strings(value, self._apply_forward)

    # -- Unmasking (outbound: token → raw) ------------------------------

    def unmask_text(self, text: str) -> str:
        """Replace every ``[PREFIX_N]`` token with its raw value.

        Unknown tokens are left untouched (the LLM may invent a token that was
        never registered — we never want to leak or corrupt such text).
        """
        if not isinstance(text, str) or "[" not in text:
            return text
        return _TOKEN_RE.sub(
            lambda match: self._reverse.get(match.group(0), match.group(0)),
            text,
        )

    def unmask_value(self, value: Any) -> Any:
        """Unmask every string leaf inside a nested dict/list (e.g. tool args)."""
        return self._map_strings(value, self.unmask_text)

    # -- Internals -------------------------------------------------------

    def _discover(self, text: str) -> None:
        """Run NER over text and register any maskable entities it finds.

        Entities are registered in order of first appearance so token numbers
        are stable and intuitive (``[PERSON_1]`` is the first person mentioned)
        and identical every time a chat is rebuilt from its stored history.
        """
        entities = list(self._extractor(text))
        entities.sort(
            key=lambda entity: entity["start"]
            if entity.get("start") is not None
            else len(text)
        )
        for entity in entities:
            self.register(entity.get("label", ""), entity.get("text", ""))

    def _apply_forward(self, text: str) -> str:
        """Replace all known raw values with their tokens (longest first).

        Longest-first ordering prevents a shorter value (``"Acme"``) from
        corrupting a longer one (``"Acme Corp"``) that contains it.
        """
        if not text:
            return text
        for raw_value in sorted(self._forward, key=len, reverse=True):
            if raw_value in text:
                text = text.replace(raw_value, self._forward[raw_value])
        return text

    @classmethod
    def _iter_strings(cls, value: Any) -> Iterator[str]:
        """Yield every string leaf in a nested dict/list structure."""
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from cls._iter_strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_strings(item)

    @classmethod
    def _map_strings(cls, value: Any, transform: Callable[[str], str]) -> Any:
        """Return a copy of ``value`` with ``transform`` applied to each string leaf."""
        if isinstance(value, str):
            return transform(value)
        if isinstance(value, dict):
            return {key: cls._map_strings(item, transform) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._map_strings(item, transform) for item in value]
        return value
