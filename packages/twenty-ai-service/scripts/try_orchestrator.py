import asyncio
import pathlib
import sys

# Make `agent` importable when run as a script (not as a module).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.orchestrator import orchestrator  # noqa: E402

query = sys.argv[1] if len(sys.argv) > 1 else "Find the number of employees in Uber"
result = asyncio.run(orchestrator.handle(query))
print("Answer:", result.get("response"))
for call in result.get("tool_calls", []):
    print("  ->", call["name"])

# Run from packages/twenty-ai-service:
#   python scripts/try_orchestrator.py "Find the number of employees in Uber"