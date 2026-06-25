#!/usr/bin/env python3
"""Proof harness for PII_MASKING_RELIABILITY_PLAN.md.

Demonstrates, with no ML models required, that the dependable-detection
techniques (Layer-0 protected spans + context gating, principles 4 & 5 in the
plan) eliminate the phone false positives that the current bare regex produces,
while keeping recall on real phone numbers.

Run from packages/twenty-ai-service::

    .venv/bin/python scripts/proof_phone_guard.py
"""

from __future__ import annotations

import re

# ── Current pipeline phone regex (the buggy one, from ner_pipeline.py) ──────
PHONE_RE = re.compile(r"(?<!\d)(\+?(?:\d[\s\-.]?){7,14}\d)(?!\d)")

# ── Proposed Layer-0: structured NON-PII the agent must keep verbatim ───────
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Generic record id: a hex-ish token containing at least one letter, len >= 6.
ID_RE = re.compile(r"\b(?=[0-9a-fA-F-]*[a-fA-F])[0-9a-fA-F]{6,}(?:-[0-9a-fA-F]{2,})*\b")

PHONE_CONTEXT = re.compile(
    r"\b(call|phone|tel|mobile|cell|fax|whatsapp|reach me|number)\b", re.I
)
ID_CONTEXT = re.compile(
    r"\b(id|uuid|order|invoice|sku|ref|reference|ticket|account)\b", re.I
)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for regex in (UUID_RE, ID_RE):
        spans += [(m.start(), m.end()) for m in regex.finditer(text)]
    return spans


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(not (end <= s0 or start >= s1) for s0, s1 in spans)


def detect_phones(text: str, guarded: bool) -> list[str]:
    out: list[str] = []
    protected = _protected_spans(text) if guarded else []
    for match in PHONE_RE.finditer(text):
        start, end, candidate = match.start(), match.end(), match.group()
        if guarded:
            if _overlaps(start, end, protected):  # inside a UUID / record id
                continue
            window = text[max(0, start - 15) : end + 15]
            digits = re.sub(r"\D", "", candidate)
            has_format = ("+" in candidate) or (" " in candidate) or ("." in candidate)
            if ID_CONTEXT.search(window):
                continue
            if not has_format and not PHONE_CONTEXT.search(window):
                continue
            if not (7 <= len(digits) <= 15):
                continue
        out.append(candidate)
    return out


SHOULD_MASK = [
    "+971 50 123 4567",
    "call me at 415-555-0199",
    "tel: +1-800-555-0199",
    "phone 020 7946 0958",
]
TRAPS = [  # must produce NO phone match
    "ccc3198c-eeb1-43f3-849f-fc72aeffb0a2",
    "company id 711b5b56-3fdf-4d54-8454-737adbab2e65",
    "order 1234567890",
    "SKU 99-8454-737",
    "invoice ref 4500123456",
]


def main() -> None:
    for label, guarded in [("CURRENT (regex only)", False), ("PROPOSED (guarded)", True)]:
        true_positives = sum(1 for s in SHOULD_MASK if detect_phones(s, guarded))
        false_positives = sum(1 for s in TRAPS if detect_phones(s, guarded))
        print(f"\n## {label}")
        print(f"   real phones detected : {true_positives}/{len(SHOULD_MASK)}  (recall)")
        print(f"   FALSE positives on IDs: {false_positives}/{len(TRAPS)}")
        for sample in TRAPS:
            hit = detect_phones(sample, guarded)
            if hit:
                print(f"     x masked {hit} inside: {sample}")


if __name__ == "__main__":
    main()
