"""Compact Twenty tool input schemas before they reach the LLM.

Twenty generates every tool's ``inputSchema`` with ``z.toJSONSchema(...)``, whose
default output is validator-oriented: regex ``pattern``s, JS safe-integer bounds,
``$schema`` URLs, ``additionalProperties: false``, and the same operator object
(``eq``/``neq``/``like``/...) repeated on every filterable field — often twice,
since ``or``/``and``/``not`` duplicate the whole filter block. None of that helps
the model choose arguments; it just burns context (a single ``find_*`` schema is
6-9k tokens).

This module rewrites the *LLM-facing* view of a schema. It is lossless for
correctness: the full schema stays server-side and still validates every
``execute_tool`` call, so the worst case of an over-aggressive compaction is a
retry, never a bad write.

The transforms are generic — keyed on JSON-Schema structure and Twenty's fixed
operator vocabulary, not on any individual tool — so one pass covers all ~254
tools and any custom object a workspace adds. A node that matches no rule is
passed through unchanged, so an unfamiliar shape degrades to verbose-but-correct.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Operator keys Twenty emits on filter fields. Their meaning is universal, so we
# drop their per-field descriptions and document them once via OPERATOR_LEGEND.
_OPERATOR_KEYS = frozenset(
    {
        "eq",
        "neq",
        "in",
        "like",
        "ilike",
        "startsWith",
        "endsWith",
        "is",
        "gt",
        "gte",
        "lt",
        "lte",
    }
)

# Keys that only matter to a validator; the model cannot act on them.
_NOISE_KEYS = frozenset({"$schema", "pattern", "exclusiveMinimum", "exclusiveMaximum"})

# z.toJSONSchema stamps JS Number.MAX_SAFE_INTEGER bounds on every int/number.
_SAFE_INT = 9007199254740991

# Repeated subschemas this size or larger (serialized chars) are hoisted into
# $defs and referenced. Below it a $ref costs more than it saves.
_DEDUP_MIN_CHARS = 200

# Documented once per learn call instead of on every field/tool (Tier 1, idea 2).
# These three are byte-identical across every read tool, so they live in the
# envelope's `conventions` block and are stripped from each tool's schema.
OPERATOR_LEGEND = (
    "Filter operators are the same on every field: eq (equals), neq (not equals), "
    "in (in array), like / ilike (case-sensitive / -insensitive pattern, % wildcard), "
    "startsWith, endsWith, gt / gte / lt / lte (ranges), "
    "is ('NULL' = missing, 'NOT_NULL' = present)."
)
PAGINATION_LEGEND = (
    "limit: max records to return (default 10, max 100); start small. "
    "offset: records to skip (default 0)."
)
ORDERBY_LEGEND = (
    "orderBy: array of single-key objects {fieldName: direction}, where direction is "
    "AscNullsFirst | AscNullsLast | DescNullsFirst | DescNullsLast. "
    "CRITICAL for 'top N' / 'largest' / 'smallest': use DescNullsLast for top/largest, "
    "AscNullsFirst for bottom/smallest. Example: [{\"employees\": \"DescNullsLast\"}]."
)

CONVENTIONS = {
    "operators": OPERATOR_LEGEND,
    "pagination": PAGINATION_LEGEND,
    "orderBy": ORDERBY_LEGEND,
}

# Top-level fields whose prose is universal and now lives in CONVENTIONS.
_UNIVERSAL_PROSE_FIELDS = ("limit", "offset", "orderBy")

# `"Filter by <field> (<type> field)"` — the key already names the field and the
# referenced operator shape names the type, so this description is pure filler.
_FORMULAIC_DESC = re.compile(r"^Filter by .+ \(.+ field\)$")


# JSON-Schema keywords whose value holds nested schema(s). Everything else in a
# schema node is data (description, type, enum, required, default, format, ...).
# Keys *inside* one of the map keywords are field NAMES, never keywords — this is
# what keeps the compactor from mistaking a field literally named `pattern` or
# `properties` for a validator keyword.
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_SCHEMA_VALUE_KEYWORDS = frozenset(
    {
        "items",
        "additionalProperties",
        "additionalItems",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "contains",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)


def _child_schemas(node: dict):
    """Yield ``(keyword, prop_name, subschema)`` for each nested schema.

    Walking only schema-bearing keywords means data values (enum lists, required
    arrays, and the field-name keys inside a ``properties`` map) are never treated
    as schemas — so a field literally named ``pattern`` is left untouched.
    """
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for name, sub in value.items():
                yield key, name, sub
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            for sub in value:
                yield key, None, sub
        elif key in _SCHEMA_VALUE_KEYWORDS:
            if isinstance(value, list):
                for sub in value:
                    yield key, None, sub
            elif isinstance(value, dict):
                yield key, None, value


# ---------------------------------------------------------------------------
# Pass 1: strip noise and redundant operator descriptions
# ---------------------------------------------------------------------------

def _strip_schema(node: Any, prop_name: str | None) -> Any:
    """Rebuild a schema node without validator-only keys or operator prose.

    ``prop_name`` is the property name this schema sits under, so a description
    is dropped only for operator leaves (``eq``, ``is``, ...). Recursion follows
    schema-bearing keywords only, so field names are preserved verbatim.
    """
    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _NOISE_KEYS:
            continue
        # `additionalProperties: false` is a validator hint; a schema value here
        # (e.g. orderBy's field-name map) is real signal and falls through below.
        if key == "additionalProperties" and value is False:
            continue
        if (
            key in ("minimum", "maximum")
            and isinstance(value, (int, float))
            and abs(value) == _SAFE_INT
        ):
            continue
        if key == "description" and prop_name in _OPERATOR_KEYS:
            continue

        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            result[key] = {
                name: _strip_schema(sub, name) for name, sub in value.items()
            }
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            result[key] = [_strip_schema(sub, None) for sub in value]
        elif key in _SCHEMA_VALUE_KEYWORDS and isinstance(value, (dict, list)):
            result[key] = (
                [_strip_schema(sub, None) for sub in value]
                if isinstance(value, list)
                else _strip_schema(value, None)
            )
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Pass 2: hoist repeated subschemas into $defs
# ---------------------------------------------------------------------------

def _signature(node: dict) -> str:
    """Stable identity of a schema node, ignoring its own ``description``.

    Two fields that differ only by description (``Filter by city`` vs
    ``Filter by jobTitle``) then share one hoisted definition.
    """
    body = {key: value for key, value in node.items() if key != "description"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _count_schemas(node: Any, counts: dict[str, int]) -> None:
    if not isinstance(node, dict):
        return
    # A $ref node is already minimal; skip it as a dedup candidate.
    if "$ref" not in node:
        signature = _signature(node)
        if len(signature) >= _DEDUP_MIN_CHARS:
            counts[signature] = counts.get(signature, 0) + 1
    for _key, _name, sub in _child_schemas(node):
        _count_schemas(sub, counts)


def _replace_schemas(node: Any, chosen: dict[str, str], defs: dict[str, Any]) -> Any:
    """Replace each chosen subschema with a ``$ref``, top-down.

    A definition is materialized only when first referenced. Its stored body is
    itself compacted, so the result is a fixed point: re-compacting changes
    nothing. Recursion follows schema positions only.
    """
    if not isinstance(node, dict):
        return node

    if "$ref" not in node:
        reference_name = chosen.get(_signature(node))
        if reference_name is not None:
            if reference_name not in defs:
                # Reserve the name first so an identical nested copy refers back
                # here instead of recursing forever.
                defs[reference_name] = None
                defs[reference_name] = _rebuild(node, chosen, defs, drop_description=True)
            reference = {"$ref": f"#/$defs/{reference_name}"}
            if "description" in node:
                # 2020-12 lets a $ref carry a sibling description.
                return {"description": node["description"], **reference}
            return reference

    return _rebuild(node, chosen, defs, drop_description=False)


def _rebuild(
    node: dict, chosen: dict[str, str], defs: dict[str, Any], *, drop_description: bool
) -> dict:
    result: dict[str, Any] = {}
    for key, value in node.items():
        if drop_description and key == "description":
            continue
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            result[key] = {
                name: _replace_schemas(sub, chosen, defs)
                for name, sub in value.items()
            }
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            result[key] = [_replace_schemas(sub, chosen, defs) for sub in value]
        elif key in _SCHEMA_VALUE_KEYWORDS and isinstance(value, (dict, list)):
            result[key] = (
                [_replace_schemas(sub, chosen, defs) for sub in value]
                if isinstance(value, list)
                else _replace_schemas(value, chosen, defs)
            )
        else:
            result[key] = value
    return result


def _dedup(stripped: dict) -> dict:
    """Hoist repeated subschemas into ``$defs`` (the original Pass 2)."""
    counts: dict[str, int] = {}
    _count_schemas(stripped, counts)
    chosen = {
        signature: f"_f{index}"
        for index, (signature, count) in enumerate(counts.items())
        if count >= 2
    }
    if not chosen:
        return stripped

    defs: dict[str, Any] = {}
    compacted = _replace_schemas(stripped, chosen, defs)
    if defs:
        compacted["$defs"] = {**compacted.get("$defs", {}), **defs}
    return compacted


# ---------------------------------------------------------------------------
# Tier 1: per-tool transforms (idea 1 collapse, idea 2 prose, idea 3 formula)
# ---------------------------------------------------------------------------

def _drop_all_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _drop_all_descriptions(value)
            for key, value in node.items()
            if key != "description"
        }
    if isinstance(node, list):
        return [_drop_all_descriptions(item) for item in node]
    return node


def _factor_universal_prose(schema: dict) -> dict:
    """Drop the universal limit/offset/orderBy prose (now in CONVENTIONS)."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    new_properties = dict(properties)
    for field in _UNIVERSAL_PROSE_FIELDS:
        if field in new_properties:
            new_properties[field] = _drop_all_descriptions(new_properties[field])
    return {**schema, "properties": new_properties}


def _shrink_formulaic_descriptions(node: Any) -> Any:
    """Drop ``"Filter by <field> (<type> field)"`` filler descriptions."""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description" and isinstance(value, str) and _FORMULAIC_DESC.match(value):
                continue
            result[key] = _shrink_formulaic_descriptions(value)
        return result
    if isinstance(node, list):
        return [_shrink_formulaic_descriptions(item) for item in node]
    return node


def _collapse_self_filter(schema: dict) -> dict:
    """Collapse the top-level filter block duplicated inside a ``$def``.

    Read tools emit every filter field twice: inline at the top level and again
    in ``__schema0`` (the recursive target of or/and/not). When a $def's whole
    property set is mirrored verbatim at the top level, drop the top-level copies
    and pull them in via ``allOf: [{$ref}]`` — identical meaning, one copy. Safe
    because we already stripped ``additionalProperties: false`` (no allOf clash).
    """
    defs = schema.get("$defs")
    properties = schema.get("properties")
    if not isinstance(defs, dict) or not isinstance(properties, dict):
        return schema

    for name, definition in defs.items():
        mirrored = definition.get("properties") if isinstance(definition, dict) else None
        if not isinstance(mirrored, dict) or not mirrored:
            continue
        if all(key in properties and properties[key] == value for key, value in mirrored.items()):
            remaining = {k: v for k, v in properties.items() if k not in mirrored}
            result = {k: v for k, v in schema.items() if k != "properties"}
            if remaining:
                result["properties"] = remaining
            result["allOf"] = schema.get("allOf", []) + [{"$ref": f"#/$defs/{name}"}]
            return result
    return schema


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compact_schema(schema: dict, *, tier1: bool = True) -> dict:
    """Return a compacted copy of one JSON-Schema (pure; never mutates input).

    ``tier1=False`` runs only the baseline strip + dedup (used for comparisons).
    """
    if not isinstance(schema, dict):
        return schema

    node = _strip_schema(schema, None)
    if tier1:
        node = _factor_universal_prose(node)
        node = _shrink_formulaic_descriptions(node)
        node = _collapse_self_filter(node)
    return _dedup(node)


# ---------------------------------------------------------------------------
# Tier 2: cross-tool shared filter shapes
# ---------------------------------------------------------------------------

def _signature_of(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _repoint_refs(node: Any, remap: dict[str, str]) -> Any:
    """Rewrite ``#/$defs/<local>`` refs to their shared ``shapes:<name>`` pointer."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref[len("#/$defs/"):] in remap:
            rest = {k: v for k, v in node.items() if k != "$ref"}
            return {**{k: _repoint_refs(v, remap) for k, v in rest.items()}, "$ref": remap[ref[len("#/$defs/"):]]}
        return {key: _repoint_refs(value, remap) for key, value in node.items()}
    if isinstance(node, list):
        return [_repoint_refs(item, remap) for item in node]
    return node


def _hoist_shared_shapes(tools: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    """Lift ``$defs`` bodies shared by >=2 tools into one envelope-level table.

    Operator shapes (DateFilter, TextFilter, ...) are byte-identical across all
    254 tools; emitting them once per learn call instead of once per tool is the
    cross-tool win. Refs become ``shapes:<name>``, resolved via data.filterShapes.
    """
    tools_per_signature: dict[str, set[int]] = {}
    body_by_signature: dict[str, Any] = {}
    for index, tool in enumerate(tools):
        defs = tool.get("inputSchema", {}).get("$defs", {}) if isinstance(tool, dict) else {}
        for body in defs.values():
            signature = _signature_of(body)
            tools_per_signature.setdefault(signature, set()).add(index)
            body_by_signature[signature] = body

    shared = sorted(sig for sig, owners in tools_per_signature.items() if len(owners) >= 2)
    if not shared:
        return tools, {}
    names = {signature: f"s{i}" for i, signature in enumerate(shared)}
    shapes = {names[signature]: body_by_signature[signature] for signature in shared}

    new_tools: list[Any] = []
    for tool in tools:
        schema = tool.get("inputSchema") if isinstance(tool, dict) else None
        local_defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}
        remap = {
            local: f"shapes:{names[_signature_of(body)]}"
            for local, body in local_defs.items()
            if _signature_of(body) in names
        }
        if not remap:
            new_tools.append(tool)
            continue
        new_schema = _repoint_refs(schema, remap)
        kept = {k: v for k, v in new_schema.get("$defs", {}).items() if k not in remap}
        if kept:
            new_schema["$defs"] = kept
        else:
            new_schema.pop("$defs", None)
        new_tools.append({**tool, "inputSchema": new_schema})
    return new_tools, shapes


# ---------------------------------------------------------------------------
# Envelope entry point
# ---------------------------------------------------------------------------

def _has_filter(tool: Any) -> bool:
    """True if a tool exposes filtering (read/find/group), false for writes."""
    schema = tool.get("inputSchema") if isinstance(tool, dict) else None
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if isinstance(properties, dict) and any(
        marker in properties for marker in ("or", "and", "not", "orderBy")
    ):
        return True
    # After _collapse_self_filter, or/and/not move into the $def (__schema0).
    for definition in schema.get("$defs", {}).values():
        nested = definition.get("properties") if isinstance(definition, dict) else None
        if isinstance(nested, dict) and any(m in nested for m in ("or", "and", "not")):
            return True
    return False


def compact_learn_payload(payload: dict, *, tier1: bool = True, tier2: bool = True) -> dict:
    """Compact every ``tool.inputSchema`` in a learn-tools envelope.

    Returns the envelope unchanged on any unexpected shape, and falls back to the
    original schema per tool on any error, so a compaction bug can never stop the
    agent from learning a tool — it only ever affects token cost.
    """
    try:
        tools = payload["data"]["tools"]
    except (KeyError, TypeError):
        return payload
    if not isinstance(tools, list):
        return payload

    compacted_tools: list[Any] = []
    for tool in tools:
        try:
            schema = tool.get("inputSchema")
            if isinstance(schema, dict):
                tool = {**tool, "inputSchema": compact_schema(schema, tier1=tier1)}
        except Exception:
            pass  # passthrough: a verbose schema beats none
        compacted_tools.append(tool)

    new_data = {**payload["data"], "tools": compacted_tools}
    if tier1 and any(_has_filter(tool) for tool in compacted_tools):
        # Only read/filter tools use operators/pagination/orderBy; documenting
        # them for a pure-write learn call would be dead weight.
        new_data["conventions"] = CONVENTIONS
    if tier2:
        compacted_tools, shapes = _hoist_shared_shapes(compacted_tools)
        new_data["tools"] = compacted_tools
        if shapes:
            new_data["filterShapes"] = shapes

    return {**payload, "data": new_data}
