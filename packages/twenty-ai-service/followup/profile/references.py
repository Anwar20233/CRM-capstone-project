"""Entity reference labels exchanged with the LLM.

The model never sees raw foreign keys on their own — every entity is labelled
``crm_<id>`` (an existing CRM record) or ``shadow_<id>`` (a tracked-but-unknown
person). The prompt builder encodes labels; the persistence layer decodes the
labels the model echoes back. Both sides import from here so the format can
never drift between them.
"""

from __future__ import annotations

from typing import Literal, Optional

_CRM_PREFIX = "crm_"
_SHADOW_PREFIX = "shadow_"

ReferenceKind = Literal["crm", "shadow"]


def crm_label(record_id: object) -> str:
    """Label an existing CRM record (person, company, or opportunity)."""
    return f"{_CRM_PREFIX}{record_id}"


def shadow_label(shadow_id: object) -> str:
    """Label a shadow entity."""
    return f"{_SHADOW_PREFIX}{shadow_id}"


def parse_label(label: object) -> Optional[tuple[ReferenceKind, str]]:
    """Decode a label into ``(kind, raw_id)``.

    Returns ``None`` for anything that is not a well-formed ``crm_``/``shadow_``
    label, including ``None`` and the literal string ``"null"`` the model emits
    for "no match". Callers treat ``None`` as an unresolvable reference.
    """
    if not isinstance(label, str):
        return None
    if label.startswith(_CRM_PREFIX):
        raw = label[len(_CRM_PREFIX) :]
        return ("crm", raw) if raw else None
    if label.startswith(_SHADOW_PREFIX):
        raw = label[len(_SHADOW_PREFIX) :]
        return ("shadow", raw) if raw else None
    return None
