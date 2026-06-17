from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from followup.context.schemas import DealContext

ObjectionCategory = Literal[
    "security",
    "privacy",
    "pricing",
    "legal",
    "timeline",
    "authority",
    "integration",
]

_OBJECTION_PATTERNS: dict[ObjectionCategory, tuple[str, ...]] = {
    "security": (
        r"\bsecurity\b",
        r"\bunauthorized\b",
        r"\baccess control\b",
    ),
    "privacy": (
        r"\bdata privacy\b",
        r"\bprivacy\b",
        r"\bgdpr\b",
        r"\bpersonal data\b",
    ),
    "pricing": (
        r"\bpricing\b",
        r"\bbudget\b",
        r"\bcost\b",
        r"\btoo expensive\b",
    ),
    "legal": (
        r"\blegal\b",
        r"\bcompliance\b",
        r"\bregulatory\b",
    ),
    "timeline": (
        r"\btimeline\b",
        r"\bdeadline\b",
        r"\bdelay\b",
    ),
    "authority": (
        r"\bdecision maker\b",
        r"\bapproval\b",
        r"\bsign off\b",
    ),
    "integration": (
        r"\bintegration\b",
        r"\bapi\b",
        r"\bwebhook\b",
    ),
}


class DetectedObjection(BaseModel):
    category: ObjectionCategory
    excerpt: str
    source: str | None = None


def _timeline_text_chunks(context: DealContext) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    for timeline_item in context.timeline:
        text = " ".join(
            filter(
                None,
                [timeline_item.title, timeline_item.summary or ""],
            ),
        ).strip()
        if not text:
            continue
        source = timeline_item.source or f"timeline.{timeline_item.type}"
        chunks.append((text, source))
    return chunks


def detect_customer_objections(context: DealContext) -> list[DetectedObjection]:
    objections: list[DetectedObjection] = []
    seen: set[tuple[ObjectionCategory, str]] = set()

    for text, source in _timeline_text_chunks(context):
        normalized = text.lower()
        for category, patterns in _OBJECTION_PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, normalized, re.IGNORECASE)
                if match is None:
                    continue
                key = (category, match.group(0).lower())
                if key in seen:
                    continue
                seen.add(key)
                start = max(0, match.start() - 40)
                end = min(len(text), match.end() + 80)
                objections.append(
                    DetectedObjection(
                        category=category,
                        excerpt=text[start:end].strip(),
                        source=source,
                    ),
                )
                break

    return objections
