"""Compare schema_compactor savings: raw vs base (strip+dedup) vs Tier 1+2.

    python scripts/measure_compaction.py                # committed fixtures
    python scripts/measure_compaction.py --bridge       # every live tool (~254)
    python scripts/measure_compaction.py --bridge --all # ...print every row

base   = strip + dedup only (the previous behavior).
tier1  = base + collapse duplicated filter block + factor universal prose +
         drop formulaic field descriptions (per tool).
tier2  = tier1 + one shared filter-shape table per learn call (cross-tool),
         only measurable at the envelope level (see the --bridge summary).
"""

from __future__ import annotations

import copy
import json
import sys
import urllib.request
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_ROOT))

from agent.schema_compactor import compact_learn_payload, compact_schema  # noqa: E402

_FIXTURE_DIR = _PKG_ROOT / "tests" / "data" / "schemas"
_LEARN_BATCH = 25


def count_tokens(text: str) -> int:
    """Token count via the o200k tokenizer, with a char-based fallback."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _tok(obj) -> int:
    return count_tokens(json.dumps(obj, indent=2))


def _pct(before: int, after: int) -> str:
    return "  0.0%" if before == 0 else f"-{(before - after) / before * 100:4.1f}%"


# ---------------------------------------------------------------------------
# Per-tool measurement (raw / base / tier1)
# ---------------------------------------------------------------------------

def _tool_variants(tool: dict) -> tuple[int, int, int]:
    schema = tool.get("inputSchema", {})
    raw = _tok(tool)
    base = _tok({**tool, "inputSchema": compact_schema(schema, tier1=False)})
    tier1 = _tok({**tool, "inputSchema": compact_schema(schema, tier1=True)})
    return raw, base, tier1


def measure(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    """Whole-envelope lines/tokens (before, after) for one fixture (tests use this)."""
    original = json.loads(path.read_text())
    compacted = compact_learn_payload(original)
    before, after = json.dumps(original, indent=2), json.dumps(compacted, indent=2)
    lines = (before.count("\n") + 1, after.count("\n") + 1)
    tokens = (count_tokens(before), count_tokens(after))
    return lines, tokens


# ---------------------------------------------------------------------------
# Tool sources
# ---------------------------------------------------------------------------

def _from_fixtures() -> list[dict]:
    tools: list[dict] = []
    for path in sorted(_FIXTURE_DIR.glob("*.json")):
        tools.extend(json.loads(path.read_text())["data"]["tools"])
    return tools


def _load_dotenv() -> None:
    import os

    env_path = _PKG_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _post(path: str, payload: dict) -> dict:
    import os

    base = os.environ.get("NODE_BRIDGE_BASE_URL", "http://localhost:3000/agent-bridge")
    request = urllib.request.Request(
        f"{base}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def _from_bridge() -> list[dict]:
    import os

    _load_dotenv()
    ids = {
        "workspaceId": os.environ["TWENTY_WORKSPACE_ID"],
        "roleId": os.environ.get("TWENTY_ROLE_ID") or os.environ["TWENTY_WRITER_ROLE_ID"],
    }
    catalog = _post("catalog", ids)["data"]["catalog"]
    names = [entry["name"] for entries in catalog.values() for entry in entries]
    print(f"catalog: {len(names)} tools across {len(catalog)} categories", file=sys.stderr)

    tools: list[dict] = []
    for start in range(0, len(names), _LEARN_BATCH):
        result = _post("learn", {"toolNames": names[start : start + _LEARN_BATCH], **ids})
        tools.extend(result.get("data", {}).get("tools", []))
    return tools


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _row(name: str, raw: int, base: int, tier1: int) -> str:
    return (
        f"{name:<30} {raw:>6} {base:>7} {_pct(raw, base)} "
        f"{tier1:>7} {_pct(raw, tier1)} {_pct(base, tier1):>7} vs base"
    )


def _report(tools: list[dict], *, show_all: bool, envelope: bool) -> int:
    rows = [(tool.get("name", "?"), *_tool_variants(tool)) for tool in tools]
    rows.sort(key=lambda r: r[1], reverse=True)

    header = f"{'tool':<30} {'raw':>6} {'base':>7} {'  Δraw':>6} {'tier1':>7} {'  Δraw':>6} {'  Δbase':>11}"
    print(header)
    print("-" * len(header))
    shown = rows if show_all else rows[:20]
    for name, raw, base, tier1 in shown:
        print(_row(name, raw, base, tier1))
    if not show_all and len(rows) > len(shown):
        print(f"... {len(rows) - len(shown)} more (use --all)")

    raw_t = sum(r[1] for r in rows)
    base_t = sum(r[2] for r in rows)
    tier1_t = sum(r[3] for r in rows)
    print("-" * len(header))
    print(_row(f"TOTAL ({len(rows)} tools)", raw_t, base_t, tier1_t))

    if envelope:
        _envelope_summary(tools)
    return 0


def _envelope_summary(tools: list[dict]) -> None:
    """Tier 2 only shows at the envelope level (shared shapes emitted once)."""
    payload = {"data": {"tools": tools}}
    base_env = _tok(compact_learn_payload(copy.deepcopy(payload), tier1=False, tier2=False)["data"])
    tier1_env = _tok(compact_learn_payload(copy.deepcopy(payload), tier1=True, tier2=False)["data"])
    tier12_env = _tok(compact_learn_payload(copy.deepcopy(payload), tier1=True, tier2=True)["data"])
    raw_env = _tok(payload["data"])

    print(f"\nWhole-catalog learn envelope ({len(tools)} tools, one call):")
    print(f"  raw          {raw_env:>8}")
    print(f"  base         {base_env:>8}  {_pct(raw_env, base_env)} vs raw")
    print(f"  tier1        {tier1_env:>8}  {_pct(raw_env, tier1_env)} vs raw  {_pct(base_env, tier1_env)} vs base")
    print(f"  tier1+tier2  {tier12_env:>8}  {_pct(raw_env, tier12_env)} vs raw  {_pct(base_env, tier12_env)} vs base")


def main(argv: list[str]) -> int:
    is_bridge = "--bridge" in argv
    tools = _from_bridge() if is_bridge else _from_fixtures()
    if not tools:
        print("No tools found.")
        return 1
    return _report(tools, show_all="--all" in argv, envelope=is_bridge)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
