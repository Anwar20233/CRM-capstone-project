"""Shared async client for the Node agent-bridge.

Both the FastAPI proxy router (routers/bridge.py) and the LangChain agent tools
(agent/crm_tools.py) forward through this single function so there is one place
that knows the bridge URL, timeout, and error envelope.
"""

import os

import httpx

# Twenty server mounts controllers at the root (no global /api prefix), so the
# bridge lives at /agent-bridge. Override with NODE_BRIDGE_BASE_URL if needed.
NODE_BRIDGE_BASE_URL = os.environ.get(
    "NODE_BRIDGE_BASE_URL", "http://localhost:3000/agent-bridge"
)

# Generous timeout: some tools (metadata scans, large finds) are slow.
_HTTP_TIMEOUT = httpx.Timeout(60.0)


async def forward(path: str, payload: dict) -> dict:
    """POST payload to the Node bridge and return its JSON envelope as-is.

    Never raises across the boundary: transport and decode failures are returned
    in the same {ok, error} shape the bridge itself uses.
    """
    url = f"{NODE_BRIDGE_BASE_URL}/{path}"

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            response = await client.post(url, json=payload)
        except httpx.RequestError as error:
            return {
                "ok": False,
                "error": {
                    "code": "BRIDGE_UNREACHABLE",
                    "message": f"Could not reach Node bridge at {url}: {error}",
                },
            }

    try:
        return response.json()
    except ValueError:
        return {
            "ok": False,
            "error": {
                "code": "BAD_RESPONSE",
                "message": (
                    f"Node bridge returned non-JSON (status {response.status_code})"
                ),
            },
        }
