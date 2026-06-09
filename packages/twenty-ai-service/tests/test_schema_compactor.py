"""Tests for agent/schema_compactor.py.

Covers the three guarantees the compactor must keep:
- Noise removal & operator-prose stripping (token win, no signal loss).
- Repeated subschemas collapse into valid $defs/$ref (the big win on filters).
- Compaction is safe: passthrough on odd shapes, idempotent, and it never drops
  a field name, required entry, or enum the model needs to call the tool.

The final class doubles as the savings report the task asked for — run with
`-s` to see the per-file lines/tokens table.
"""

import json
from pathlib import Path

import pytest

from agent.schema_compactor import (
    CONVENTIONS,
    _child_schemas,
    compact_learn_payload,
    compact_schema,
)
from scripts.measure_compaction import count_tokens, measure

_SCHEMA_DIR = Path(__file__).parent / "data" / "schemas"
_FIXTURES = sorted(_SCHEMA_DIR.glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((_SCHEMA_DIR / name).read_text())


def _walk(node):
    """Yield every dict node in a schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _property_names(schema: dict) -> set[str]:
    """Real input field names, collected position-aware.

    Uses the compactor's own schema traversal so a CRM field literally named
    ``properties`` or ``pattern`` is treated as a field, and its schema's
    keywords are never miscounted as field names.
    """
    names: set[str] = set()

    def visit(node):
        if not isinstance(node, dict):
            return
        for keyword, name, sub in _child_schemas(node):
            if keyword == "properties" and name is not None:
                names.add(name)
            visit(sub)

    visit(schema)
    return names


# ---------------------------------------------------------------------------
# Pass 1: noise & operator-description stripping
# ---------------------------------------------------------------------------

class TestStrip:
    def test_drops_validator_only_keys(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "id": {"type": "string", "format": "uuid", "pattern": "^[0-9a-f]+$"}
            },
        }
        compacted = compact_schema(schema)
        assert "$schema" not in compacted
        assert "pattern" not in compacted["properties"]["id"]
        # `format` is cheap, useful signal — kept.
        assert compacted["properties"]["id"]["format"] == "uuid"

    def test_drops_safe_integer_bounds_but_keeps_real_ones(self):
        schema = {
            "type": "object",
            "properties": {
                "offset": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                "limit": {"type": "integer", "exclusiveMinimum": 0, "maximum": 100},
            },
        }
        compacted = compact_schema(schema)
        assert compacted["properties"]["offset"] == {"type": "integer", "minimum": 0}
        assert compacted["properties"]["limit"] == {"type": "integer", "maximum": 100}

    def test_drops_operator_descriptions_keeps_field_descriptions(self):
        schema = {
            "type": "object",
            "properties": {
                "annualRecurringRevenue": {
                    "description": "Annual Recurring Revenue: estimated yearly revenue",
                    "type": "object",
                    "properties": {
                        "eq": {"description": "Equals", "type": "string"},
                        "is": {
                            "description": "Use NULL or NOT_NULL",
                            "type": "string",
                            "enum": ["NULL", "NOT_NULL"],
                        },
                    },
                    "additionalProperties": False,
                }
            },
        }
        compacted = compact_schema(schema)
        field = compacted["properties"]["annualRecurringRevenue"]
        # Real, non-formulaic field descriptions are preserved.
        assert field["description"] == "Annual Recurring Revenue: estimated yearly revenue"
        assert "description" not in field["properties"]["eq"]  # universal -> dropped
        assert "description" not in field["properties"]["is"]
        assert field["properties"]["is"]["enum"] == ["NULL", "NOT_NULL"]  # signal kept
        assert "additionalProperties" not in field  # `false` -> dropped

    def test_drops_formulaic_field_descriptions(self):
        # Tier 1 idea 3: "Filter by <field> (<type> field)" is pure filler.
        schema = {
            "type": "object",
            "properties": {
                "jobTitle": {
                    "description": "Filter by jobTitle (text field)",
                    "type": "object",
                    "properties": {"eq": {"type": "string"}},
                }
            },
        }
        field = compact_schema(schema)["properties"]["jobTitle"]
        assert "description" not in field

    def test_keeps_meaningful_additional_properties(self):
        # orderBy uses additionalProperties as a field-name -> direction map.
        schema = {
            "type": "object",
            "properties": {
                "orderBy": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": {"type": "string", "enum": ["Asc", "Desc"]},
                }
            },
        }
        compacted = compact_schema(schema)
        assert compacted["properties"]["orderBy"]["additionalProperties"] == {
            "type": "string",
            "enum": ["Asc", "Desc"],
        }


# ---------------------------------------------------------------------------
# Pass 2: dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_repeated_operator_object_collapses_to_ref(self):
        compacted = compact_schema(_load("find_people.json")["data"]["tools"][0]["inputSchema"])
        assert "$defs" in compacted

        # Every $ref must resolve to a real def.
        def_names = set(compacted["$defs"].keys())
        for node in _walk(compacted):
            ref = node.get("$ref")
            if ref:
                assert ref.startswith("#/$defs/")
                assert ref.rsplit("/", 1)[-1] in def_names

    def test_small_or_unique_nodes_are_not_hoisted(self):
        # Two tiny identical leaves: below the size threshold, left inline.
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        }
        assert "$defs" not in compact_schema(schema)


# ---------------------------------------------------------------------------
# Safety: payload handling
# ---------------------------------------------------------------------------

class TestPayload:
    def test_passthrough_on_unexpected_shape(self):
        assert compact_learn_payload({"ok": False}) == {"ok": False}
        assert compact_learn_payload({"data": {"tools": "nope"}}) == {
            "data": {"tools": "nope"}
        }

    def test_adds_conventions_once(self):
        result = compact_learn_payload(_load("find_people.json"))
        assert result["data"]["conventions"] == CONVENTIONS

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.stem)
    def test_idempotent(self, fixture):
        once = compact_learn_payload(json.loads(fixture.read_text()))
        twice = compact_learn_payload(json.loads(json.dumps(once)))
        assert once == twice

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.stem)
    def test_preserves_field_vocabulary_and_required(self, fixture):
        original = json.loads(fixture.read_text())
        compacted = compact_learn_payload(original)

        for before, after in zip(original["data"]["tools"], compacted["data"]["tools"]):
            # No field name the model needs may disappear (refs keep them in $defs).
            assert _property_names(before["inputSchema"]) <= _property_names(
                after["inputSchema"]
            )
            # The execution contract — required args — is untouched.
            assert before["inputSchema"].get("required") == after["inputSchema"].get(
                "required"
            )


# ---------------------------------------------------------------------------
# Savings report (the per-file lines/tokens table)
# ---------------------------------------------------------------------------

class TestSavings:
    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.stem)
    def test_meaningful_reduction(self, fixture, capsys):
        lines, tokens = measure(fixture)
        line_cut = (lines[0] - lines[1]) / lines[0]
        token_cut = (tokens[0] - tokens[1]) / tokens[0]

        with capsys.disabled():
            print(
                f"\n{fixture.stem:<22} "
                f"lines {lines[0]:>5} -> {lines[1]:<5} (-{line_cut*100:.1f}%)   "
                f"tokens {tokens[0]:>6} -> {tokens[1]:<6} (-{token_cut*100:.1f}%)"
            )

        # Floor that holds across tool classes: lean write tools dedup ~25-30%,
        # filter-heavy read tools shed 60%+ (see the printed report).
        assert token_cut >= 0.20, f"expected >=20% token cut, got {token_cut:.1%}"
        assert line_cut >= 0.20, f"expected >=20% line cut, got {line_cut:.1%}"

    def test_token_counter_is_available(self):
        assert count_tokens("hello world") > 0


# ---------------------------------------------------------------------------
# Full sweep: every live tool (~254). Needs the Node bridge + a workspace.
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBridgeSweep:
    @pytest.fixture(scope="class")
    def tools(self):
        from scripts.measure_compaction import _from_bridge

        try:
            tools = _from_bridge()
        except Exception as error:  # bridge down, no env, etc.
            pytest.skip(f"bridge unavailable: {error}")
        if not tools:
            pytest.skip("bridge returned no tools")
        return tools

    def test_every_tool_compacts_safely(self, tools):
        """No tool crashes the compactor or loses field names / required args."""
        before_tokens = after_tokens = 0
        for tool in tools:
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict):
                continue
            compacted = compact_schema(schema)  # must not raise for any tool

            assert _property_names(schema) <= _property_names(compacted), tool["name"]
            assert schema.get("required") == compacted.get("required"), tool["name"]

            before_tokens += count_tokens(json.dumps(schema))
            after_tokens += count_tokens(json.dumps(compacted))

        cut = (before_tokens - after_tokens) / before_tokens
        assert cut >= 0.40, f"expected >=40% aggregate token cut, got {cut:.1%}"
