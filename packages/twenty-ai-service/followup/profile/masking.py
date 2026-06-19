"""PII masking for the Follow-Up pipeline's LLM boundaries.

The extraction and synthesis steps send the LLM real person names and the raw
email body. This module wraps the shared :class:`agent.masking.EntityHandleMap`
so those LLMs see stable handles (``person001``) instead — while record ids
(``crm_<uuid>``) stay visible (opaque, leak nothing, and the model needs them for
attribution). Real values are restored on the way out via :meth:`ProfileMasker.unmask`,
so persisted facts and the synthesized narrative keep real names.

Scope is deliberately **person + email + phone only**. Company / competitor /
location names are business context, not PII, and the NER tagger mislabels
ordinary words ("Budget", "Segment") as companies — masking them would corrupt
the facts. Only the genuinely-sensitive labels are masked here.

Model loading is an explicit startup concern, not a per-call one: the Presidio /
spaCy models are loaded once by :func:`ensure_models_loaded` (called from
``PipelineDeps.create``), so real runs discover not-yet-known names while unit
tests — which never build real deps — stay fast and model-free.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from agent.masking import EntityHandleMap

logger = logging.getLogger(__name__)

# The only NER labels that are genuinely PII in a B2B CRM context. Everything
# else (company, location, url, …) is left visible for the model to reason over.
# Phone numbers are unambiguous PII (no company-mislabel risk), so they mask too.
_PII_LABELS: dict[str, str] = {
    "person": "person",
    "email address": "email",
    "phone number": "phone",
}

# The shared EntityHandleMap deliberately preserves email addresses (it stashes
# them so name-masking can't corrupt them), so we mask them ourselves with this
# pattern, registering each into the SAME handle map so unmasking stays unified.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A handle reference, optionally with a dotted field. Case-INSENSITIVE on the
# token so we still unmask "Person003" — the LLM capitalizes handles at sentence
# starts in prose, which the shared (lowercase-only) unmasker would leak.
_HANDLE_TOKEN_RE = re.compile(r"\b([A-Za-z]+\d{3,})(?:\.([A-Za-z_]+))?\b")

_models_ready: Optional[bool] = None


def ensure_models_loaded() -> bool:
    """Load the PII NER models once (idempotent); return whether they're available.

    Without them, discovery of names not already in the CRM silently no-ops, so
    real runs call this at startup. Failure degrades to "registered names only"
    rather than crashing the pipeline.
    """
    global _models_ready
    if _models_ready is not None:
        return _models_ready
    try:
        from pipelines import load_models, models_loaded

        if not models_loaded():
            load_models()
        _models_ready = bool(models_loaded())
    except Exception as exc:  # models are an optional, heavy dependency
        logger.warning("PII NER models unavailable; new-name masking disabled: %s", exc)
        _models_ready = False
    return _models_ready


def models_available() -> bool:
    """Whether the NER models are loaded right now (never triggers a load)."""
    try:
        from pipelines import models_loaded

        return bool(models_loaded())
    except Exception:
        return False


def _split_name(name: str) -> dict[str, str]:
    """Flattened display name → ``{firstName, lastName}`` so first-name mentions
    ("John") mask to the same handle as the full name ("John Park")."""
    first, _, last = (name or "").strip().partition(" ")
    return {"firstName": first, "lastName": last}


class ProfileMasker:
    """One masking session for a single extraction or synthesis pass.

    Seed it with the deal's known people via :meth:`register`, then :meth:`mask`
    inbound text and :meth:`unmask` the model's output. Cheap to construct; holds
    no global state.
    """

    def __init__(
        self,
        *,
        extractor: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        discover: Optional[bool] = None,
    ) -> None:
        # An injected extractor (tests) is itself the discovery source, so default
        # discovery on; otherwise discovery needs the real models to be loaded.
        if discover is None:
            discover = True if extractor is not None else models_available()
        self._map = EntityHandleMap(label_to_type=dict(_PII_LABELS), extractor=extractor)
        self._discover = discover

    # -- Seeding ---------------------------------------------------------

    def register(
        self,
        *,
        contacts: Optional[list[dict[str, Any]]] = None,
        shadows: Optional[list[Any]] = None,
    ) -> "ProfileMasker":
        """Register the deal's known people so their names mask consistently.

        Companies are intentionally NOT registered — their names are not PII and
        must stay visible. Returns ``self`` for fluent use.
        """
        for contact in contacts or []:
            self._register_person(contact)
        for shadow in shadows or []:
            name = getattr(shadow, "name", None)
            if name:
                self._map.register_privacy("person", name)
        return self

    def _register_person(self, contact: dict[str, Any]) -> None:
        name = contact.get("name")
        record = {
            "id": contact.get("id"),
            "name": _split_name(name) if isinstance(name, str) else name,
        }
        self._map.register_resolved("person", record)

    # -- Masking / unmasking --------------------------------------------

    def mask(self, text: str) -> str:
        """Mask free text (e.g. the email body): registered names + NER-discovered."""
        return self._mask_emails(self._map.mask_text(text, discover=self._discover))

    def mask_known(self, text: str) -> str:
        """Mask only already-registered surfaces (plus emails) — no name NER.

        Used on pre-rendered blocks (the KNOWN ENTITIES list) where running NER
        over structured text would mis-tag field values.
        """
        return self._mask_emails(self._map.mask_text(text, discover=False))

    def _mask_emails(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            handle = self._map.register_privacy("email", match.group(0))
            return handle.name if handle is not None else match.group(0)

        return _EMAIL_RE.sub(replace, text)

    def unmask(self, value: Any) -> Any:
        """Restore real values in the model's output (a string or nested dict/list)."""
        if isinstance(value, str):
            return self._unmask_text(value)
        if isinstance(value, list):
            return [self.unmask(item) for item in value]
        if isinstance(value, dict):
            return {key: self.unmask(item) for key, item in value.items()}
        return value

    def unmask_fields(
        self, items: list[dict[str, Any]], fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Unmask only the named free-text fields of each item, in place.

        Id-bearing fields (``entity_id``, ``from_id``, …) are left untouched —
        they carry ``crm_``/``shadow_`` labels, never handles.
        """
        for item in items:
            for field in fields:
                if isinstance(item.get(field), str):
                    item[field] = self._unmask_text(item[field])
        return items

    def _unmask_text(self, text: str) -> str:
        if not text:
            return text
        handles = {handle.name: handle for handle in self._map.handles}

        def replace(match: re.Match[str]) -> str:
            handle = handles.get(match.group(1).lower())
            if handle is None:
                return match.group(0)  # not one of ours — leave it untouched
            field = match.group(2)
            if field is None:
                return handle.canonical
            return str(handle.fields.get(field, match.group(0)))

        return _HANDLE_TOKEN_RE.sub(replace, text)


__all__ = ["ProfileMasker", "ensure_models_loaded", "models_available"]
