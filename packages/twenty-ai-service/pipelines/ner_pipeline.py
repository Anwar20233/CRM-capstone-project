"""CRM entity-recognition pipeline.

Ported from CRM_NER_Pipeline_v3.ipynb (see ../notebooks/). Hybrid system:
  - GLiNER ensemble (large + medium, zero-shot) for person/company/job title/
    date/money/location/product
  - Regex extractors for email/phone/money/date
  - Context rules for competitors
followed by a 12-step post-processing chain.

Notebook-only code (IPython display, matplotlib, pandas tables, the embedded
benchmark blob) is intentionally dropped. The models are loaded once via
load_models() at service startup, never per request.
"""

import re
from collections import defaultdict

# GLiNER models are loaded lazily by load_models() and cached at module level so
# the ~1.3 GB of weights are read once per process, not per request.
_model_lg = None
_model_md = None


GLINER_LABELS = [
    "person", "company", "job title", "date",
    "money", "location", "product", "competitor",
]

LABEL_THRESHOLDS = {
    "person":     0.55,   # cuts single-word low-confidence names
    "company":    0.55,   # cuts email-fragment companies
    "job title":  0.50,   # cuts ambiguous single-word extractions
    "date":       0.35,   # low — GLiNER dates are generally reliable
    "money":      0.45,   # cuts vague financial terms
    "location":   0.50,   # moderate — avoids generic words
    "product":    0.50,   # raised from 0.30 in v1 — product was noisy
    "competitor": 0.35,   # low — competitor recall was the hardest to achieve
}


# ── Blocklists ──────────────────────────────────────────────────────────────
# Words that GLiNER hallucinates as company names
_COMPANY_PRONOUN_BLOCKLIST = {
    "we", "i", "they", "it", "our", "your", "their", "us", "you",
    "he", "she", "this", "that", "these", "those", "his", "her",
    "internal", "company", "the client", "them",
}

# Pronouns falsely tagged as person names
_PERSON_PRONOUN_BLOCKLIST = {
    "they", "she", "he", "i", "them", "hi", "we", "his", "her", "you",
    "someone", "anyone", "whoever", "nobody", "somebody",
}

# Generic words falsely tagged as locations
_LOCATION_GENERIC_WORDS = {
    "address", "location", "locations", "office", "offices",
    "site", "sites", "internal", "remote",
}

# Single generic words falsely tagged as products
_PRODUCT_SINGLE_BLOCKLIST = {
    "product", "pilot", "platform", "tool", "suite", "solution", "solutions",
    "software", "system", "systems", "module", "bundle", "package",
    "service", "services", "reporting",
}

# Phrases falsely tagged as job titles
_JOB_TITLE_NONTITLE_PHRASES = {
    "contact", "our team", "main contact", "follow-up", "support team",
    "the team", "our staff", "my team",
}
_JOB_TITLE_NONTITLE_CONTAINS = [
    "poc is", "main contact", "follow-up", "our team",
]


# ── Regex patterns ──────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b')

# Matches international formats: +971 50 123 4567 / 01-555-1234 / +1-800-555-0199
# plus a trailing extension (x4715 / ext. 12 / #6218) and a leading "(422)".
# Letter/digit look-arounds (not just digit) so a number wedged inside a larger
# token — e.g. the "1574306202" in record id "C1574306202Eb8e" — never matches.
_PHONE_RE = re.compile(
    r'(?<![0-9A-Za-z])(\+?(?:\(\d+\)[\s\-.]?)?(?:\d[\s\-.]?){6,14}\d'
    r'(?:\s?(?:x|ext\.?|#)\s?\d{1,7})?)(?![0-9A-Za-z])',
    re.I,
)

# Handles: $45,000  €1.2M  AED 500k  120,000 EGP  $4,500/month  six figures
_MONEY_PATTERNS = [
    re.compile(r'(?:[$€£¥])\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|K|M|B|million|billion))?(?:/\w+)*', re.I),
    re.compile(r'\d[\d,]*(?:\.\d+)?\s?(?:k|K)\b'),
    re.compile(r'(?:USD|EUR|GBP|AED|SAR|EGP|GHS|CHF|JPY|CNY)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|K|M|B|million|billion))?', re.I),
    re.compile(r'\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|AED|SAR|EGP|GHS)', re.I),
    re.compile(r'(?:approximately\s+)?\d+(?:\.\d+)?\s+million\s+(?:EGP|USD|EUR|GBP|AED|SAR)', re.I),
    re.compile(r'(?:six|seven|eight|nine)\s+figures', re.I),
    re.compile(r'\d+\s+grand\b', re.I),
]

# Handles: June 12th  Thursday, June 12th  Q3  this week  end of month  tomorrow
_MONTHS = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
_DATE_PATTERNS = [
    re.compile(rf'{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?', re.I),
    re.compile(rf'\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}(?:,?\s+\d{{4}})?', re.I),
    re.compile(r'\d{1,2}/\d{1,2}/\d{2,4}'),
    re.compile(r'Q[1-4]\s*(?:FY)?\s*\d{4}', re.I),
    re.compile(r'Q[1-4]\b', re.I),
    re.compile(r'(?:next|last|this|early next)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month|quarter|year)', re.I),
    re.compile(r'(?:end of|by end of)\s+(?:the\s+)?(?:month|year|week|quarter|fiscal year|Friday|March|April|May|June|July|August|September|October|November|December|January|February|Q[1-4])', re.I),
    re.compile(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)', re.I),
    re.compile(r'tomorrow|yesterday', re.I),
    re.compile(rf'(?:next|last)\s+{_MONTHS}', re.I),
]

# Context signals: "comparing you against X", "switching from X", "vs X"
_COMP_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'compar(?:ing|ed|e)?\s+(?:you\s+)?(?:against|with|to)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:[,\n\.]|$| and )',
        r'evaluat(?:ing|ed|e)?\s+(?:your\s+)?(?:product\s+)?(?:against\s+)?(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:\s+(?:and|as|but|,)|$)',
        r'(?:switch(?:ed|ing)?|mov(?:ed?|ing)|migrat(?:ed?|ing))\s+(?:away\s+)?from\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:\s+to\b)',
        r'(?:went|go(?:ing)?|chose?)\s+with\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:[,.\n]|$)',
        r'(?:vs\.?|versus)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:[,.\n]|$)',
        r'(?:we\'ve\s+(?:been\s+)?using|we\s+(?:currently\s+)?use|used)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)\s+(?:for|and|but)',
        r'(?:alongside|alternative\s+to|instead\s+of|replace(?:ment)?(?:\s+for)?)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:[,.\n]|$| and )',
        r'(?:away\s+from)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:\s+and\s+(?P<name2>[A-Z][\w\s&.\-]{1,40}?))?',
        r'(?:they\s+(?:chose|went)\s+(?:with\s+)?)(?P<name>[A-Z][\w\s&.\-]{1,40}?)(?:[,.\n]|$)',
        r'(?:were?\s+with)\s+(?P<name>[A-Z][\w\s&.\-]{1,40}?)\s+(?:for|but)',
    ]
]

_KNOWN_COMPETITORS = {
    "salesforce", "hubspot", "zoho", "zoho crm", "pipedrive", "freshsales",
    "microsoft dynamics", "dynamics 365", "monday crm", "copper", "oracle", "sap",
    "salesforce sales cloud", "microsoft dynamics 365 sales", "sugar crm", "fusion crm",
}

_PRODUCT_SIGNALS = re.compile(
    r'\b(?:plan|tier|package|subscription|module|suite|add[\-\s]?on|upgrade|downgrade|'
    r'version|edition|bundle|platform|tool|product)\b', re.I
)


# ── Protected (structured NON-PII) spans ──────────────────────────────────────
# These are identifiers the agent must keep verbatim (record ids, UUIDs, URLs).
# They are recognised FIRST so no PII recogniser can mask a chunk inside them —
# the root-cause fix for "a record id had a phone number masked in the middle".
_URL_RE = re.compile(r'\bhttps?://[^\s,;<>"\']+|\bwww\.[^\s,;<>"\']+', re.I)
_UUID_RE = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)
# A record id is an alphanumeric token (len >= 6) containing BOTH a letter and a
# digit — e.g. "dE014d010c7ab0c", "INV00123" — optionally hyphen-segmented.
_RECORD_ID_RE = re.compile(
    r'\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}'
    r'(?:-[A-Za-z0-9]+)*\b'
)

# Context windows for numeric ambiguity (phone vs id/date).
_PHONE_CONTEXT = re.compile(
    r'\b(call|phone|tel|telephone|mobile|cell|fax|whatsapp|dial|contact)\b', re.I
)
_ID_CONTEXT = re.compile(
    r'\b(id|uuid|order|invoice|sku|ref|reference|ticket|account|index|customer ?id)\b',
    re.I,
)
# A date-shaped token must never be taken as a phone number.
_DATE_LIKE = re.compile(
    r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$'
)


def _spans_overlap(start, end, spans):
    return any(not (end <= s0 or start >= s1) for s0, s1 in spans)


def _phone_is_possible(candidate):
    """Length/structure plausibility via libphonenumber, when available.

    Uses ``is_possible_number`` (not ``is_valid_number``): we want to keep real,
    oddly-formatted or synthetic numbers while rejecting random digit runs.
    Degrades to ``None`` (unknown) if the library is missing.
    """
    try:
        import phonenumbers
    except ImportError:
        return None
    for region in ("US", None):
        try:
            parsed = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_possible_number(parsed):
            return True
    return False


# ── Model loading ───────────────────────────────────────────────────────────
def load_models():
    """Load both GLiNER models into the module-level cache. Call once at startup."""
    global _model_lg, _model_md

    if _model_lg is not None and _model_md is not None:
        return

    from gliner import GLiNER

    _model_lg = GLiNER.from_pretrained("urchade/gliner_large-v2.1")
    _model_md = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")


def models_loaded():
    return _model_lg is not None and _model_md is not None


# ── Post-processing chain (12 steps) ─────────────────────────────────────────
# Step 1 — Per-label confidence threshold
def apply_label_thresholds(entities):
    return [e for e in entities
            if e.get("score", 1.0) >= LABEL_THRESHOLDS.get(e["label"], 0.4)]


# Step 2 — Exact deduplication: same (label, text) → keep first occurrence
def deduplicate(entities):
    seen, result = set(), []
    for e in entities:
        key = (e["label"], e["text"].lower().strip())
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


# Step 3 — Merge adjacent spans of the same label
# Example: "James" at pos 5 + "Whitfield" at pos 11 → "James Whitfield"
def merge_adjacent_spans(entities, text):
    if not entities:
        return entities
    positioned = []
    for e in entities:
        idx = text.lower().find(e["text"].lower())
        positioned.append((idx, e))
    positioned.sort(key=lambda x: (x[1]["label"], x[0]))
    merged = []
    i = 0
    while i < len(positioned):
        pos, ent = positioned[i]
        if pos < 0:
            merged.append(ent)
            i += 1
            continue
        j = i + 1
        while j < len(positioned):
            pos2, ent2 = positioned[j]
            if ent2["label"] != ent["label"] or pos2 < 0:
                break
            gap = pos2 - (pos + len(ent["text"]))
            if 0 <= gap <= 4:
                combined = text[pos:pos2 + len(ent2["text"])]
                ent = {"label": ent["label"], "text": combined,
                       "score": min(ent.get("score", 1.0), ent2.get("score", 1.0))}
                j += 1
            else:
                break
        merged.append(ent)
        i = j
    return merged


# Step 4 — Cross-entity validation
def cross_entity_validate(entities):
    email_locals, email_domain_bases = set(), set()
    for e in entities:
        if e["label"] == "email address" and "@" in e["text"]:
            local, domain = e["text"].lower().split("@", 1)
            email_locals.add(local)
            email_domain_bases.add(domain.split(".")[0])
    person_texts = {e["text"].lower() for e in entities if e["label"] == "person"}
    result = []
    for e in entities:
        if e["label"] == "person" and e["text"].lower() in email_locals:
            continue
        if e["label"] == "company":
            tl = e["text"].lower().strip()
            if tl in email_locals or tl in email_domain_bases:
                continue
            if any(tl in p for p in person_texts):
                continue
        result.append(e)
    return result


# Step 5 — Product context filter
def filter_products_by_context(entities, text):
    if not _PRODUCT_SIGNALS.search(text):
        return [e for e in entities if e["label"] != "product"]
    result = []
    for e in entities:
        if e["label"] != "product":
            result.append(e)
            continue
        idx = text.lower().find(e["text"].lower())
        if idx < 0:
            result.append(e)
            continue
        window = text[max(0, idx - 60):idx + len(e["text"]) + 60].lower()
        if _PRODUCT_SIGNALS.search(window) or e.get("score", 1.0) >= 0.6:
            result.append(e)
    return result


# Step 6 — Pronoun company filter
def filter_pronoun_companies(entities):
    return [e for e in entities
            if not (e["label"] == "company"
                    and e["text"].lower().strip() in _COMPANY_PRONOUN_BLOCKLIST)]


# Step 7 — Person pronoun filter
def filter_person_pronouns(entities):
    result = []
    for e in entities:
        if e["label"] == "person":
            words = e["text"].strip().split()
            if len(words) == 1 and e["text"].lower() in _PERSON_PRONOUN_BLOCKLIST:
                continue
        result.append(e)
    return result


# Step 8 — Money word filter (drop money with no digits and no currency symbol)
def filter_money_no_digits(entities):
    _symbols = set('$€£¥')
    result = []
    for e in entities:
        if e["label"] == "money":
            if (not any(c.isdigit() for c in e["text"])
                    and not any(c in _symbols for c in e["text"])):
                continue
        result.append(e)
    return result


# Step 9 — Location refined filter
def filter_location_refined(entities):
    result = []
    for e in entities:
        if e["label"] == "location":
            text = e["text"].strip()
            tl = text.lower()
            digits = sum(1 for c in text if c.isdigit())
            if len(text) > 0 and digits / len(text) > 0.40:
                continue
            if tl in _LOCATION_GENERIC_WORDS:
                continue
            words = tl.split()
            if (len(words) == 2
                    and words[0].isdigit()
                    and words[1].rstrip('s') in _LOCATION_GENERIC_WORDS):
                continue
        result.append(e)
    return result


# Step 10 — Job title non-phrase filter
def filter_job_title_nontitles(entities):
    result = []
    for e in entities:
        if e["label"] == "job title":
            tl = e["text"].lower().strip()
            if tl in _JOB_TITLE_NONTITLE_PHRASES:
                continue
            if any(phrase in tl for phrase in _JOB_TITLE_NONTITLE_CONTAINS):
                continue
        result.append(e)
    return result


# Step 11 — Product single-word filter
def filter_product_single_generic(entities):
    result = []
    for e in entities:
        if e["label"] == "product":
            words = e["text"].strip().split()
            if len(words) == 1 and words[0].lower() in _PRODUCT_SINGLE_BLOCKLIST:
                continue
        result.append(e)
    return result


# Step 12 — Containment deduplication (drop spans contained in a longer span)
def deduplicate_with_containment(entities):
    by_label = defaultdict(list)
    for e in entities:
        by_label[e["label"]].append(e)
    result = []
    for label, ents in by_label.items():
        ents_sorted = sorted(ents, key=lambda x: len(x["text"]), reverse=True)
        kept = []
        for e in ents_sorted:
            tl = e["text"].lower().strip()
            covered = any(
                tl in k["text"].lower().strip() and tl != k["text"].lower().strip()
                for k in kept
            )
            if not covered:
                kept.append(e)
        result.extend(kept)
    return result


# ── Regex / rule extractors ───────────────────────────────────────────────────
def extract_emails(text):
    return [{"label": "email address", "text": m.group(), "score": 1.0}
            for m in _EMAIL_RE.finditer(text)]


def extract_phones(text):
    """Extract phone numbers as *validated candidates*, not bare regex hits.

    A candidate is accepted only when it is phone-plausible (libphonenumber) or
    clearly phone-formatted, is in the 7–15 digit range, is not date-shaped, and
    is not sitting in id-context ("order", "invoice", …). Bare digit runs with no
    formatting and no phone-context are rejected — those are ids, not phones.
    """
    res, seen = [], set()
    for m in _PHONE_RE.finditer(text):
        candidate = m.group().strip()
        if candidate in seen:
            continue
        if _is_plausible_phone(candidate, text, m.start(), m.end()):
            res.append({"label": "phone number", "text": candidate, "score": 1.0})
            seen.add(candidate)
    return res


_EXT_RE = re.compile(r'\s?(?:x|ext\.?|#)\s?\d{1,7}$', re.I)


def _is_plausible_phone(candidate, text, start, end):
    if _DATE_LIKE.match(candidate):
        return False

    # Validate the base number (E.164 is ≤ 15 digits); an extension adds more.
    base = _EXT_RE.sub('', candidate).strip()
    base_digits = re.sub(r'\D', '', base)
    if not (7 <= len(base_digits) <= 15):
        return False

    window = text[max(0, start - 20):end + 20]
    if _ID_CONTEXT.search(window):
        return False

    has_extension = candidate != base
    has_format = (
        '+' in candidate
        or has_extension
        or bool(re.search(r'[()/.\s]', candidate))
        or base.count('-') >= 1
    )
    has_phone_context = bool(_PHONE_CONTEXT.search(window))

    possible = _phone_is_possible(candidate)
    if possible is None:
        possible = _phone_is_possible(base)

    if has_format or has_phone_context:
        # Formatted, or a human flagged it with "phone"/"call": trust it unless
        # libphonenumber is confident it's impossible *and* it's unformatted.
        return has_format or possible is not False or len(base_digits) >= 10

    # Bare digit run, no context: accept only a plausible 10–15 digit number.
    # (Masking is reversible, so a mislabelled order id is recoverable; the
    # harmful case — masking a chunk *inside* an id — is handled by protected
    # spans, not here.)
    return possible is True and 10 <= len(base_digits) <= 15


def extract_money(text):
    res, seen = [], set()
    for pat in _MONEY_PATTERNS:
        for m in pat.finditer(text):
            t = m.group().strip()
            if t not in seen:
                res.append({"label": "money", "text": t, "score": 0.95})
                seen.add(t)
    return res


def extract_dates(text):
    res, seen = [], set()
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            t = m.group().strip()
            if t not in seen and len(t) > 2:
                res.append({"label": "date", "text": t, "score": 0.90})
                seen.add(t)
    return res


def extract_competitors(text, gliner_ents):
    companies = {e["text"].lower().strip() for e in gliner_ents
                 if e["label"] in ("company", "competitor")}
    found, res = set(), []

    def add(name):
        name = name.strip().rstrip(".,;")
        if len(name) < 2 or name.lower() in found:
            return
        found.add(name.lower())
        res.append({"label": "competitor", "text": name, "score": 0.85})

    for pat in _COMP_PATTERNS:
        for m in pat.finditer(text):
            for gname in ["name", "name2"]:
                try:
                    c = m.group(gname)
                    if c:
                        c = c.strip().rstrip(" .,;")
                        if c.lower() in companies or any(k in c.lower() for k in _KNOWN_COMPETITORS):
                            add(c)
                except IndexError:
                    pass
    text_lower = text.lower()
    for kw in _KNOWN_COMPETITORS:
        if kw in text_lower:
            idx = text_lower.find(kw)
            add(text[idx:idx + len(kw)])
    return res


# ── Offsets ───────────────────────────────────────────────────────────────────
def add_offsets(entities, text):
    """Attach character start/end to each entity.

    The notebook output only carries the matched substring. Offsets make the
    backend's masked-text replacement robust to a string appearing more than
    once. When the same text occurs multiple times we hand out successive
    occurrences so distinct entities don't collapse onto one position.
    """
    text_lower = text.lower()
    cursor_by_text = {}
    result = []
    for e in entities:
        needle = e["text"].lower()
        start_from = cursor_by_text.get(needle, 0)
        idx = text_lower.find(needle, start_from)
        if idx < 0:
            # Fall back to the first occurrence (e.g. merged/normalized spans).
            idx = text_lower.find(needle)
        if idx >= 0:
            cursor_by_text[needle] = idx + len(needle)
            result.append({**e, "start": idx, "end": idx + len(e["text"])})
        else:
            result.append({**e, "start": None, "end": None})
    return result


def filter_protected_spans(entities, text):
    """Drop any entity that sits inside a structured identifier (URL/UUID/id).

    Phone and email entities are *trusted* — they carve out their own spans so a
    phone like ``846-790-4623x4715`` (which superficially looks like a record
    id) is never reclassified. Every other recogniser (GLiNER persons/companies,
    etc.) is rejected when it overlaps a protected span.
    """
    trusted = [
        (e["start"], e["end"])
        for e in entities
        if e["label"] in ("email address", "phone number") and e.get("start") is not None
    ]

    protected = []
    for regex in (_URL_RE, _UUID_RE, _RECORD_ID_RE):
        for match in regex.finditer(text):
            span = match.span()
            if not _spans_overlap(span[0], span[1], trusted):
                protected.append(span)

    if not protected:
        return entities

    kept = []
    for entity in entities:
        start, end = entity.get("start"), entity.get("end")
        is_trusted = entity["label"] in ("email address", "phone number")
        if start is not None and not is_trusted and _spans_overlap(start, end, protected):
            continue
        kept.append(entity)
    return kept


# ── Main pipeline ───────────────────────────────────────────────────────────
def extract(text):
    """Run the full CRM entity extraction pipeline on a single text.

    Returns a list of entity dicts: {label, text, score, start, end}.
    """
    if not models_loaded():
        raise RuntimeError("GLiNER models are not loaded — call load_models() first")

    # 1. Ensemble GLiNER — both models at low threshold (post-process will filter)
    ents_lg = _model_lg.predict_entities(text, GLINER_LABELS, threshold=0.30)
    ents_md = _model_md.predict_entities(text, GLINER_LABELS, threshold=0.30)
    gliner_raw = ents_lg + ents_md

    # 2. Per-label threshold
    gliner_filtered = apply_label_thresholds(gliner_raw)
    # Strip email/phone/competitor — handled by regex/rules
    gliner_core = [e for e in gliner_filtered
                   if e["label"] not in ("email address", "phone number", "competitor")]

    # 3. Regex layers
    emails = extract_emails(text)
    phones = extract_phones(text)
    money_r = extract_money(text)
    dates_r = extract_dates(text)
    comp = extract_competitors(text, gliner_filtered)

    # 4. Combine everything
    combined = gliner_core + emails + phones + money_r + dates_r + comp

    # 5–12. Post-processing chain
    combined = deduplicate(combined)
    combined = merge_adjacent_spans(combined, text)
    combined = cross_entity_validate(combined)
    combined = filter_products_by_context(combined, text)
    combined = filter_pronoun_companies(combined)
    combined = filter_person_pronouns(combined)
    combined = filter_money_no_digits(combined)
    combined = filter_location_refined(combined)
    combined = filter_job_title_nontitles(combined)
    combined = filter_product_single_generic(combined)
    combined = deduplicate_with_containment(combined)
    extracted = deduplicate(combined)

    # 13. Offsets, then drop anything inside a protected identifier (URL/UUID/id).
    with_offsets = add_offsets(extracted, text)
    return filter_protected_spans(with_offsets, text)
