"""Tests for the Tool Capability Registry (agent/tool_scope.py).

Verifies:
- Verb-prefix classification (read, write, meta, internal).
- Scope guards: is_tool_allowed, filter_catalog.
- Reader scope = read + meta.  Writer scope = write + meta.
- Reader blocks writes.  Writer blocks reads.
- Internal tools (safety/session) are blocked by both scopes.
"""

import pytest

from agent.tool_scope import (
    Capability,
    DENIED_TOOLS,
    READER_SCOPE,
    WRITER_SCOPE,
    ToolScope,
    classify_tool,
    filter_catalog,
    is_tool_allowed,
    is_write_tool,
)


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------

class TestClassifyTool:
    """Verify verb-prefix → capability mapping."""

    @pytest.mark.parametrize(
        "tool_name, expected",
        [
            # READ verbs
            ("find_people", Capability.READ),
            ("find_one_person", Capability.READ),
            ("find_companies", Capability.READ),
            ("find_one_company", Capability.READ),
            ("find_opportunities", Capability.READ),
            ("group_by_person", Capability.READ),
            ("get_pipeline_stages", Capability.READ),
            ("search_records", Capability.READ),
            ("list_views", Capability.READ),
            # WRITE verbs
            ("create_person", Capability.WRITE),
            ("create_many_people", Capability.WRITE),
            ("update_person", Capability.WRITE),
            ("update_many_people", Capability.WRITE),
            ("delete_person", Capability.WRITE),
            ("delete_many_people", Capability.WRITE),
            ("advance_deal_stage", Capability.WRITE),
            ("link_person_to_company", Capability.WRITE),
            ("transfer_ownership", Capability.WRITE),
            ("merge_people", Capability.WRITE),
            ("restore_person", Capability.WRITE),
            # META tools
            ("get_tool_catalog", Capability.META),
            ("learn_tools", Capability.META),
            ("execute_tool", Capability.META),
            ("get_current_user", Capability.META),
            # INTERNAL tools (safety/session — never exposed to LLM)
            ("lookup_action_tier", Capability.INTERNAL),
            ("check_conflicts", Capability.INTERNAL),
            ("resolve_date", Capability.INTERNAL),
            ("session_set_topic", Capability.INTERNAL),
            ("session_get_topic", Capability.INTERNAL),
            ("session_log_write", Capability.INTERNAL),
            ("session_get_write_log", Capability.INTERNAL),
            ("session_check_duplicate", Capability.INTERNAL),
        ],
    )
    def test_classification(self, tool_name: str, expected: Capability) -> None:
        assert classify_tool(tool_name) == expected

    def test_unknown_tool_defaults_to_read(self) -> None:
        """Unknown tools default to READ (conservative)."""
        assert classify_tool("some_exotic_tool") == Capability.READ


# ---------------------------------------------------------------------------
# is_tool_allowed
# ---------------------------------------------------------------------------

class TestIsToolAllowed:
    """Verify scope guards."""

    # -- Reader scope --

    def test_reader_allows_read_tools(self) -> None:
        assert is_tool_allowed("find_people", READER_SCOPE) is True
        assert is_tool_allowed("find_one_person", READER_SCOPE) is True
        assert is_tool_allowed("group_by_company", READER_SCOPE) is True

    def test_reader_allows_meta_tools(self) -> None:
        assert is_tool_allowed("get_tool_catalog", READER_SCOPE) is True
        assert is_tool_allowed("learn_tools", READER_SCOPE) is True
        assert is_tool_allowed("execute_tool", READER_SCOPE) is True

    def test_reader_blocks_write_tools(self) -> None:
        assert is_tool_allowed("create_person", READER_SCOPE) is False
        assert is_tool_allowed("update_opportunity", READER_SCOPE) is False
        assert is_tool_allowed("delete_company", READER_SCOPE) is False

    def test_reader_blocks_internal_tools(self) -> None:
        assert is_tool_allowed("lookup_action_tier", READER_SCOPE) is False
        assert is_tool_allowed("session_log_write", READER_SCOPE) is False

    # -- Writer scope --

    def test_writer_allows_write_tools(self) -> None:
        assert is_tool_allowed("create_person", WRITER_SCOPE) is True
        assert is_tool_allowed("update_opportunity", WRITER_SCOPE) is True
        assert is_tool_allowed("delete_company", WRITER_SCOPE) is True

    def test_writer_allows_meta_tools(self) -> None:
        assert is_tool_allowed("get_tool_catalog", WRITER_SCOPE) is True
        assert is_tool_allowed("learn_tools", WRITER_SCOPE) is True

    def test_writer_blocks_read_tools(self) -> None:
        """Writer cannot read — that's the reader's job."""
        assert is_tool_allowed("find_people", WRITER_SCOPE) is False
        assert is_tool_allowed("find_one_person", WRITER_SCOPE) is False
        assert is_tool_allowed("group_by_company", WRITER_SCOPE) is False

    def test_writer_blocks_internal_tools(self) -> None:
        assert is_tool_allowed("lookup_action_tier", WRITER_SCOPE) is False
        assert is_tool_allowed("session_log_write", WRITER_SCOPE) is False


# ---------------------------------------------------------------------------
# is_write_tool
# ---------------------------------------------------------------------------

class TestIsWriteTool:
    """Verify the is_write_tool helper."""

    def test_write_tools_detected(self) -> None:
        assert is_write_tool("create_person") is True
        assert is_write_tool("update_company") is True
        assert is_write_tool("delete_note") is True

    def test_non_write_tools(self) -> None:
        assert is_write_tool("find_people") is False
        assert is_write_tool("get_tool_catalog") is False
        assert is_write_tool("lookup_action_tier") is False


# ---------------------------------------------------------------------------
# filter_catalog
# ---------------------------------------------------------------------------

class TestFilterCatalog:
    """Verify catalog filtering by scope."""

    _SAMPLE_CATALOG = [
        {"name": "find_people", "description": "Find people"},
        {"name": "find_one_person", "description": "Find one person"},
        {"name": "create_person", "description": "Create a person"},
        {"name": "update_person", "description": "Update a person"},
        {"name": "delete_person", "description": "Delete a person"},
        {"name": "group_by_company", "description": "Group by company"},
    ]

    def test_reader_catalog_sees_only_reads(self) -> None:
        filtered = filter_catalog(self._SAMPLE_CATALOG, READER_SCOPE)
        names = {e["name"] for e in filtered}

        assert "find_people" in names
        assert "find_one_person" in names
        assert "group_by_company" in names
        assert "create_person" not in names
        assert "update_person" not in names
        assert "delete_person" not in names

    def test_writer_catalog_sees_only_writes(self) -> None:
        filtered = filter_catalog(self._SAMPLE_CATALOG, WRITER_SCOPE)
        names = {e["name"] for e in filtered}

        assert "create_person" in names
        assert "update_person" in names
        assert "delete_person" in names
        assert "find_people" not in names
        assert "find_one_person" not in names
        assert "group_by_company" not in names

    def test_empty_catalog(self) -> None:
        assert filter_catalog([], READER_SCOPE) == []


# ---------------------------------------------------------------------------
# ToolScope.role_id
# ---------------------------------------------------------------------------

class TestToolScopeRoleId:
    """Verify role_id resolution from env vars."""

    def test_reader_role_from_specific_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role-123")
        assert READER_SCOPE.role_id == "reader-role-123"

    def test_fallback_to_generic_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TWENTY_READER_ROLE_ID", raising=False)
        monkeypatch.setenv("TWENTY_ROLE_ID", "generic-role-456")
        assert READER_SCOPE.role_id == "generic-role-456"

    def test_raises_when_no_role_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TWENTY_READER_ROLE_ID", raising=False)
        monkeypatch.delenv("TWENTY_ROLE_ID", raising=False)
        with pytest.raises(RuntimeError, match="TWENTY_READER_ROLE_ID"):
            _ = READER_SCOPE.role_id

    def test_writer_role_from_specific_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TWENTY_WRITER_ROLE_ID", "writer-role-789")
        assert WRITER_SCOPE.role_id == "writer-role-789"


# ---------------------------------------------------------------------------
# Configurable allow/deny — capability overrides + deny-list
# ---------------------------------------------------------------------------

class TestConfigurableAllowDeny:
    """ACTION-category tools are classified by override, and DENIED_TOOLS are
    blocked for every scope."""

    def test_action_tools_classified_by_override(self) -> None:
        # Outbound actions → writer-only.
        assert classify_tool("send_email") == Capability.WRITE
        assert classify_tool("draft_email") == Capability.WRITE
        # Read-only helper.
        assert classify_tool("search_help_center") == Capability.READ

    def test_send_email_is_writer_only(self) -> None:
        assert is_tool_allowed("send_email", WRITER_SCOPE) is True
        assert is_tool_allowed("send_email", READER_SCOPE) is False

    def test_denied_tools_blocked_for_all_scopes(self) -> None:
        for tool in DENIED_TOOLS:
            assert is_tool_allowed(tool, READER_SCOPE) is False
            assert is_tool_allowed(tool, WRITER_SCOPE) is False

    def test_denied_tools_dropped_from_catalog(self) -> None:
        catalog = [
            {"name": "find_people", "description": "ok"},
            {"name": "http_request", "description": "denied"},
            {"name": "code_interpreter", "description": "denied"},
        ]
        names = {e["name"] for e in filter_catalog(catalog, READER_SCOPE)}
        assert "find_people" in names
        assert "http_request" not in names
        assert "code_interpreter" not in names
