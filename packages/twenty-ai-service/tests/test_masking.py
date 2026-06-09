"""Tests for the PII masking layer (PIISessionMap) and its BaseWorker wiring.

Verifies:
- Token registration: sequential, idempotent, label-filtered.
- mask_text / unmask_text round-trips with an injected NER stub.
- Recursive mask_value / unmask_value over nested dicts and lists.
- Longest-first replacement so substrings don't corrupt longer values.
- Cross-call consistency (the same value keeps its token across payloads).
- BaseWorker enables masking by default and honours mask_pii=False.
"""

import pytest

from agent.masking import PIISessionMap
from agent.tool_scope import READER_SCOPE
from agent.workers.base_worker import BaseWorker


# A deterministic NER stub: matches a fixed set of values so tests never load
# the GLiNER models. Returns the same {label, text} shape as pipelines.extract.
_KNOWN_ENTITIES = [
    ("person", "John Doe"),
    ("person", "Alice Cooper"),
    ("company", "Acme Corp"),
    ("company", "Acme"),
    ("email address", "john@acme.com"),
]


def fake_extractor(text: str) -> list[dict]:
    return [
        {"label": label, "text": value}
        for label, value in _KNOWN_ENTITIES
        if value in text
    ]


@pytest.fixture
def session_map() -> PIISessionMap:
    return PIISessionMap(extractor=fake_extractor)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_sequential_tokens_per_prefix(self, session_map: PIISessionMap) -> None:
        assert session_map.register("person", "John Doe") == "[PERSON_1]"
        assert session_map.register("person", "Alice Cooper") == "[PERSON_2]"
        assert session_map.register("company", "Acme Corp") == "[COMPANY_1]"

    def test_registration_is_idempotent(self, session_map: PIISessionMap) -> None:
        first = session_map.register("person", "John Doe")
        second = session_map.register("person", "John Doe")
        assert first == second == "[PERSON_1]"
        assert len(session_map) == 1

    def test_non_maskable_label_returns_none(self, session_map: PIISessionMap) -> None:
        # money / date / job title are intentionally left visible to the model.
        assert session_map.register("money", "$45,000") is None
        assert session_map.register("date", "next Friday") is None
        assert len(session_map) == 0

    def test_empty_value_returns_none(self, session_map: PIISessionMap) -> None:
        assert session_map.register("person", "   ") is None
        assert len(session_map) == 0


# ---------------------------------------------------------------------------
# Text masking / unmasking
# ---------------------------------------------------------------------------

class TestText:
    def test_mask_then_unmask_roundtrip(self, session_map: PIISessionMap) -> None:
        masked = session_map.mask_text("Email John Doe at john@acme.com")
        assert "John Doe" not in masked
        assert "john@acme.com" not in masked
        assert "[PERSON_1]" in masked
        assert "[EMAIL_1]" in masked

        restored = session_map.unmask_text(masked)
        assert restored == "Email John Doe at john@acme.com"

    def test_longest_value_wins(self, session_map: PIISessionMap) -> None:
        # Both "Acme Corp" and "Acme" are entities; the longer must not be
        # corrupted by the shorter one's replacement.
        masked = session_map.mask_text("Acme Corp is a company")
        assert masked == "[COMPANY_1] is a company"

    def test_unknown_token_left_intact(self, session_map: PIISessionMap) -> None:
        # The model may hallucinate a token that was never registered.
        assert session_map.unmask_text("ping [PERSON_99]") == "ping [PERSON_99]"

    def test_consistency_across_calls(self, session_map: PIISessionMap) -> None:
        first = session_map.mask_text("call John Doe")
        second = session_map.mask_text("John Doe again")
        assert "[PERSON_1]" in first
        assert "[PERSON_1]" in second

    def test_non_string_passthrough(self, session_map: PIISessionMap) -> None:
        assert session_map.mask_text("") == ""
        assert session_map.unmask_text("no tokens here") == "no tokens here"


# ---------------------------------------------------------------------------
# Recursive value masking (tool args / tool results)
# ---------------------------------------------------------------------------

class TestValue:
    def test_mask_nested_tool_result(self, session_map: PIISessionMap) -> None:
        result = {
            "ok": True,
            "data": [{"title": "Follow up with John Doe", "amount": 100}],
        }
        masked = session_map.mask_value(result)
        assert masked["ok"] is True
        assert masked["data"][0]["title"] == "Follow up with [PERSON_1]"
        # Non-string leaves are untouched.
        assert masked["data"][0]["amount"] == 100

    def test_unmask_tool_args(self, session_map: PIISessionMap) -> None:
        # Register first (as if the prompt mentioned the name).
        session_map.mask_text("John Doe")
        args = {"tool": "find_people", "tool_args": {"name": "[PERSON_1]"}}
        unmasked = session_map.unmask_value(args)
        assert unmasked["tool_args"]["name"] == "John Doe"

    def test_value_roundtrip(self, session_map: PIISessionMap) -> None:
        payload = {"records": ["John Doe", "Alice Cooper"]}
        masked = session_map.mask_value(payload)
        assert masked == {"records": ["[PERSON_1]", "[PERSON_2]"]}
        assert session_map.unmask_value(masked) == payload


# ---------------------------------------------------------------------------
# Default extractor degradation
# ---------------------------------------------------------------------------

class TestDefaultExtractor:
    def test_no_models_skips_ner(self) -> None:
        # With models unloaded, the default extractor returns nothing rather
        # than raising — known values still mask, discovery just pauses.
        plain = PIISessionMap()
        assert plain.mask_text("Email John Doe") == "Email John Doe"


# ---------------------------------------------------------------------------
# Priming: rebuild the map from a chat's stored (unmasked) history
# ---------------------------------------------------------------------------

class TestPriming:
    def test_reopened_chat_recovers_same_tokens(self) -> None:
        # Two independent maps primed from the same history must agree — that is
        # what lets a reopened chat keep its tokens without persisting anything.
        history = ["Met John Doe at Acme Corp", "Alice Cooper joined later"]

        first = PIISessionMap(extractor=fake_extractor)
        first.prime(history)
        second = PIISessionMap(extractor=fake_extractor)
        second.prime(history)

        assert first.mapping == second.mapping
        assert first.mapping["[PERSON_1]"] == "John Doe"

    def test_new_message_reuses_primed_tokens(self) -> None:
        session = PIISessionMap(extractor=fake_extractor)
        session.prime(["Met John Doe"])
        # A new turn mentioning the same person must reuse the existing token.
        assert "[PERSON_1]" in session.mask_text("ping John Doe again")

    def test_new_entity_continues_numbering(self) -> None:
        session = PIISessionMap(extractor=fake_extractor)
        session.prime(["Met John Doe"])  # [PERSON_1]
        masked = session.mask_text("now add Alice Cooper")
        assert masked == "now add [PERSON_2]"


# ---------------------------------------------------------------------------
# Alias resolution: case-insensitive, partial names, ambiguity, variants
# ---------------------------------------------------------------------------

# An extractor that finds any of a fixed vocabulary, case-insensitively, and
# reports offsets — closer to how the real pipeline behaves.
def _vocab_extractor(vocab: list[tuple[str, str]]):
    def extractor(text: str) -> list[dict]:
        lowered = text.lower()
        out = []
        for label, value in vocab:
            index = lowered.find(value.lower())
            if index >= 0:
                out.append({"label": label, "text": text[index:index + len(value)],
                            "start": index})
        return out
    return extractor


class TestAliasResolution:
    def test_case_insensitive_same_token(self) -> None:
        session = PIISessionMap(extractor=_vocab_extractor([("person", "John Doe")]))
        session.mask_text("John Doe arrived")
        masked = session.mask_text("later john doe and JOHN DOE came")
        assert masked == "later [PERSON_1] and [PERSON_1] came"
        assert len(session) == 1

    def test_partial_name_resolves_when_unambiguous(self) -> None:
        session = PIISessionMap(
            extractor=_vocab_extractor([("person", "John Doe"), ("person", "John")])
        )
        session.mask_text("Met John Doe")  # [PERSON_1]
        assert "[PERSON_1]" in session.mask_text("John says hi")
        assert len(session) == 1

    def test_ambiguous_partial_is_not_merged(self) -> None:
        session = PIISessionMap(
            extractor=_vocab_extractor(
                [("person", "John Doe"), ("person", "John Smith"), ("person", "John")]
            )
        )
        session.mask_text("John Doe and John Smith met")  # P1, P2
        masked = session.mask_text("then John spoke")
        # "John" can't be attributed to either → its own token, not P1/P2.
        assert "[PERSON_1]" not in masked and "[PERSON_2]" not in masked
        assert "[PERSON_3]" in masked

    def test_company_variant_resolves_and_unmask_is_fullest(self) -> None:
        session = PIISessionMap(
            extractor=_vocab_extractor(
                [("company", "Acme Corporation"), ("company", "Acme")]
            )
        )
        session.mask_text("Acme Corporation grew")  # [COMPANY_1]
        assert "[COMPANY_1]" in session.mask_text("Acme is hiring")
        # Unmask expands to the fullest known surface, not the partial.
        assert session.unmask_text("[COMPANY_1] wins") == "Acme Corporation wins"

    def test_lowercase_person_is_capitalized(self) -> None:
        session = PIISessionMap(extractor=_vocab_extractor([("person", "john doe")]))
        # The value the model/user typed is lower-case; the token must unmask to
        # a properly-cased name so writes/reads hit the CRM consistently.
        token = session.mask_text("add john doe").split()[-1]
        assert session.unmask_text(token) == "John Doe"

    def test_lowercase_company_is_capitalized(self) -> None:
        session = PIISessionMap(extractor=_vocab_extractor([("company", "acme corp")]))
        masked = session.mask_text("at acme corp")
        assert session.unmask_text(masked) == "at Acme Corp"

    def test_existing_internal_casing_preserved(self) -> None:
        # Only a leading lower-case letter is fixed; internal caps stay intact.
        session = PIISessionMap(extractor=_vocab_extractor([("person", "John McDonald")]))
        masked = session.mask_text("call John McDonald")
        assert session.unmask_text(masked) == "call John McDonald"

    def test_email_not_capitalized(self) -> None:
        # Emails must stay verbatim — capitalizing would corrupt the address.
        session = PIISessionMap(extractor=_vocab_extractor([("email address", "john@acme.com")]))
        masked = session.mask_text("mail john@acme.com")
        assert session.unmask_text(masked) == "mail john@acme.com"

    def test_case_variants_share_capitalized_token(self) -> None:
        # Lower- and mixed-case mentions collapse to one token whose canonical
        # is properly cased regardless of which surface appeared first.
        session = PIISessionMap(extractor=_vocab_extractor([("person", "john doe")]))
        session.mask_text("john doe")
        masked = session.mask_text("JOHN DOE and john doe")
        assert masked == "[PERSON_1] and [PERSON_1]"
        assert session.unmask_text("[PERSON_1]") == "John Doe"

    def test_full_roundtrip_is_faithful(self) -> None:
        session = PIISessionMap(
            extractor=_vocab_extractor(
                [("person", "John Doe"), ("company", "Acme"), ("email address", "a@b.com")]
            )
        )
        original = "John Doe at Acme, a@b.com"
        masked = session.mask_text(original)
        assert "John Doe" not in masked and "a@b.com" not in masked
        assert session.unmask_text(masked) == original


# ---------------------------------------------------------------------------
# BaseWorker wiring
# ---------------------------------------------------------------------------

class TestWorkerWiring:
    def test_masking_on_by_default(self) -> None:
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="read-only")
        assert isinstance(worker.pii_map, PIISessionMap)

    def test_masking_can_be_disabled(self) -> None:
        worker = BaseWorker(
            scope=READER_SCOPE, system_prompt="read-only", mask_pii=False
        )
        assert worker.pii_map is None

    def test_shared_map_is_used(self) -> None:
        shared = PIISessionMap(extractor=fake_extractor)
        worker = BaseWorker(
            scope=READER_SCOPE, system_prompt="read-only", pii_map=shared
        )
        assert worker.pii_map is shared
