"""Tests for the Writer worker and WritePolicy.

Verifies:
- WriterWorker has only write + meta + resolve_date tools (NO read, NO safety/session).
- BaseWorker with READER_SCOPE has only read + meta tools.
- WritePolicy gate: tier 1/2/3 flows.
- Confirmation tokens: generation, validation, single-use, expiry.
- resolve_date utility tool works via invoke_tool.
"""

import time

import pytest

from agent.tool_scope import READER_SCOPE
from agent.workers.base_worker import BaseWorker
from agent.workers.write_policy import WritePolicy
from agent.workers.writer_worker import WriterWorker


@pytest.fixture
def identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required identity env vars."""
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("TWENTY_USER_ID", "user-test")
    monkeypatch.setenv("TWENTY_ROLE_ID", "generic-role")
    monkeypatch.setenv("TWENTY_WRITER_ROLE_ID", "writer-role")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")


# ---------------------------------------------------------------------------
# WriterWorker toolset — must have write + meta + resolve_date ONLY
# ---------------------------------------------------------------------------

class TestWriterWorkerToolset:
    """Verify the WriterWorker has exactly the right tools."""

    def test_writer_has_meta_tools(self, identity_env: None) -> None:
        worker = WriterWorker(session_id="test")
        names = worker.tool_names

        assert "get_tool_catalog" in names
        assert "learn_tools" in names
        assert "execute_tool" in names
        assert "get_current_user" in names

    def test_writer_has_resolve_date(self, identity_env: None) -> None:
        worker = WriterWorker(session_id="test")
        assert "resolve_date" in worker.tool_names

    def test_writer_does_NOT_have_safety_tools(self, identity_env: None) -> None:
        """Safety tools are invisible middleware, not LLM-facing tools."""
        worker = WriterWorker(session_id="test")
        names = worker.tool_names

        assert "lookup_action_tier" not in names
        assert "check_conflicts" not in names

    def test_writer_does_NOT_have_session_tools(self, identity_env: None) -> None:
        """Session tools are invisible middleware, not LLM-facing tools."""
        worker = WriterWorker(session_id="test")
        names = worker.tool_names

        assert "session_set_topic" not in names
        assert "session_get_topic" not in names
        assert "session_log_write" not in names
        assert "session_get_write_log" not in names
        assert "session_check_duplicate" not in names

    def test_writer_total_tool_count(self, identity_env: None) -> None:
        """Writer should have exactly 5 tools: 4 meta + resolve_date."""
        worker = WriterWorker(session_id="test")
        assert len(worker.tools) == 5


# ---------------------------------------------------------------------------
# Reader worker toolset — no write, no safety, no session
# ---------------------------------------------------------------------------

class TestReaderWorkerToolset:
    """Verify a reader worker has only read + meta tools."""

    def test_reader_has_meta_tools(self, identity_env: None) -> None:
        worker = BaseWorker(
            scope=READER_SCOPE,
            system_prompt="You are a read-only CRM agent.",
        )
        names = worker.tool_names
        assert "get_tool_catalog" in names
        assert "learn_tools" in names
        assert "execute_tool" in names
        assert "get_current_user" in names

    def test_reader_has_NO_safety_or_session_tools(self, identity_env: None) -> None:
        worker = BaseWorker(
            scope=READER_SCOPE,
            system_prompt="You are a read-only CRM agent.",
        )
        names = worker.tool_names

        # No safety
        assert "lookup_action_tier" not in names
        assert "check_conflicts" not in names
        # No session
        assert "session_log_write" not in names
        assert "session_check_duplicate" not in names
        # No resolve_date (not added as extra tool)
        assert "resolve_date" not in names

    def test_reader_total_tool_count(self, identity_env: None) -> None:
        """Reader should have exactly 4 tools: the 4 meta-tools."""
        worker = BaseWorker(
            scope=READER_SCOPE,
            system_prompt="Read-only.",
        )
        assert len(worker.tools) == 4


# ---------------------------------------------------------------------------
# WritePolicy — unit tests
# ---------------------------------------------------------------------------

class TestWritePolicy:
    """Test the write-policy gate directly."""

    @pytest.mark.asyncio
    async def test_tier1_allowed(self) -> None:
        """Tier 1 action (create_person) is allowed immediately."""
        policy = WritePolicy(session_id="test")
        decision = await policy.gate("create_person", {"name": "Ada"})

        assert decision.allowed is True
        assert decision.tier == 1
        assert decision.confirmation_token is None

    @pytest.mark.asyncio
    async def test_tier2_allowed(self) -> None:
        """Tier 2 action is allowed (no writer-side conflict checks)."""
        policy = WritePolicy(session_id="test")
        decision = await policy.gate("create_opportunity", {"name": "Big Deal"})

        assert decision.allowed is True
        assert decision.tier == 2

    @pytest.mark.asyncio
    async def test_tier3_returns_token(self) -> None:
        """Tier 3 action returns a confirmation token, not allowed."""
        policy = WritePolicy(session_id="test")
        decision = await policy.gate("delete_person", {"id": "p1"})

        assert decision.allowed is False
        assert decision.tier == 3
        assert decision.confirmation_token is not None
        assert len(decision.confirmation_token) > 0

    @pytest.mark.asyncio
    async def test_token_confirms_execution(self) -> None:
        """Valid token allows tier 3 execution."""
        policy = WritePolicy(session_id="test")

        # Get token.
        decision1 = await policy.gate("delete_person", {"id": "p1"})
        token = decision1.confirmation_token

        # Confirm with token.
        decision2 = await policy.gate("delete_person", {"id": "p1"}, confirmation_token=token)
        assert decision2.allowed is True
        assert decision2.tier == 3

    @pytest.mark.asyncio
    async def test_token_single_use(self) -> None:
        """Tokens are consumed — reuse is rejected."""
        policy = WritePolicy(session_id="test")

        decision1 = await policy.gate("delete_person", {"id": "p1"})
        token = decision1.confirmation_token

        # First use.
        decision2 = await policy.gate("delete_person", {"id": "p1"}, confirmation_token=token)
        assert decision2.allowed is True

        # Second use — rejected.
        decision3 = await policy.gate("delete_person", {"id": "p1"}, confirmation_token=token)
        assert decision3.allowed is False
        assert "invalid" in decision3.reason.lower() or "used" in decision3.reason.lower()

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self) -> None:
        """A bogus token is rejected."""
        policy = WritePolicy(session_id="test")
        decision = await policy.gate("delete_person", {"id": "p1"}, confirmation_token="bogus")

        assert decision.allowed is False

    @pytest.mark.asyncio
    async def test_token_wrong_action_rejected(self) -> None:
        """Token issued for one action cannot be used for another."""
        policy = WritePolicy(session_id="test")

        decision1 = await policy.gate("delete_person", {"id": "p1"})
        token = decision1.confirmation_token

        # Try to use it for a different action.
        decision2 = await policy.gate("delete_company", {"id": "c1"}, confirmation_token=token)
        assert decision2.allowed is False
        assert "delete_person" in decision2.reason

    @pytest.mark.asyncio
    async def test_unknown_action_escalates_to_tier3(self) -> None:
        """Unknown actions escalate to tier 3 (fail safe)."""
        policy = WritePolicy(session_id="test")
        decision = await policy.gate("exotic_destructive_action", {})

        assert decision.allowed is False
        assert decision.tier == 3
        assert decision.confirmation_token is not None

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self) -> None:
        """Expired confirmation tokens are rejected."""
        policy = WritePolicy(session_id="test")
        decision1 = await policy.gate("delete_person", {"id": "p1"})
        token = decision1.confirmation_token

        # Manually expire the token.
        policy._pending[token].created_at = time.time() - 700  # > 600s TTL

        decision2 = await policy.gate("delete_person", {"id": "p1"}, confirmation_token=token)
        assert decision2.allowed is False
        assert "expired" in decision2.reason.lower()


# ---------------------------------------------------------------------------
# Utility tools — resolve_date via invoke_tool
# ---------------------------------------------------------------------------

class TestUtilityTools:
    """Test utility tools accessible through the worker."""

    @pytest.mark.asyncio
    async def test_resolve_date_known(self, identity_env: None) -> None:
        worker = WriterWorker(session_id="test")
        result = await worker.invoke_tool("resolve_date", {"text": "next friday"})

        assert result["ok"] is True
        assert "iso" in result["data"]

    @pytest.mark.asyncio
    async def test_resolve_date_unknown(self, identity_env: None) -> None:
        worker = WriterWorker(session_id="test")
        result = await worker.invoke_tool("resolve_date", {"text": "the ides of march"})

        assert result["ok"] is False
        assert result["error"]["code"] == "UNKNOWN_DATE_PHRASE"

    @pytest.mark.asyncio
    async def test_invoke_unknown_tool(self, identity_env: None) -> None:
        worker = WriterWorker(session_id="test")
        result = await worker.invoke_tool("nonexistent_tool", {})

        assert result["ok"] is False
        assert result["error"]["code"] == "UNKNOWN_TOOL"
