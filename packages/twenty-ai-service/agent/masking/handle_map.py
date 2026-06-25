"""EntityHandleMap — the session's entity → structured-handle translation.

This replaces the old opaque-token ``PIISessionMap``. Instead of ``[PERSON_1]``
standing for a single raw string, an entity becomes a **structured handle** that
carries the resolved CRM record:

    company001 = {id: "<uuid>", name: "Acme Corp", ...}

The LLM never sees raw PII. It references handles, and — because resolved
handles expose fields — it can address a specific field with dotted access:

    person001        → masks a name; unmasks to the display name
    person001.id     → unmasks to the record UUID (for tool arguments)
    person001.email  → unmasks to the stored email

There are two kinds of handle:

- **Resolved** (``person`` / ``company``) — backed by a CRM record. Keyed by
  record id, so the *same record* always reuses the *same handle* across turns,
  and a genuinely different record (a different ``John``) gets a fresh handle.
- **Privacy** (``email`` / ``phone`` / ``location`` / ``url``, or an
  unresolved person/company) — a value we mask for privacy but do not tie to a
  record. Keyed by normalized value.

Resolution (turning a name into a CRM record, and disambiguating when there are
several) lives in :mod:`agent.masking.resolver` and is driven by the
orchestrator — this class is a decoupled store that knows nothing about the
bridge. It only needs an ``extractor`` (defaulting to the Presidio pipeline) to
discover stray PII when masking free text such as tool results.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

# An extractor maps raw text to entity dicts: {label, text, start, end, ...} —
# exactly what ``pipelines.extract`` returns.
Extractor = Callable[[str], list[dict[str, Any]]]

# NER label → handle entity-type. Person/company are resolvable to records; the
# rest are privacy-only. Anything not listed here is left visible (date, money,
# job title, …) so the agent can still reason about deals and timelines.
DEFAULT_LABEL_TO_TYPE: dict[str, str] = {
    "person": "person",
    "company": "company",
    "email address": "email",
    "phone number": "phone",
    "location": "location",
    "url": "url",
}

# Handle types backed by a CRM record (vs. masked privacy-only values).
RESOLVABLE_TYPES: frozenset[str] = frozenset({"person", "company"})

# Entity types whose stored surface is given title-case so values typed in
# lower-case ("acme corp") round-trip with consistent casing. Emails/phones/urls
# are excluded — capitalizing them would corrupt the value.
_NAME_TYPES: frozenset[str] = frozenset({"person", "company", "location"})

# All handle entity-type prefixes (person, company, email, phone, location, url).
_ENTITY_TYPES: tuple[str, ...] = ("person", "company", "email", "phone", "location", "url")

# Matches a handle reference with optional dotted field: "person001",
# "company002.id", "email001.value".
_HANDLE_RE = re.compile(r"\b([a-z]+\d{3,})(?:\.([a-zA-Z_]+))?\b")

# Like ``_HANDLE_RE`` but anchored to known entity-type prefixes, so the
# unresolved-reference guard never false-flags ordinary text like "item123".
_ENTITY_REF_RE = re.compile(
    r"\b((?:" + "|".join(_ENTITY_TYPES) + r")\d{3,})(?:\.([a-zA-Z_]+))?\b"
)

# Splits a normalized string into words for stopword filtering / display.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# A bare email address, for the opt-in email-masking pass (``mask_emails``).
# Applied AFTER name replacement, since ``_replace`` stashes emails untouched.
_BARE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _normalize(text: str) -> str:
    """Casefold + collapse whitespace so casing/spacing never splits a handle."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _capitalize_name(text: str) -> str:
    """Capitalize each word's first (lower-case) letter, preserving the rest.

    "john doe" → "John Doe", "acme corp" → "Acme Corp"; already-correct casing
    ("McDonald", "iPhone") is left intact.
    """
    def fix_word(word: str) -> str:
        for index, char in enumerate(word):
            if char.isalpha():
                return word[:index] + char.upper() + word[index + 1 :] if char.islower() else word
        return word

    return " ".join(fix_word(word) for word in text.split())


@dataclass
class Handle:
    """One masked entity within a session."""

    name: str  # the reference the LLM uses, e.g. "person001"
    entity_type: str  # person | company | email | phone | location | url
    canonical: str  # bare-handle display value (name, or the raw value)
    fields: dict[str, Any] = field(default_factory=dict)  # dotted-access fields
    record_id: str | None = None  # set for resolved (CRM-backed) handles
    surfaces: set[str] = field(default_factory=set)  # normalized forms masked → this

    @property
    def is_resolved(self) -> bool:
        return self.record_id is not None


class EntityHandleMap:
    """Session-scoped map between CRM entities / PII and structured handles.

    Parameters
    ----------
    extractor:
        Callable returning NER entities for a string. Defaults to the shared
        Presidio pipeline (lazy-imported). Inject a stub in tests.
    label_to_type:
        NER label → handle entity-type. Defaults to ``DEFAULT_LABEL_TO_TYPE``.
    mask_emails:
        When true, mask bare email addresses too. ``_replace`` deliberately
        stashes emails behind sentinels so name-masking can't corrupt them, so
        without this flag emails reach the LLM verbatim. Off by default to keep
        callers that mask emails themselves (``ProfileMasker``) unchanged.
    """

    def __init__(
        self,
        *,
        extractor: Extractor | None = None,
        label_to_type: dict[str, str] | None = None,
        mask_emails: bool = False,
    ) -> None:
        self._extractor = extractor or _default_extractor
        self._label_to_type = label_to_type or dict(DEFAULT_LABEL_TO_TYPE)
        self._mask_emails = mask_emails

        self._handles: dict[str, Handle] = {}  # name → handle
        self._by_record_id: dict[str, Handle] = {}  # record id → handle
        self._by_surface: dict[str, Handle] = {}  # normalized surface → handle
        self._counters: dict[str, int] = {}  # entity-type → next index

    # -- Introspection ---------------------------------------------------

    @property
    def handles(self) -> list[Handle]:
        """All handles, in creation order."""
        return list(self._handles.values())

    @property
    def mapping(self) -> dict[str, str]:
        """A copy of the handle-name → canonical-value mapping (read-only)."""
        return {name: handle.canonical for name, handle in self._handles.items()}

    def __len__(self) -> int:
        return len(self._handles)

    def detect(self, text: str) -> list[dict[str, Any]]:
        """Run the entity extractor over *text* (used by the orchestrator)."""
        if not isinstance(text, str) or not text:
            return []
        return list(self._extractor(text))

    def handle_for_surface(self, surface: str) -> Handle | None:
        """Return the handle a given surface already masks to, if any."""
        return self._by_surface.get(_normalize(surface))

    # -- Registration ----------------------------------------------------

    def register_resolved(self, entity_type: str, record: dict[str, Any]) -> Handle | None:
        """Register a CRM record, returning its (possibly existing) handle.

        Keyed by ``record["id"]`` so the same record reuses one handle across
        turns; a record without an id falls back to a privacy handle. Returns
        ``None`` only for an empty record (no id and no name).
        """
        record_id = record.get("id")
        display, surfaces, fields = _record_display(record)
        if not record_id:
            return self._register_privacy(entity_type, display)

        existing = self._by_record_id.get(record_id)
        if existing is not None:
            self._note_surfaces(existing, surfaces, claim=True)
            existing.fields.update(fields)
            return existing

        handle = self._new_handle(entity_type, display, record_id=record_id, fields=fields)
        self._note_surfaces(handle, surfaces, claim=True)
        return handle

    def register_privacy(self, entity_type: str, value: str) -> Handle | None:
        """Register a privacy-only value (email/phone/…/unresolved name)."""
        return self._register_privacy(entity_type, value)

    def register_records(self, value: Any) -> None:
        """Register every CRM record found in a (nested) tool result.

        Lets the LLM reference a freshly-fetched record by its handle on later
        turns, and ensures the record's name is masked back consistently.
        """
        for record, entity_type in _iter_records(value):
            self.register_resolved(entity_type, record)

    # -- Masking (inbound: raw → handle) --------------------------------

    def mask_text(self, text: str, *, discover: bool = True) -> str:
        """Mask a string by replacing known surfaces with their handle.

        When *discover* is true, the extractor also runs to catch and register
        PII not yet known (stray entities in tool results / history). The
        orchestrator pre-registers the user query's entities and passes
        ``discover=False`` to avoid a redundant pass.
        """
        if not isinstance(text, str) or not text:
            return text
        discovered = self._discover_pairs(text) if discover else []
        return self._mask_email_addresses(
            self._replace(text, self._all_pairs(extra=discovered))
        )

    def mask_value(self, value: Any) -> Any:
        """Mask every string leaf in a nested dict/list (e.g. a tool result).

        Tool/agent results are structured CRM data, not prose: we register the
        records they contain (so their ids become referenceable and their names
        get masked) and replace already-known surfaces — but we do NOT run NER
        over the blob. NER over a metadata/JSON payload mis-tags object names,
        system strings, and field values ("Company", "SYSTEM", entity_type
        values) as fresh entities, polluting the handle space and corrupting the
        very data the model needs. Free-text PII discovery belongs on user input
        (``mask_text``), not on structured results.
        """
        self.register_records(value)
        pairs = self._all_pairs(extra=[])
        return self._map_strings(
            value, lambda leaf: self._mask_email_addresses(self._replace(leaf, pairs))
        )

    def _mask_email_addresses(self, text: str) -> str:
        """Replace bare emails with handles when ``mask_emails`` is enabled.

        Runs after ``_replace`` (which stashes emails so name-masking skips them),
        registering each email into this same map so unmasking stays unified.
        """
        if not self._mask_emails or not text:
            return text

        def replace(match: re.Match[str]) -> str:
            handle = self.register_privacy("email", match.group(0))
            return handle.name if handle is not None else match.group(0)

        return _BARE_EMAIL_RE.sub(replace, text)

    # -- Unmasking (outbound: handle → raw) -----------------------------

    def unmask_text(self, text: str) -> str:
        """Replace handle references with their value (dotted field or canonical).

        ``person001`` → canonical; ``person001.id`` → the field value. Unknown
        handles / fields are left untouched (never leak or guess).
        """
        if not isinstance(text, str) or not text:
            return text

        def replace(match: re.Match[str]) -> str:
            handle = self._handles.get(match.group(1))
            if handle is None:
                return match.group(0)
            field_name = match.group(2)
            if field_name is None:
                return handle.canonical
            if field_name in handle.fields:
                return str(handle.fields[field_name])
            # Known handle, unknown field — leave intact so a bad reference is
            # visible rather than silently dropped.
            return match.group(0)

        return _HANDLE_RE.sub(replace, text)

    def unmask_value(self, value: Any) -> Any:
        """Unmask every string leaf in a nested dict/list (e.g. tool args)."""
        return self._map_strings(value, self.unmask_text)

    def find_unresolved_references(self, value: Any) -> list[str]:
        """Return handle references in *value* that did not unmask to a value.

        A reference survives unmasking only when its handle is unknown, or its
        dotted field does not exist. Used by the worker to reject a tool call
        that would otherwise send a literal ``person009.id`` to the CRM, giving
        the model a chance to re-reference a valid handle.
        """
        leftovers: list[str] = []
        for leaf in self._iter_strings(value):
            for match in _ENTITY_REF_RE.finditer(leaf):
                handle = self._handles.get(match.group(1))
                field_name = match.group(2)
                if handle is None or (field_name is not None and field_name not in handle.fields):
                    leftovers.append(match.group(0))
        return leftovers

    # -- Priming & context ----------------------------------------------

    def prime(self, texts: Iterable[str]) -> None:
        """Register privacy entities found in stored (unmasked) chat history.

        Resolved (CRM-backed) handles are re-established by the orchestrator
        re-resolving names each turn; this only recovers privacy-only tokens so
        a reopened chat keeps masking known emails/phones consistently.
        """
        blob = "\n".join(text for text in texts if isinstance(text, str) and text)
        if blob.strip():
            self._discover_pairs(blob)

    def handle_context(self) -> str:
        """A compact briefing of available handles for the system prompt."""
        if not self._handles:
            return ""
        lines = ["## Available entity handles", ""]
        for handle in self._handles.values():
            if handle.is_resolved:
                fields = ", ".join(sorted(handle.fields))
                lines.append(f"- {handle.name} ({handle.entity_type}) — fields: {fields}")
            else:
                lines.append(f"- {handle.name} ({handle.entity_type}, private)")
        return "\n".join(lines)

    # -- Internal: handle creation --------------------------------------

    def _register_privacy(self, entity_type: str, value: str) -> Handle | None:
        value = (value or "").strip()
        if not entity_type or not value:
            return None
        if entity_type in _NAME_TYPES:
            value = _capitalize_name(value)
        normalized = _normalize(value)
        existing = self._by_surface.get(normalized)
        if existing is not None:
            return existing
        fields = {"value": value}
        handle = self._new_handle(entity_type, value, fields=fields)
        self._note_surfaces(handle, {normalized})
        return handle

    def _new_handle(
        self,
        entity_type: str,
        display: str,
        *,
        record_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Handle:
        self._counters[entity_type] = self._counters.get(entity_type, 0) + 1
        name = f"{entity_type}{self._counters[entity_type]:03d}"
        handle = Handle(
            name=name,
            entity_type=entity_type,
            canonical=display,
            fields=dict(fields or {}),
            record_id=record_id,
        )
        self._handles[name] = handle
        if record_id is not None:
            self._by_record_id[record_id] = handle
        return handle

    def _note_surfaces(self, handle: Handle, surfaces: Iterable[str], *, claim: bool = False) -> None:
        """Index new normalized surfaces so they mask onto this handle.

        Normally first-writer-wins so handles don't steal each other's surfaces.
        A resolved handle passes ``claim=True`` to take over a surface currently
        held by a *privacy* placeholder (never another resolved handle), so a
        record the user later pins down isn't masked by a stale privacy token.
        """
        for surface in surfaces:
            normalized = _normalize(surface)
            if not normalized:
                continue
            handle.surfaces.add(normalized)
            holder = self._by_surface.get(normalized)
            if holder is None or (claim and not holder.is_resolved and holder is not handle):
                self._by_surface[normalized] = handle

    # -- Internal: discovery & replacement ------------------------------

    def _discover_pairs(self, text: str) -> list[tuple[str, str]]:
        """Run the extractor and register each entity, returning (surface, name)."""
        entities = sorted(
            self._extractor(text),
            key=lambda entity: entity["start"] if entity.get("start") is not None else len(text),
        )
        pairs: list[tuple[str, str]] = []
        for entity in entities:
            surface = (entity.get("text") or "").strip()
            entity_type = self._label_to_type.get(entity.get("label", ""))
            if not entity_type or not surface:
                continue
            normalized = _normalize(surface)
            handle = self._by_surface.get(normalized) or self._register_privacy(entity_type, surface)
            if handle is not None:
                pairs.append((surface, handle.name))
        return pairs

    def _all_pairs(self, *, extra: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Every known (surface, handle-name) pair, plus freshly discovered ones."""
        pairs = [
            (surface, handle.name)
            for surface, handle in self._iter_known_surfaces()
        ]
        pairs.extend(extra)
        return pairs

    def _iter_known_surfaces(self) -> Iterator[tuple[str, Handle]]:
        for normalized, handle in self._by_surface.items():
            yield normalized, handle

    # Matches URLs (http/https/www) and bare email addresses. These tokens are
    # stashed behind placeholders before name replacement runs so that a company
    # name like "Anthropic" is never substituted inside "https://anthropic.com"
    # or a person's name inside their email address domain.
    _PROTECTED_TOKEN_RE = re.compile(
        r"https?://\S+|www\.\S+|\S+@\S+\.\S+",
        re.IGNORECASE,
    )

    @classmethod
    def _replace(cls, text: str, pairs: list[tuple[str, str]]) -> str:
        """Replace each surface with its handle, longest-first, case-insensitively.

        Longest-first stops a short surface ("Acme") from corrupting a longer
        one ("Acme Corp"); alnum look-arounds keep us from masking inside a word.
        URLs and email addresses are stashed behind null-byte sentinels before
        replacement runs and restored afterwards — preventing a company name from
        being substituted inside its own domain (e.g. "anthropic" in a URL).
        Surfaces already equal to their handle name are skipped (idempotent).
        """
        if not text:
            return text

        # Stash URLs / emails so name replacement never touches them.
        protected: list[str] = []

        def stash(match: re.Match[str]) -> str:
            slot = f"\x00P{len(protected)}\x00"
            protected.append(match.group(0))
            return slot

        text = cls._PROTECTED_TOKEN_RE.sub(stash, text)

        seen: set[tuple[str, str]] = set()
        for surface, name in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
            if not surface or surface == name or (surface, name) in seen:
                continue
            seen.add((surface, name))
            pattern = re.compile(
                r"(?<![0-9A-Za-z])" + re.escape(surface) + r"(?![0-9A-Za-z])",
                re.IGNORECASE,
            )
            text = pattern.sub(lambda _match, replacement=name: replacement, text)

        # Restore stashed tokens verbatim.
        for index, original in enumerate(protected):
            text = text.replace(f"\x00P{index}\x00", original)

        return text

    @classmethod
    def _iter_strings(cls, value: Any) -> Iterator[str]:
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
        if isinstance(value, str):
            return transform(value)
        if isinstance(value, dict):
            return {key: cls._map_strings(item, transform) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._map_strings(item, transform) for item in value]
        return value


# ---------------------------------------------------------------------------
# Record interpretation — turn a Twenty CRM record into display/surfaces/fields
# ---------------------------------------------------------------------------

def _record_display(record: dict[str, Any]) -> tuple[str, set[str], dict[str, Any]]:
    """Extract (display name, maskable surfaces, dotted-access fields) from a record.

    The record's name shape disambiguates the entity: a ``{firstName, lastName}``
    object is a person, a bare string is a company.
    """
    fields: dict[str, Any] = {}
    if record.get("id"):
        fields["id"] = record["id"]

    surfaces: set[str] = set()
    name = record.get("name")

    if isinstance(name, dict):  # person: FULL_NAME field
        first = (name.get("firstName") or "").strip()
        last = (name.get("lastName") or "").strip()
        display = " ".join(part for part in (first, last) if part)
        surfaces.update(part for part in (first, last, display) if part)
        if first:
            fields["firstName"] = first
        if last:
            fields["lastName"] = last
    else:  # company: TEXT name
        display = (name or "").strip()
        if display:
            surfaces.add(display)

    fields["name"] = display

    email = _primary(record.get("emails"), "primaryEmail")
    if email:
        fields["email"] = email
        # Email is a field value, not a name surface — adding it to surfaces would
        # cause the person handle to replace the email string wherever it appears
        # (e.g. in tool results), making the LLM output the person's name instead
        # of the actual email address.
    phone = _primary(record.get("phones"), "primaryPhoneNumber")
    if phone:
        fields["phone"] = phone
        # Same as email: phone numbers are field values, not masking surfaces.
    domain = _primary(record.get("domainName"), "primaryLinkUrl")
    if domain:
        fields["domainName"] = domain

    return display, {surface for surface in surfaces if surface}, fields


def _primary(value: Any, key: str) -> str | None:
    """Pull a primary value out of a Twenty composite field (emails/phones/links)."""
    if isinstance(value, dict):
        primary = value.get(key)
        return primary.strip() if isinstance(primary, str) and primary.strip() else None
    return None


def _iter_records(value: Any) -> Iterator[tuple[dict[str, Any], str]]:
    """Yield (record, entity_type) for record-shaped dicts in a nested payload.

    Sub-agents (e.g. the reader) return their resolved record as a JSON-encoded
    *string* in ``response``, so we parse JSON-looking string leaves and recurse
    into them — otherwise the structured record (and its id) would be invisible
    and only get NER-masked as a fieldless privacy handle.
    """
    if isinstance(value, str):
        if value[:1].strip() in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                return
            yield from _iter_records(parsed)
        return
    if isinstance(value, dict):
        entity_type = _classify_record(value)
        if entity_type is not None:
            yield value, entity_type
        for item in value.values():
            yield from _iter_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_records(item)


def _classify_record(record: dict[str, Any]) -> str | None:
    """Best-effort: is this dict a person or company record? Else ``None``."""
    if not isinstance(record.get("id"), str):
        return None
    name = record.get("name")
    if isinstance(name, dict) and ("firstName" in name or "lastName" in name):
        return "person"
    if isinstance(name, str) and ("domainName" in record or "employees" in record):
        return "company"
    return None


def _default_extractor(text: str) -> list[dict[str, Any]]:
    """Run the shared Presidio pipeline, degrading gracefully when unloaded."""
    from pipelines import extract, models_loaded

    if not models_loaded():
        return []
    return extract(text)
