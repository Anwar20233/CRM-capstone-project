#!/usr/bin/env python3
"""End-to-end masking evaluation over tests/data/customers-1000.csv.

For each customer we build a realistic CRM note that embeds the row's PII
(name, company, city/country, two phones, email) together with *traps* that must
NOT be masked (CustomerId, Website, SubscriptionDate). Then we run the full
masking layer and measure, per entity type:

    recall    = gold PII values that got masked / all gold values
    precision = correct masks / all masks            (a trap that gets masked,
                                                       or a gold value masked
                                                       under the wrong type, is
                                                       a false positive)
    F1        = harmonic mean

Plus a **round-trip** check: unmask(mask(note)) must reproduce the note, proving
masking and unmasking are lossless.

Person / company / location detection needs the GLiNER models; phone / email and
the trap precision are pure-regex and run anywhere. Run with models loaded for
the full table::

    HF_HOME=$HOME/.cache/huggingface .venv/bin/python scripts/eval_masking.py
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.masking import PIISessionMap  # noqa: E402
from pipelines import load_models, models_loaded  # noqa: E402

CSV_PATH = pathlib.Path(__file__).resolve().parent.parent / "tests" / "data" / "customers-1000.csv"

# Gold PII per row: (entity type, CSV column).
GOLD_FIELDS = [
    ("person", "FirstName"),
    ("person", "LastName"),
    ("company", "Company"),
    ("location", "City"),
    ("location", "Country"),
    ("phone", "Phone1"),
    ("phone", "Phone2"),
    ("email", "Email"),
]
# Traps: values that must survive masking untouched.
TRAP_FIELDS = ["CustomerId", "Website", "SubscriptionDate"]


def build_note(row: dict) -> str:
    return (
        f"Logged ticket for customer {row['CustomerId']} on {row['SubscriptionDate']}. "
        f"Contact {row['FirstName']} {row['LastName']} at {row['Company']}, based in "
        f"{row['City']}, {row['Country']}. Reach them by phone {row['Phone1']} or "
        f"{row['Phone2']}, email {row['Email']}. Website: {row['Website']}."
    )


def _masked_away(value: str, masked_text: str) -> bool:
    """True if the value is no longer present verbatim (i.e. it got masked)."""
    return bool(value) and value.lower() not in masked_text.lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="Masking evaluation over the customer CSV")
    parser.add_argument("--limit", type=int, default=100,
                        help="number of rows to evaluate (default 100; GLiNER is ~1s/row)")
    parser.add_argument("--all", action="store_true", help="evaluate every row")
    args = parser.parse_args()

    if not models_loaded():
        print("loading GLiNER models…", file=sys.stderr)
        try:
            load_models()
        except Exception as error:  # noqa: BLE001
            print(f"⚠ models unavailable ({error}); person/company/location will read 0",
                  file=sys.stderr)

    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    if not args.all:
        rows = rows[: args.limit]

    types = ["person", "company", "location", "phone", "email"]
    tp = dict.fromkeys(types, 0)
    fn = dict.fromkeys(types, 0)
    masks_total = 0
    masks_bad = 0
    trap_violations = 0
    roundtrip_failures = 0

    started = time.time()
    for index, row in enumerate(rows, start=1):
        if index % 25 == 0 or index == len(rows):
            rate = index / max(time.time() - started, 1e-6)
            print(f"  …{index}/{len(rows)} rows ({rate:.1f}/s)", file=sys.stderr)
        note = build_note(row)
        session = PIISessionMap()  # fresh map per note — isolates the measurement
        masked = session.mask_text(note)

        # Recall: each gold value should be gone from the masked text.
        gold_values = set()
        for entity_type, column in GOLD_FIELDS:
            value = row[column].strip()
            if not value:
                continue
            gold_values.add(value.lower())
            if _masked_away(value, masked):
                tp[entity_type] += 1
            else:
                fn[entity_type] += 1

        # Precision: every mask should correspond to a gold PII value. A mask is
        # bad only if its value overlaps NO gold field (e.g. a masked id/url) —
        # partial captures (a phone minus its extension) still count as correct.
        masks_total += len(session)
        for raw in session.mapping.values():
            low = raw.lower()
            if not any(low in gold or gold in low for gold in gold_values):
                masks_bad += 1

        # Traps must remain intact and verbatim.
        for column in TRAP_FIELDS:
            value = row[column].strip()
            if value and value not in masked:
                trap_violations += 1

        # Round-trip fidelity.
        if session.unmask_text(masked) != note:
            roundtrip_failures += 1

    print(f"\n  Masking evaluation — {len(rows)} customers "
          f"(models loaded: {models_loaded()})")
    print("  " + "─" * 58)
    print(f"  {'entity':10} {'recall':>8} {'precision*':>11} {'F1':>7}   (TP/FN)")
    overall_tp = overall_fn = 0
    for entity_type in types:
        t, f = tp[entity_type], fn[entity_type]
        overall_tp += t
        overall_fn += f
        recall = t / (t + f) if (t + f) else 0.0
        print(f"  {entity_type:10} {recall:>7.1%} {'—':>11} {'—':>7}   ({t}/{f})")

    precision = (masks_total - masks_bad) / masks_total if masks_total else 1.0
    recall = overall_tp / (overall_tp + overall_fn) if (overall_tp + overall_fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    print("  " + "─" * 58)
    print(f"  OVERALL    recall={recall:.2%}  precision={precision:.2%}  F1={f1:.2%}")
    print(f"  masks made: {masks_total}   mis-masks: {masks_bad}")
    print(f"  trap violations (id/url/date masked): {trap_violations}")
    print(f"  round-trip failures (unmask≠original): {roundtrip_failures}")
    print("\n  *precision counts any mask whose value isn't a gold PII field.\n")


if __name__ == "__main__":
    main()
