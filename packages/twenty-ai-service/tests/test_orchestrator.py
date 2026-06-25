"""Tests for the Orchestrator planner (agent/orchestrator.py).

Verifies:
- The orchestrator's worker is wired with the agent-discovery + memory tools and
  NO CRM tools (tools_override honored).
- Delegating to a stub agent (followup) is recorded — proves the orchestrator
  can activate the complex-agent stubs.
- Multi-turn memory: conversation history grows and is replayed into each run.
- Compaction folds older turns into a summary once over the token limit.
- Regression: a default BaseWorker still builds CRM tools (override is opt-in).
"""

from unittest.mock import AsyncMock

import pytest

from agent.orchestrator import RECENT_TURNS_KEPT, Orchestrator
from agent.stubs.agent_stubs import CALL_LOG, reset_call_log
from agent.tool_scope import READER_SCOPE
from agent.workers.base_worker import BaseWorker


@pytest.fixture
def identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("TWENTY_USER_ID", "user-test")
    monkeypatch.setenv("TWENTY_ROLE_ID", "generic-role")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")
    monkeypatch.setenv("TWENTY_WRITER_ROLE_ID", "writer-role")


@pytest.fixture(autouse=True)
def _clear_call_log() -> None:
    reset_call_log()


class TestOrchestratorToolset:
    def test_has_agent_and_memory_tools(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test")
        names = orch.tool_names
        assert "get_agent_catalog" in names
        assert "learn_agent" in names
        assert "delegate_to_agent" in names
        assert "remember" in names
        assert "recall" in names
        assert "get_session_context" in names

    def test_has_NO_crm_tools(self, identity_env: None) -> None:
        """The orchestrator never touches the bridge — no CRM meta-tools."""
        orch = Orchestrator(session_id="test")
        names = orch.tool_names
        assert "execute_tool" not in names
        assert "get_tool_catalog" not in names
        assert "learn_tools" not in names

    def test_discovery_results_are_not_masked(self, identity_env: None) -> None:
        """Agent-discovery payloads (names/schemas) must bypass PII masking, else
        the NER masker mangles agent names like 'reader' into [PERSON_N]."""
        orch = Orchestrator(session_id="test")
        assert orch._worker.unmasked_tools == frozenset(
            {"get_agent_catalog", "learn_agent"}
        )


class TestStubActivation:
    @pytest.mark.asyncio
    async def test_delegating_to_followup_is_recorded(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test")
        result = await orch._worker.invoke_tool(
            "delegate_to_agent",
            {"agent": "followup", "instruction": "Book a 1h meeting Monday 9:30am"},
        )
        assert result["ok"] is True
        assert result["data"]["agent"] == "followup"
        assert CALL_LOG == [
            {"agent": "followup", "instruction": "Book a 1h meeting Monday 9:30am"}
        ]


class TestSessionMemory:
    @pytest.mark.asyncio
    async def test_history_grows_and_replays(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test")
        fake_run = AsyncMock(return_value={"response": "done", "tool_calls": []})
        orch._worker.run = fake_run  # type: ignore[method-assign]

        await orch.handle("first message")
        await orch.handle("second message")

        # Two turns => 4 stored entries (user + assistant each).
        assert len(orch._turns) == 4
        assert orch._turns[0] == {"role": "user", "content": "first message"}

        # The second run must have replayed turn 1 as prior_messages.
        prior = fake_run.call_args_list[1].kwargs["prior_messages"]
        assert prior == [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "done"},
        ]


class TestCompaction:
    @pytest.mark.asyncio
    async def test_compacts_older_turns_over_limit(self, identity_env: None) -> None:
        # Tiny limit so any history triggers compaction.
        orch = Orchestrator(session_id="test", compaction_token_limit=1)
        orch._summarize = AsyncMock(return_value="SUMMARY")  # type: ignore[method-assign]

        # 8 turns => older = first 2, recent = last RECENT_TURNS_KEPT (6).
        orch._turns = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(2 + RECENT_TURNS_KEPT)
        ]

        await orch._maybe_compact()

        assert orch._summary == "SUMMARY"
        assert len(orch._turns) == RECENT_TURNS_KEPT
        # The summary becomes a system message at the front of the replay.
        prior = orch._build_prior_messages()
        assert prior[0]["role"] == "system"
        assert "SUMMARY" in prior[0]["content"]

    @pytest.mark.asyncio
    async def test_no_compaction_under_limit(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test", compaction_token_limit=10_000)
        orch._summarize = AsyncMock()  # type: ignore[method-assign]
        orch._turns = [{"role": "user", "content": "short"}]

        await orch._maybe_compact()

        orch._summarize.assert_not_awaited()
        assert orch._summary is None


class TestBaseWorkerOverride:
    def test_default_worker_still_builds_crm_tools(self, identity_env: None) -> None:
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="x")
        assert "execute_tool" in worker.tool_names
        assert len(worker.tools) == 4

    def test_override_replaces_crm_tools(self, identity_env: None) -> None:
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="x", tools_override=[])
        assert worker.tools == []


class TestDisambiguation:
    """The mask-time gate: an ambiguous name asks the user, then binds the choice."""

    def _wire(self, orch: Orchestrator, candidates: list[dict]) -> None:
        from agent.masking import EntityHandleMap, Resolution

        def extractor(text: str) -> list[dict]:
            index = text.find("John")
            return (
                [{"label": "person", "text": "John", "start": index, "end": index + 4}]
                if index >= 0
                else []
            )

        orch.pii_map = EntityHandleMap(extractor=extractor)

        class StubResolver:
            async def resolve_company(self, name: str) -> Resolution:
                return Resolution("none", "company", name, [])

            async def resolve_person(self, name: str, company_name: str | None = None) -> Resolution:
                return Resolution("multiple", "person", name, candidates)

        orch.resolver = StubResolver()  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_ambiguous_name_asks_and_does_not_run_worker(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test")
        self._wire(
            orch,
            [
                {"id": "p1", "name": {"firstName": "John", "lastName": "Doe"},
                 "emails": {"primaryEmail": "john@acme.com"}},
                {"id": "p2", "name": {"firstName": "John", "lastName": "Smith"}},
            ],
        )
        orch._worker.run = AsyncMock(return_value={"response": "done", "tool_calls": []})

        result = await orch.handle("email John")

        assert "John" in result["response"]
        assert "1." in result["response"] and "2." in result["response"]
        orch._worker.run.assert_not_awaited()
        assert orch._pending is not None

    @pytest.mark.asyncio
    async def test_choice_binds_handle_and_resumes_original(self, identity_env: None) -> None:
        orch = Orchestrator(session_id="test")
        candidates = [
            {"id": "p1", "name": {"firstName": "John", "lastName": "Doe"},
             "emails": {"primaryEmail": "john@acme.com"}},
            {"id": "p2", "name": {"firstName": "John", "lastName": "Smith"}},
        ]
        self._wire(orch, candidates)
        orch._worker.run = AsyncMock(return_value={"response": "done", "tool_calls": []})

        await orch.handle("email John")  # turn 1: ambiguous
        result = await orch.handle("1")  # turn 2: pick the first candidate

        # Worker ran on the resumed original message, ambiguity cleared.
        orch._worker.run.assert_awaited_once()
        assert orch._worker.run.call_args.args[0] == "email John"
        assert orch._pending is None
        # The chosen record is now a resolved handle.
        handle = orch.pii_map.handle_for_surface("John")
        assert handle is not None and handle.record_id == "p1"
        assert result["response"] == "done"
