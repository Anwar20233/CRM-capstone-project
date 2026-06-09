# PII Masking Reliability Plan

How to make entity masking **dependable** — grounded in how production PII
systems (Microsoft Presidio, cloud DLP, LLM privacy vaults) are actually built,
not in patching one edge case at a time.

---

## 0. The trigger bug (evidence)

A Twenty record id was sent to the agent and a chunk in the middle was masked as
a phone number. Reproduced exactly against the current
`pipelines/ner_pipeline.py` phone regex:

```
company id 711b5b56-3fdf-4d54-8454-737adbab2e65   ->  masked "54-8454-737" as PHONE
order 1234567890                                  ->  masked "1234567890" as PHONE
SKU 99-8454-737                                   ->  masked "99-8454-737" as PHONE
invoice ref 4500123456                            ->  masked "4500123456" as PHONE
```

This is not one bug — it is the **symptom of a missing architecture**. The
phone "recognizer" is a bare regex (`(?:\d[\s\-.]?){7,14}\d`) that fires on any
digit-and-separator run, and nothing downstream validates, scopes, or
deconflicts the match. Masking an id is doubly harmful: it both leaks nothing
useful *and* corrupts the identifier the agent needs to call tools with.

---

## 1. How dependable PII systems are built (the principles)

Every serious system (Presidio, AWS Comprehend, Google DLP, Skyflow/Private AI
vaults) converges on the same layered pipeline. A regex match is treated as a
*candidate*, never as truth.

| # | Principle | What it prevents |
| - | --------- | ---------------- |
| 1 | **Candidate → Validate → Decide.** A pattern match is only a hypothesis; a validator (checksum/format library) confirms it. | Structured-PII false positives (phones, cards, IBANs) — the single biggest FP source. |
| 2 | **One recognizer per entity** (regex + validator + context words), not one combined regex. | Cross-talk where a phone pattern eats an id. |
| 3 | **Confidence score + per-entity threshold + explainable decision.** Every span carries a score and *why* it fired. | Silent, unauditable mistakes. |
| 4 | **Context-aware scoring.** Nearby words ("call", "fax", "email") raise confidence; id-context ("order", "sku", "uuid") suppresses it. | Ambiguous digit runs masked with no signal. |
| 5 | **Overlap & precedence resolution, with PROTECTED non-PII spans.** Structured identifiers (UUIDs, record ids, URLs) are recognized *first* and PII detectors may not overlap them. | Exactly the reported bug. |
| 6 | **Allow / deny lists.** Known-safe terms never mask; known-sensitive always do. | Recurring domain-specific mistakes. |
| 7 | **Proven by an evaluation set (precision / recall / F1), regression-gated.** Dependability is *measured*, not asserted. | Regressions; "it felt better" tuning. |

Validators used in production, per type:

- **Phone** → `phonenumbers` (Google's libphonenumber port — the exact library
  Presidio's `PhoneRecognizer` uses). A candidate is a phone only if the library
  parses it as a *valid, possible* number for a region.
- **Email** → RFC-shaped + must be a whole token (not embedded).
- **Credit card** → Luhn checksum. **IBAN / SSN / national id** → their checksums.
- **UUID / record id / URL** → recognized as **protected**, never masked.

The reversible detect → tokenize → restore flow we already implement (Option C)
is the right top-level design; the gap is purely in **detection quality**.

---

## 2. Proof the approach works (runnable, no models needed)

A before/after harness on a gold-labelled set — real phones that *must* mask vs
identifier "traps" that *must not*. Run:

```
.venv/bin/python scripts/proof_phone_guard.py
```

Result:

```
## CURRENT (regex only)
   real phones detected : 4/4   (recall)
   FALSE positives on IDs: 4/5      ← includes the reported UUID bug

## PROPOSED (guarded: protected-span + context + validation)
   real phones detected : 4/4   (recall)
   FALSE positives on IDs: 0/5
```

Same recall on real PII, **false positives eliminated**. The two mechanisms that
did it — *protected-span exclusion* (principle 5) and *context gating* (principle
4) — are exactly the dependable-system techniques above, not bug-specific hacks.
Adding `phonenumbers` (principle 1) closes the residual dash-ambiguous cases
(e.g. distinguishing the SKU `99-8454-737` from a real US number by validation
rather than shape).

---

## 3. Applying it to this codebase

Detection lives in `pipelines/ner_pipeline.py`; the masking layer
(`agent/masking/`) already consumes its entities and is unchanged by this work.

**Layer 0 — Protected spans (new).** Compute spans for UUIDs, record ids, and
URLs up front. Exposed as a helper and threaded through post-processing so *any*
detector (regex **and** GLiNER) that overlaps a protected span is dropped. This
is the general fix; the reported bug is one instance of it.

**Layer 1 — Validating recognizers (rework the regex extractors).**
- `extract_phones`: candidate regex → exclude protected-overlap → `phonenumbers`
  validate (`is_valid_number`/`is_possible_number`) → context gate for
  unformatted runs.
- `extract_emails`: require whole-token boundaries; reject inside larger tokens.
- Add Luhn for any credit-card pattern.

**Layer 2 — Context + thresholds (extend what exists).** Keep the per-label
GLiNER `LABEL_THRESHOLDS`; add `PHONE_CONTEXT` / `ID_CONTEXT` windows for numeric
ambiguity.

**Layer 3 — Overlap & precedence (extend `deduplicate_with_containment`).**
Generalise to cross-label resolution: protected spans win over everything; then
higher-confidence / longer spans win.

**Masking-layer defense-in-depth (`agent/masking/session_map.py`).** Already only
masks `person/company/email/phone/location` (ids excluded). Add a final guard so
the map refuses to register a value that matches the UUID/id shape, even if a
detector mistakenly emits one — masking quality never depends on a single layer.

**Evaluation harness (new, the durable part).** Promote the proof script into
`tests/test_ner_reliability.py` with a labelled corpus (real PII + trap ids,
dates, SKUs, versions). Assert **0 false positives on the trap set** and a recall
floor per entity. This gates every future change — the mechanism that keeps the
system dependable instead of regressing.

---

## 4. Phasing

1. **Layer 0 + phone rework + harness** — kills the reported bug class; proof is
   the failing→passing trap test. (`phonenumbers` added to `requirements.txt`.)
2. **Email/card validation + cross-label overlap resolution.**
3. **Context scoring + per-entity threshold tuning against the corpus.**
4. **Masking-layer id guard (defense-in-depth) + CI gate on the harness.**

## 5. Dependency

`phonenumbers` (pure-Python libphonenumber port; the validator Presidio itself
uses). Added to `requirements.txt`. Everything else uses the stdlib.

---

## Sources

- [Presidio — Analyzer & decision process](https://microsoft.github.io/presidio/analyzer/) ·
  [decision process](https://microsoft.github.io/presidio/analyzer/decision_process/)
- [Presidio — customizing recognizers (regex + validation + context)](https://microsoft.github.io/presidio/samples/python/customizing_presidio_analyzer/)
- [Preventing PII leakage when using LLMs (Presidio)](https://ploomber.io/blog/presidio/)
- [Microsoft — PII Shield: a privacy proxy for every LLM call](https://techcommunity.microsoft.com/blog/azuredevcommunityblog/introducing-pii-shield-a-privacy-proxy-for-every-llm-call/4514726)
- [Redacting PII before it hits the LLM (detect → tokenize → restore)](https://nirajranasinghe.medium.com/redacting-pii-before-it-hits-the-llm-0fe9507f05e0)
- [Skyflow — PII data privacy vault (tokenization, referential integrity)](https://www.skyflow.com/product/pii-data-privacy-vault)
- [PRvL: Quantifying LLM PII redaction capabilities (evaluation)](https://arxiv.org/html/2508.05545v1)
