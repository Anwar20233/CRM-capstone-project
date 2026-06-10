"""Presidio-backed PII detection — the detection half of the masking layer.

Replaces the former GLiNER ensemble. We use Presidio's ``AnalyzerEngine`` only
(spans + scores) and do our own handle substitution downstream — the anonymizer
is intentionally not used.

The public surface is identical to the old pipeline so the FastAPI ``/ner``
route and the Node ``text-masking`` consumer are drop-in: ``extract(text)``
returns ``[{label, text, score, start, end}]`` with the same lowercase labels
("person", "company", "email address", …), plus ``load_models`` /
``models_loaded`` for warm-up and graceful degradation.

NLP model
~~~~~~~~~
Detection quality is dominated by the spaCy model. We default to the
transformer pipeline (``en_core_web_trf``) for the best PERSON/ORG recall —
precision matters less because the CRM resolver confirms each match. Override
with ``PRESIDIO_SPACY_MODEL`` (e.g. ``en_core_web_lg``) for a lighter, faster,
CPU-friendly setup. Presidio does not expose ORGANIZATION out of the box, so we
map spaCy's ``ORG`` label to it explicitly.

If Presidio / the spaCy model are not installed (e.g. a unit-test process that
never starts the service), loading fails softly: ``models_loaded()`` returns
``False`` and ``extract()`` returns ``[]``, exactly like the old pipeline — the
handle map's plain-string replacement still works against already-known values.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Presidio entity type → downstream lowercase label. These labels are the
# contract the rest of the system (handle map, Node consumer) already expects.
_PRESIDIO_TO_LABEL: dict[str, str] = {
    "PERSON": "person",
    "ORGANIZATION": "company",
    "EMAIL_ADDRESS": "email address",
    "PHONE_NUMBER": "phone number",
    "LOCATION": "location",
    "URL": "url",
}

# Only ask the analyzer for the entities we mask — fewer recognizers, less noise.
_SUPPORTED_ENTITIES: list[str] = list(_PRESIDIO_TO_LABEL)

# spaCy model — transformer by default; swappable via env for lighter setups.
_SPACY_MODEL = os.environ.get("PRESIDIO_SPACY_MODEL", "en_core_web_trf")

# spaCy/HF NER label → Presidio entity. The ``ORG → ORGANIZATION`` line is what
# surfaces company names (Presidio has no built-in company recognizer).
_MODEL_TO_PRESIDIO_ENTITY: dict[str, str] = {
    "PERSON": "PERSON",
    "PER": "PERSON",
    "ORG": "ORGANIZATION",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "FAC": "LOCATION",
}

# Built lazily on first use; ``None`` means "not loaded yet". ``_load_failed``
# latches so we don't retry an impossible import on every call.
_analyzer: Any = None
_load_failed = False


def load_models() -> None:
    """Build the Presidio analyzer singleton; degrade softly if unavailable."""
    global _analyzer, _load_failed

    if _analyzer is not None or _load_failed:
        return

    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
                "ner_model_configuration": {
                    "model_to_presidio_entity_mapping": _MODEL_TO_PRESIDIO_ENTITY,
                    # Keep recall high — the resolver filters false positives.
                    "low_score_entity_names": [],
                },
            }
        )
        _analyzer = AnalyzerEngine(
            nlp_engine=provider.create_engine(),
            supported_languages=["en"],
        )
    except Exception as error:  # noqa: BLE001 — optional dep; degrade, don't crash
        _load_failed = True
        logger.warning("Presidio analyzer unavailable, masking detection disabled: %s", error)


def models_loaded() -> bool:
    """Return ``True`` once the analyzer is ready to extract entities."""
    return _analyzer is not None


def extract(text: str) -> list[dict[str, Any]]:
    """Detect maskable PII in *text*, normalised to the shared entity shape."""
    if not text:
        return []

    load_models()
    if _analyzer is None:
        return []

    results = _analyzer.analyze(
        text=text,
        language="en",
        entities=_SUPPORTED_ENTITIES,
    )

    entities: list[dict[str, Any]] = []
    for result in results:
        label = _PRESIDIO_TO_LABEL.get(result.entity_type)
        if label is None:
            continue
        entities.append(
            {
                "label": label,
                "text": text[result.start : result.end],
                "score": float(result.score),
                "start": result.start,
                "end": result.end,
            }
        )
    return entities
