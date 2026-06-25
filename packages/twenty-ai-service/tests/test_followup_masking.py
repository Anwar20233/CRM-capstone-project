"""Unit tests for the Follow-Up PII masker.

A fake NER extractor stands in for Presidio so these run without the heavy
models: it tags a configured set of names as ``person`` spans. The suite covers
realistic surfaces the pipeline actually masks — inbound emails (signatures, CC
lists, multiple addresses, quoted threads), connection/relationship descriptions,
profile synthesis round-trips — plus the edge cases that make masking safe
(substrings, repeated/duplicate names, capitalization, ids and titles preserved).
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from followup.profile.masking import ProfileMasker
from followup.profile.schemas import ContactSummary
from followup.profile.synthesis import synthesize_profile
from followup.store.repositories import ProfileFact


# ===========================================================================
# Helpers
# ===========================================================================


def _extractor(person_names: list[str]) -> Callable[[str], list[dict[str, Any]]]:
    """Fake NER: tag each configured name as a person span wherever it occurs."""

    def extract(text: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for name in person_names:
            start = 0
            while (start := text.find(name, start)) != -1:
                spans.append({"label": "person", "text": name, "start": start, "end": start + len(name)})
                start += len(name)
        return spans

    return extract


def _masker(discovered: list[str] | None = None) -> ProfileMasker:
    return ProfileMasker(extractor=_extractor(discovered or []))


def _build(model: type, data: dict[str, Any], **defaults: Any) -> Any:
    row = {"id": uuid.uuid4(), **defaults, **data}
    names = {f.name for f in fields(model)}
    return model(**{k: v for k, v in row.items() if k in names})


class RecordingChat:
    """Fake chat model: records the messages it received, returns canned prose."""

    def __init__(self, narrative: str) -> None:
        self._narrative = narrative
        self.seen_text = ""

    async def ainvoke(self, messages):
        self.seen_text = " ".join(getattr(m, "content", "") for m in messages)
        return SimpleNamespace(content=self._narrative)


# ===========================================================================
# Inbound email bodies (the primary PII surface)
# ===========================================================================


def test_known_contact_name_and_email_masked_and_round_trip():
    masker = _masker().register(
        contacts=[{"id": "id1", "name": "John Park", "email": "john@acme.com"}]
    )
    masked = masker.mask("John Park can be reached at john@acme.com")

    assert "John" not in masked
    assert "john@acme.com" not in masked
    assert masker.unmask(masked) == "John Park can be reached at john@acme.com"


def test_first_name_mention_masks_to_same_handle_as_full_name():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    masked = masker.mask("John said yes; John Park signed.")

    assert "John" not in masked
    assert masker.unmask(masked) == "John Park said yes; John Park signed."


def test_multi_person_email_known_and_discovered():
    masker = _masker(discovered=["Rachel Torres", "David Kim"]).register(
        contacts=[
            {"id": "id1", "name": "John Park", "email": "john@acme.com"},
            {"id": "id2", "name": "Lisa Huang"},
        ]
    )
    body = (
        "Hi, John here. Lisa will loop in our new VP Rachel Torres and procurement "
        "lead David Kim before we sign."
    )
    masked = masker.mask(body)

    for name in ("John", "Lisa", "Rachel Torres", "David Kim"):
        assert name not in masked
    assert masker.unmask(masked) == (
        "Hi, John Park here. Lisa Huang will loop in our new VP Rachel Torres and "
        "procurement lead David Kim before we sign."
    )


def test_email_signature_block_with_multiple_addresses():
    masker = _masker(discovered=["Sarah Chen"]).register(
        contacts=[{"id": "id1", "name": "John Park", "email": "john@acme.com"}]
    )
    body = (
        "Thanks,\nJohn Park\njohn@acme.com | cc: sarah.chen@vendor.com\n"
        "Looping Sarah Chen for scheduling."
    )
    masked = masker.mask(body)

    assert "John Park" not in masked
    assert "Sarah Chen" not in masked
    assert "john@acme.com" not in masked
    assert "sarah.chen@vendor.com" not in masked
    # Two distinct emails → two distinct email handles.
    assert masker.unmask(masked) == body


def test_cc_list_with_several_people():
    masker = _masker(discovered=["Maria Santos", "Kevin Cho"]).register(contacts=[])
    body = "CC: Maria Santos, Kevin Cho. Both should review the SOW."
    masked = masker.mask(body)

    assert "Maria Santos" not in masked and "Kevin Cho" not in masked
    assert masker.unmask(masked) == body


def test_quoted_reply_thread_keeps_names_consistent():
    # The same person quoted twice (reply + original) must reuse one handle.
    masker = _masker(discovered=["Priya Sharma"]).register(contacts=[])
    body = "Priya Sharma wrote:\n> Priya Sharma here, can we move the date?"
    masked = masker.mask(body)

    handles = {token for token in masked.split() if token.startswith("person")}
    assert len(handles) == 1  # one person → one handle, even across the quote
    assert "Priya Sharma" not in masked


# ===========================================================================
# Connections / relationships (handles inside descriptions)
# ===========================================================================


def test_relationship_description_round_trips():
    masker = _masker().register(
        contacts=[{"id": "id1", "name": "John Park"}, {"id": "id2", "name": "Dana Reed"}]
    )
    masked = masker.mask("John Park reports to Dana Reed on technical approvals")
    items = [{"from_id": "crm_id1", "to_id": "crm_id2", "relationship_type": "reports_to", "description": masked}]

    masker.unmask_fields(items, ("description",))

    assert items[0]["description"] == "John Park reports to Dana Reed on technical approvals"
    assert items[0]["from_id"] == "crm_id1"  # id endpoints untouched
    assert items[0]["to_id"] == "crm_id2"


def test_unknown_persons_names_unmasked_for_resolution():
    # The extractor surfaces new people as handles; resolution needs real names.
    masker = _masker(discovered=["Rachel Torres"]).register(contacts=[])
    masked = masker.mask("Rachel Torres will sign off")
    handle = next(token for token in masked.split() if token.startswith("person"))
    persons = [{"name": handle, "apparent_role": "VP of Engineering", "likely_matches_shadow": "shadow_abc"}]

    masker.unmask_fields(persons, ("name", "apparent_role"))

    assert persons[0]["name"] == "Rachel Torres"
    assert persons[0]["likely_matches_shadow"] == "shadow_abc"  # label untouched


def test_unmask_fields_handles_missing_and_none_fields():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    items = [
        {"fact_value": masker.mask("John Park is in"), "source_snippet": None},
        {"fact_value": "no handles here"},  # missing source_snippet
    ]

    masker.unmask_fields(items, ("fact_value", "source_snippet"))

    assert items[0]["fact_value"] == "John Park is in"
    assert items[0]["source_snippet"] is None
    assert items[1]["fact_value"] == "no handles here"


# ===========================================================================
# Profile synthesis (read path) — mask in, unmask the briefing out
# ===========================================================================


async def test_synthesis_masks_prompt_and_unmasks_narrative():
    contacts = [ContactSummary(crm_id="id1", name="John Park", role="Eng Manager", email="john@acme.com", facts=[])]
    deal = {"name": "Acme Expansion", "stage": "PROPOSAL", "value": 50000.0}
    facts = [
        _build(
            ProfileFact,
            {"opportunity_id": uuid.uuid4(), "entity_type": "person", "entity_crm_id": uuid.uuid4(),
             "fact_type": "concern", "fact_value": "timeline risk", "source_type": "email"},
            extracted_at=datetime.now(timezone.utc),
        )
    ]
    # The model only ever sees handles, so it can only answer in handles.
    chat = RecordingChat("Person001 has gone quiet; re-engage Person001 this week.")

    narrative = await synthesize_profile(
        deal=deal, company={"name": "Acme"}, contacts=contacts, shadows=[],
        facts=facts, relationships=[], risk_score=78.0, chat_llm=chat,
    )

    # Prompt the model received carried no real name...
    assert "John Park" not in chat.seen_text
    # ...and the returned briefing reads with the real name, no handle leaks.
    assert narrative == "John Park has gone quiet; re-engage John Park this week."


async def test_synthesis_unmasks_capitalized_handle_in_prose():
    contacts = [ContactSummary(crm_id="id1", name="Dana Reed", role=None, email=None, facts=[])]
    chat = RecordingChat("Person001 is the economic buyer.")  # sentence-start capital

    narrative = await synthesize_profile(
        deal={"name": "D", "stage": "NEW", "value": 0.0}, company=None, contacts=contacts,
        shadows=[], facts=[], relationships=[], risk_score=None, chat_llm=chat,
    )

    assert narrative == "Dana Reed is the economic buyer."


# ===========================================================================
# Scope: ids, titles, and company names stay visible
# ===========================================================================


def test_company_and_competitor_names_not_masked():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    masked = masker.mask("John Park is evaluating Segment at Airbnb against Datadog.")

    for keep in ("Segment", "Airbnb", "Datadog"):
        assert keep in masked
    assert "John" not in masked


def test_mask_known_preserves_ids_and_titles():
    masker = _masker().register(
        contacts=[{"id": "id1", "name": "John Park", "email": "john@acme.com"}]
    )
    block = "- id: crm_id1, name: John Park, role: Senior Engineering Manager, email: john@acme.com"
    masked = masker.mask_known(block)

    assert "crm_id1" in masked  # id untouched
    assert "Senior Engineering Manager" in masked  # job title is not PII
    assert "John Park" not in masked
    assert "john@acme.com" not in masked


def test_mask_known_does_not_run_ner():
    masker = _masker(discovered=["Rachel Torres"]).register(
        contacts=[{"id": "id1", "name": "John Park"}]
    )
    masked = masker.mask_known("John Park met Rachel Torres")

    assert "John" not in masked  # registered → masked
    assert "Rachel Torres" in masked  # unregistered, no NER → visible


# ===========================================================================
# Edge cases / robustness
# ===========================================================================


def test_distinct_people_same_first_name_get_distinct_handles():
    masker = _masker().register(
        contacts=[{"id": "id1", "name": "John Park"}, {"id": "id2", "name": "John Smith"}]
    )
    masked = masker.mask("John Park and John Smith disagree.")
    handles = [token.strip(".") for token in masked.split() if token.startswith("person")]

    assert handles[0] != handles[1]  # keyed by record id, not name
    assert masker.unmask(masked) == "John Park and John Smith disagree."


def test_substring_name_not_masked_inside_longer_word():
    masker = _masker().register(contacts=[{"id": "id1", "name": "Jon"}])
    masked = masker.mask("Jonathan emailed; Jon replied.")

    assert "Jonathan" in masked  # not corrupted
    assert masker.unmask(masked) == "Jonathan emailed; Jon replied."


def test_repeated_calls_reuse_the_same_handle():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    first = masker.mask("John Park called.")
    second = masker.mask("John Park called again.")
    token_first = next(t.strip(".") for t in first.split() if t.startswith("person"))
    token_second = next(t.strip(".") for t in second.split() if t.startswith("person"))

    assert token_first == token_second  # stable across passes


def test_shadow_names_are_masked():
    masker = _masker().register(shadows=[SimpleNamespace(name="David")])
    masked = masker.mask("David is the decision maker.")

    assert "David" not in masked
    assert masker.unmask(masked) == "David is the decision maker."


def test_accented_name_round_trips():
    masker = _masker(discovered=["José Álvarez"]).register(contacts=[])
    masked = masker.mask("José Álvarez owns the budget.")

    assert "José" not in masked
    assert masker.unmask(masked) == "José Álvarez owns the budget."


def test_empty_and_non_string_inputs_are_safe():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])

    assert masker.mask("") == ""
    assert masker.unmask("") == ""
    assert masker.unmask(None) is None
    assert masker.unmask(42) == 42
    assert masker.unmask(["John Park", {"k": "John Park"}]) == ["John Park", {"k": "John Park"}]


def test_unmask_leaves_unknown_handles_untouched():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    # person999 was never issued — must not be guessed or dropped.
    assert masker.unmask("person999 said hi") == "person999 said hi"


def test_capitalized_handle_unmasks_in_free_text():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    masked = masker.mask("John Park is concerned")
    capitalized = masked[0].upper() + masked[1:]

    assert capitalized.startswith("Person")
    assert masker.unmask(capitalized) == "John Park is concerned"


def test_unmask_fields_restores_text_but_leaves_ids():
    masker = _masker().register(contacts=[{"id": "id1", "name": "John Park"}])
    masked_value = masker.mask("John Park is concerned")
    items = [{"entity_id": "crm_id1", "fact_value": masked_value, "fact_type": "concern"}]

    masker.unmask_fields(items, ("fact_value",))

    assert items[0]["fact_value"] == "John Park is concerned"
    assert items[0]["entity_id"] == "crm_id1"
    assert items[0]["fact_type"] == "concern"
