"""Tests for ReaderWorker prompt content and CLI wiring."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from agent.tool_scope import READER_SCOPE
from agent.workers.reader_worker import READER_SYSTEM_PROMPT, ReaderWorker


@pytest.fixture
def identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("TWENTY_USER_ID", "user-test")
    monkeypatch.setenv("TWENTY_ROLE_ID", "generic-role")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")


class TestReaderSystemPrompt:
    def test_uses_tool_args_not_arguments(self) -> None:
        assert "tool_args=" in READER_SYSTEM_PROMPT
        assert 'arguments={' not in READER_SYSTEM_PROMPT

    def test_no_filter_wrapper_in_examples(self) -> None:
        assert '"filter":' not in READER_SYSTEM_PROMPT
        assert "top-level" in READER_SYSTEM_PROMPT
        assert "NOT inside a `filter` key" in READER_SYSTEM_PROMPT

    def test_documents_ilike_for_name_search(self) -> None:
        assert "ilike" in READER_SYSTEM_PROMPT

    def test_documents_company_id_for_relation_filters(self) -> None:
        assert "companyId" in READER_SYSTEM_PROMPT
        assert '"company": { "eq"' not in READER_SYSTEM_PROMPT

    def test_documents_handle_id_not_bare_handle(self) -> None:
        assert "company002.id" in READER_SYSTEM_PROMPT
        assert "never bare `company002`" in READER_SYSTEM_PROMPT


class TestReaderWorker:
    def test_uses_reader_scope(self, identity_env: None) -> None:
        worker = ReaderWorker(session_id="test")
        assert worker.scope is READER_SCOPE

    def test_has_read_meta_tools_only(self, identity_env: None) -> None:
        worker = ReaderWorker(session_id="test")
        names = set(worker.tool_names)
        assert {"get_tool_catalog", "learn_tools", "execute_tool"}.issubset(names)
        assert "create_person" not in names


class TestChatCliReaderWiring:
    def test_build_worker_reader_returns_reader_worker(self, identity_env: None) -> None:
        chat_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "chat.py"
        spec = importlib.util.spec_from_file_location("chat_script", chat_path)
        assert spec is not None and spec.loader is not None
        chat_module = importlib.util.module_from_spec(spec)
        sys.modules["chat_script"] = chat_module
        spec.loader.exec_module(chat_module)

        worker = chat_module.build_worker("reader", model=None, session_id="cli-test")
        assert isinstance(worker, ReaderWorker)
