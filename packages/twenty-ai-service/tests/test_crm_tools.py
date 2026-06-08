"""Tests for scoped CRM meta-tools (agent/crm_tools.py).

Verifies:
- Reader's execute_tool returns OUT_OF_SCOPE for write tools and never calls bridge.
- Writer's execute_tool returns OUT_OF_SCOPE for read tools and never calls bridge.
- Reader's get_tool_catalog only shows read tools.
- Writer's get_tool_catalog only shows write tools.
- Writer's execute_tool with WritePolicy middleware:
  - Tier 1 writes execute transparently.
  - Tier 3 writes return CONFIRMATION_REQUIRED with a token.
  - Passing a valid token executes the write.
- Reader and writer use different bridge roleIds.
"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.crm_tools import build_crm_tools
from agent.tool_scope import READER_SCOPE, WRITER_SCOPE
from agent.workers.write_policy import WritePolicy


@pytest.fixture
def identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required identity env vars for all scopes."""
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "ws-test-123")
    monkeypatch.setenv("TWENTY_USER_ID", "user-test-456")
    monkeypatch.setenv("TWENTY_ROLE_ID", "generic-role")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")
    monkeypatch.setenv("TWENTY_WRITER_ROLE_ID", "writer-role")


def _get_tool(tools, name: str):
    """Find a tool by name from a list of StructuredTools."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in {[t.name for t in tools]}")


# ---------------------------------------------------------------------------
# Reader scope — execute_tool guard
# ---------------------------------------------------------------------------

class TestReaderExecuteGuard:
    """Reader's execute_tool must block write operations."""

    @pytest.mark.asyncio
    async def test_reader_blocks_create(self, identity_env: None) -> None:
        tools = build_crm_tools(READER_SCOPE)
        execute = _get_tool(tools, "execute_tool")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await execute.ainvoke({"tool": "create_person", "tool_args": {"name": "test"}})

        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        assert "reader" in result["error"]["message"]
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reader_blocks_update(self, identity_env: None) -> None:
        tools = build_crm_tools(READER_SCOPE)
        execute = _get_tool(tools, "execute_tool")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await execute.ainvoke({"tool": "update_opportunity", "tool_args": {}})

        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reader_allows_find(self, identity_env: None) -> None:
        tools = build_crm_tools(READER_SCOPE)
        execute = _get_tool(tools, "execute_tool")

        mock_response = {"ok": True, "data": [{"id": "1"}]}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            result = await execute.ainvoke({"tool": "find_people", "tool_args": {"limit": 5}})

        assert result["ok"] is True
        mock_fwd.assert_awaited_once()
        assert mock_fwd.call_args[0][1]["roleId"] == "reader-role"


# ---------------------------------------------------------------------------
# Writer scope — execute_tool guard
# ---------------------------------------------------------------------------

class TestWriterExecuteGuard:
    """Writer's execute_tool must block read operations."""

    @pytest.mark.asyncio
    async def test_writer_blocks_find(self, identity_env: None) -> None:
        tools = build_crm_tools(WRITER_SCOPE)
        execute = _get_tool(tools, "execute_tool")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await execute.ainvoke({"tool": "find_people", "tool_args": {}})

        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        assert "writer" in result["error"]["message"]
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writer_allows_create(self, identity_env: None) -> None:
        tools = build_crm_tools(WRITER_SCOPE)
        execute = _get_tool(tools, "execute_tool")

        mock_response = {"ok": True, "data": {"id": "new-id"}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            result = await execute.ainvoke({"tool": "create_person", "tool_args": {"name": "test"}})

        assert result["ok"] is True
        mock_fwd.assert_awaited_once()
        assert mock_fwd.call_args[0][1]["roleId"] == "writer-role"


# ---------------------------------------------------------------------------
# Catalog filtering
# ---------------------------------------------------------------------------

class TestCatalogFiltering:
    """Catalog shows only tools within scope."""

    @staticmethod
    def _mock_catalog() -> dict:
        """Return a fresh catalog dict each time (avoids mutation issues)."""
        return {
            "ok": True,
            "data": [
                {"name": "find_people", "description": "Find people"},
                {"name": "create_person", "description": "Create person"},
                {"name": "update_company", "description": "Update company"},
                {"name": "delete_note", "description": "Delete note"},
                {"name": "find_one_company", "description": "Find one company"},
            ],
        }

    @pytest.mark.asyncio
    async def test_reader_catalog_sees_only_reads(self, identity_env: None) -> None:
        tools = build_crm_tools(READER_SCOPE)
        catalog = _get_tool(tools, "get_tool_catalog")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=self._mock_catalog()):
            result = await catalog.ainvoke({})

        names = {e["name"] for e in result["data"]}
        assert "find_people" in names
        assert "find_one_company" in names
        assert "create_person" not in names
        assert "update_company" not in names

    @pytest.mark.asyncio
    async def test_writer_catalog_sees_only_writes(self, identity_env: None) -> None:
        tools = build_crm_tools(WRITER_SCOPE)
        catalog = _get_tool(tools, "get_tool_catalog")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=self._mock_catalog()):
            result = await catalog.ainvoke({})

        names = {e["name"] for e in result["data"]}
        assert "create_person" in names
        assert "update_company" in names
        assert "delete_note" in names
        assert "find_people" not in names
        assert "find_one_company" not in names


# ---------------------------------------------------------------------------
# Learn guard
# ---------------------------------------------------------------------------

class TestLearnGuard:
    """learn_tools must reject out-of-scope tool names."""

    @pytest.mark.asyncio
    async def test_reader_learn_rejects_writes(self, identity_env: None) -> None:
        tools = build_crm_tools(READER_SCOPE)
        learn = _get_tool(tools, "learn_tools")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await learn.ainvoke({"tool_names": ["create_person"]})

        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writer_learn_rejects_reads(self, identity_env: None) -> None:
        tools = build_crm_tools(WRITER_SCOPE)
        learn = _get_tool(tools, "learn_tools")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await learn.ainvoke({"tool_names": ["find_people"]})

        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writer_learn_allows_writes(self, identity_env: None) -> None:
        tools = build_crm_tools(WRITER_SCOPE)
        learn = _get_tool(tools, "learn_tools")

        mock_response = {"ok": True, "data": {"create_person": {"type": "object"}}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            result = await learn.ainvoke({"tool_names": ["create_person"]})

        assert result["ok"] is True
        mock_fwd.assert_awaited_once()


# ---------------------------------------------------------------------------
# WritePolicy middleware (embedded in execute_tool)
# ---------------------------------------------------------------------------

class TestWritePolicyMiddleware:
    """Test the invisible write-policy middleware inside execute_tool."""

    @pytest.mark.asyncio
    async def test_tier1_executes_transparently(self, identity_env: None) -> None:
        """Tier 1 writes (create_person) go through without confirmation."""
        policy = WritePolicy(session_id="test-middleware")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        mock_response = {"ok": True, "data": {"id": "new-id"}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response):
            result = await execute.ainvoke({"tool": "create_person", "tool_args": {"name": "Ada"}})

        assert result["ok"] is True
        assert result["data"]["id"] == "new-id"

    @pytest.mark.asyncio
    async def test_tier3_returns_confirmation_token(self, identity_env: None) -> None:
        """Tier 3 writes (delete_person) require confirmation — return a token."""
        policy = WritePolicy(session_id="test-tier3")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await execute.ainvoke({"tool": "delete_person", "tool_args": {"id": "p1"}})

        assert result["ok"] is False
        assert result["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert "confirmation_token" in result["error"]
        assert result["error"]["draft"]["tool"] == "delete_person"
        # Bridge must NOT have been called.
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier3_token_confirms_and_executes(self, identity_env: None) -> None:
        """Passing a valid confirmation token executes the tier 3 write."""
        policy = WritePolicy(session_id="test-confirm")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        # Step 1: Get the token.
        with patch("agent.crm_tools.forward", new_callable=AsyncMock):
            blocked = await execute.ainvoke({"tool": "delete_person", "tool_args": {"id": "p1"}})

        token = blocked["error"]["confirmation_token"]

        # Step 2: Confirm with the token.
        mock_response = {"ok": True, "data": {"deleted": True}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            result = await execute.ainvoke({
                "tool": "delete_person",
                "tool_args": {"id": "p1"},
                "confirmation_token": token,
            })

        assert result["ok"] is True
        mock_fwd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, identity_env: None) -> None:
        """An invalid/reused confirmation token is rejected."""
        policy = WritePolicy(session_id="test-bad-token")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result = await execute.ainvoke({
                "tool": "delete_person",
                "tool_args": {"id": "p1"},
                "confirmation_token": "bogus-token",
            })

        assert result["ok"] is False
        assert result["error"]["code"] == "WRITE_BLOCKED"
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_token_is_single_use(self, identity_env: None) -> None:
        """Tokens are consumed after first use — reuse is rejected."""
        policy = WritePolicy(session_id="test-single-use")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        # Get token.
        with patch("agent.crm_tools.forward", new_callable=AsyncMock):
            blocked = await execute.ainvoke({"tool": "delete_person", "tool_args": {"id": "p1"}})
        token = blocked["error"]["confirmation_token"]

        # First use — succeeds.
        mock_response = {"ok": True, "data": {"deleted": True}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response):
            result1 = await execute.ainvoke({
                "tool": "delete_person",
                "tool_args": {"id": "p1"},
                "confirmation_token": token,
            })
        assert result1["ok"] is True

        # Second use — rejected (token consumed).
        with patch("agent.crm_tools.forward", new_callable=AsyncMock) as mock_fwd:
            result2 = await execute.ainvoke({
                "tool": "delete_person",
                "tool_args": {"id": "p1"},
                "confirmation_token": token,
            })
        assert result2["ok"] is False
        assert result2["error"]["code"] == "WRITE_BLOCKED"
        mock_fwd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier2_executes_transparently(self, identity_env: None) -> None:
        """Tier 2 writes (update_opportunity) go through without confirmation."""
        policy = WritePolicy(session_id="test-tier2")
        tools = build_crm_tools(WRITER_SCOPE, write_policy=policy)
        execute = _get_tool(tools, "execute_tool")

        mock_response = {"ok": True, "data": {"id": "opp-1"}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            result = await execute.ainvoke({
                "tool": "update_opportunity",
                "tool_args": {"amount": 1000},
            })

        assert result["ok"] is True
        mock_fwd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_policy_means_no_gate(self, identity_env: None) -> None:
        """Without a write_policy, writes execute without checks."""
        tools = build_crm_tools(WRITER_SCOPE)  # no policy
        execute = _get_tool(tools, "execute_tool")

        mock_response = {"ok": True, "data": {"deleted": True}}
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response):
            result = await execute.ainvoke({"tool": "delete_person", "tool_args": {"id": "p1"}})

        # Tier 3 but no policy — goes through.
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Role isolation
# ---------------------------------------------------------------------------

class TestRoleIsolation:
    """Verify that reader and writer use different bridge roleIds."""

    @pytest.mark.asyncio
    async def test_different_roles_on_bridge_calls(self, identity_env: None) -> None:
        reader_tools = build_crm_tools(READER_SCOPE)
        writer_tools = build_crm_tools(WRITER_SCOPE)

        mock_response = {"ok": True, "data": []}

        # Reader execute_tool (on a read tool).
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            await _get_tool(reader_tools, "execute_tool").ainvoke({"tool": "find_people"})
            reader_role = mock_fwd.call_args[0][1]["roleId"]

        # Writer execute_tool (on a write tool).
        with patch("agent.crm_tools.forward", new_callable=AsyncMock, return_value=mock_response) as mock_fwd:
            await _get_tool(writer_tools, "execute_tool").ainvoke({"tool": "create_person"})
            writer_role = mock_fwd.call_args[0][1]["roleId"]

        assert reader_role == "reader-role"
        assert writer_role == "writer-role"
        assert reader_role != writer_role
