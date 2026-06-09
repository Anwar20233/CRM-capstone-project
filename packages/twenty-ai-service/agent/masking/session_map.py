"""PIISessionMap — the transient token ↔ raw-value mapping for one session.

This is the single light object behind the "Translate-on-Storage" masking
strategy (Option C in the PII architecture plan).  It holds a bidirectional,
session-scoped mapping between real entity values and sequential tokens:

    raw value  ──►  token     "John Doe"   ──►  "[PERSON_1]"
    token      ──►  raw value "[PERSON_1]" ──►  "John Doe"

Tokens are sequential and human-readable (``[PERSON_1]``, ``[COMPANY_2]``) so
the LLM context stays clean.  The map is **never persisted** — it is rebuilt on
demand from the chat's own stored history (which Twenty already keeps unmasked,
with real values) via ``prime``.

Matching is robust, not literal:

- **Case / whitespace insensitive** — ``John``, ``john`` and ``John`` all map to
  the same token.
- **Partial / variant aware** — once ``John Doe`` is ``[PERSON_1]``, a later
  bare ``John`` resolves to ``[PERSON_1]`` too… *unless it is ambiguous*. If a
  second John (``John Smith`` → ``[PERSON_2]``) exists, a bare ``John`` can't be
  attributed to either, so it gets its own fresh token rather than guessing.
  This applies to every entity type, not just names (``Acme`` ↔ ``Acme Corp``).
- **Faithful unmask** — a token always expands to its *fullest* known surface
  form, so masking and unmasking round-trip to a correct entity reference.

Design goals: **light** (one NER pass per masked payload; plain case-insensitive
replacement for everything already known) and **decoupled** (the NER extractor
is injected, so this class has no hard dependency on GLiNER and is trivial to
test with a stub).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

# An extractor maps raw text to a list of entity dicts: {label, text, ...}.
# This is exactly the shape ``pipelines.extract`` returns.
Extractor = Callable[[str], list[dict[str, Any]]]

# Matches any token we emit, e.g. "[PERSON_1]", "[EMAIL_12]".
_TOKEN_RE = re.compile(r"\[[A-Z]+_\d+\]")

# Splits a normalized string into alphanumeric words for subset matching.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Dropped from subset matching so corporate/grammatical filler never drives an
# alias (e.g. "Acme" ↔ "Acme Corp" should match on "acme", not on "corp").
_STOPWORDS = {
    "of", "the", "and", "for", "inc", "llc", "ltd", "co", "corp", "corporation",
    "company", "group", "plc", "gmbh", "sa", "sas", "ag", "holdings",
}

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

# Token prefixes that are proper names: their stored surface is given a leading
# capital per word so values typed in lower-case ("john doe", "acme corp") are
# read from and written back to the CRM with consistent casing. Emails and phone
# numbers are excluded — capitalizing them would corrupt the value.
_NAME_PREFIXES: frozenset[str] = frozenset({"PERSON", "COMPANY", "LOCATION"})


def _normalize(text: str) -> str:
    """Casefold + collapse whitespace so casing/spacing never splits a token."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _capitalize_name(text: str) -> str:
    """Give each word a leading capital, fixing only lower-case first letters.

    Single point of name-casing for the whole masking layer. Only a word's first
    letter is touched, and only when it is lower-case, so already-correct casing
    is preserved ("McDonald", "iPhone" stay intact). "john doe" → "John Doe",
    "acme corp" → "Acme Corp".
    """
    def fix_word(word: str) -> str:
        for index, char in enumerate(word):
            if char.isalpha():
                if char.islower():
                    return word[:index] + char.upper() + word[index + 1:]
                return word
            # Skip leading non-letters (quotes, digits) before the first letter.
        return word

    return " ".join(fix_word(word) for word in text.split())


def _significant_words(normalized: str) -> set[str]:
    """Words used for subset matching: alnum, length ≥ 2, minus stopwords."""
    return {
        word
        for word in _WORD_RE.findall(normalized)
        if len(word) >= 2 and word not in _STOPWORDS
    }


def _subset(words_a: set[str], words_b: set[str]) -> bool:
    """True when one significant-word set fully contains the other."""
    return bool(words_a) and bool(words_b) and (words_a <= words_b or words_b <= words_a)


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


@dataclass
class _Token:
    """Everything known about one masked entity within a session."""

    prefix: str
    canonical: str  # fullest surface form seen — what unmask expands to
    words: set[str] = field(default_factory=set)  # significant words (for aliasing)
    surfaces: set[str] = field(default_factory=set)  # normalized full surfaces seen


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

        self._tokens: dict[str, _Token] = {}  # token → info
        self._norm_index: dict[str, str] = {}  # normalized full surface → token
        self._all_surfaces: list[tuple[str, str]] = []  # (surface, token) for replacement
        self._counters: dict[str, int] = {}  # token prefix → next index

    # -- Introspection ---------------------------------------------------

    @property
    def mapping(self) -> dict[str, str]:
        """A copy of the token → canonical-value mapping (read-only)."""
        return {token: info.canonical for token, info in self._tokens.items()}

    def __len__(self) -> int:
        return len(self._tokens)

    # -- Registration ----------------------------------------------------

    def register(self, label: str, raw_value: str) -> str | None:
        """Resolve a raw value to its token, creating one if needed.

        Returns the token, or ``None`` if the label is not maskable or the
        value is empty.  See ``_resolve`` for the case/alias/ambiguity rules.
        """
        return self._resolve(label, raw_value)

    # -- Masking (inbound: raw → token) ---------------------------------

    def mask_text(self, text: str) -> str:
        """Mask a single string: discover entities, then apply all known tokens."""
        if not isinstance(text, str) or not text:
            return text
        pairs = self._discover_pairs(text)
        pairs += [(surface, token) for surface, token in self._all_surfaces
                  if surface.casefold() in text.casefold()]
        return self._replace(text, pairs)

    def mask_value(self, value: Any) -> Any:
        """Mask every string leaf inside a nested dict/list (e.g. a tool result).

        Runs the NER pipeline **once** over all string content for efficiency,
        then rewrites each leaf with case-insensitive replacement.
        """
        blob = "\n".join(self._iter_strings(value))
        pairs = self._discover_pairs(blob) if blob.strip() else []
        pairs += list(self._all_surfaces)
        return self._map_strings(value, lambda leaf: self._replace(leaf, pairs))

    # -- Unmasking (outbound: token → raw) ------------------------------

    def unmask_text(self, text: str) -> str:
        """Replace every ``[PREFIX_N]`` token with its fullest known value.

        Unknown tokens are left untouched (the LLM may invent a token that was
        never registered — we never want to leak or corrupt such text).
        """
        if not isinstance(text, str) or "[" not in text:
            return text
        return _TOKEN_RE.sub(
            lambda match: self._tokens[match.group(0)].canonical
            if match.group(0) in self._tokens
            else match.group(0),
            text,
        )

    def unmask_value(self, value: Any) -> Any:
        """Unmask every string leaf inside a nested dict/list (e.g. tool args)."""
        return self._map_strings(value, self.unmask_text)

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
            self._discover_pairs(blob)

    # -- Resolution ------------------------------------------------------

    def _resolve(self, label: str, surface: str) -> str | None:
        """Map a surface form to a token, with case/alias/ambiguity handling."""
        prefix = self._maskable_labels.get(label)
        if prefix is None:
            return None

        surface = surface.strip()
        if not surface:
            return None

        # Normalize proper-name casing once, here, so every downstream use (new
        # token canonical, alias surfaces, unmasked tool args/answers) is
        # consistent regardless of how the user typed the name.
        if prefix in _NAME_PREFIXES:
            surface = _capitalize_name(surface)

        normalized = _normalize(surface)

        # 1. Exact (case/space-insensitive) match against a known full surface.
        token = self._norm_index.get(normalized)
        if token is not None:
            self._note_surface(token, surface)
            return token

        # 2. Partial / variant match: subset of an existing token's words.
        words = _significant_words(normalized)
        if words:
            candidates = {
                candidate
                for candidate, info in self._tokens.items()
                if info.prefix == prefix and _subset(words, info.words)
            }
            if len(candidates) == 1:
                # Unambiguous variant → reuse the token, but do NOT persist this
                # surface, so a later second match ("another John") is correctly
                # re-judged as ambiguous instead of locking onto this one.
                return next(iter(candidates))
            # 0 candidates → genuinely new; >1 → ambiguous, don't guess. Both
            # fall through to a fresh token.

        return self._new_token(prefix, surface, normalized, words)

    def _new_token(
        self, prefix: str, surface: str, normalized: str, words: set[str]
    ) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        token = f"[{prefix}_{self._counters[prefix]}]"
        self._tokens[token] = _Token(
            prefix=prefix, canonical=surface, words=set(words), surfaces={normalized}
        )
        self._norm_index[normalized] = token
        self._all_surfaces.append((surface, token))
        return token

    def _note_surface(self, token: str, surface: str) -> None:
        """Record a new full surface variant and keep the canonical the fullest."""
        info = self._tokens[token]
        normalized = _normalize(surface)
        if normalized not in info.surfaces:
            info.surfaces.add(normalized)
            info.words |= _significant_words(normalized)
            self._all_surfaces.append((surface, token))
        if len(surface) > len(info.canonical):
            info.canonical = surface

    # -- Discovery & replacement ----------------------------------------

    def _discover_pairs(self, text: str) -> list[tuple[str, str]]:
        """Run NER, resolve each entity, return (surface, token) pairs.

        Entities are resolved in order of first appearance so token numbers are
        stable and intuitive ([PERSON_1] is the first person mentioned) and
        identical every time a chat is rebuilt from its stored history.
        """
        entities = list(self._extractor(text))
        entities.sort(
            key=lambda entity: entity["start"]
            if entity.get("start") is not None
            else len(text)
        )
        pairs: list[tuple[str, str]] = []
        for entity in entities:
            surface = (entity.get("text") or "").strip()
            token = self._resolve(entity.get("label", ""), surface)
            if token:
                pairs.append((surface, token))
        return pairs

    @staticmethod
    def _replace(text: str, pairs: list[tuple[str, str]]) -> str:
        """Replace each surface with its token, longest-first, case-insensitively.

        Longest-first prevents a shorter surface (``Acme``) from corrupting a
        longer one (``Acme Corp``). Alphanumeric look-arounds act as boundaries
        so we never mask inside a larger word.
        """
        if not text:
            return text
        for surface, token in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
            if not surface:
                continue
            pattern = re.compile(
                r"(?<![0-9A-Za-z])" + re.escape(surface) + r"(?![0-9A-Za-z])",
                re.IGNORECASE,
            )
            text = pattern.sub(lambda _match, replacement=token: replacement, text)
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
