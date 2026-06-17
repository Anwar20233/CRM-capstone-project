"""Unit tests for followup/orchestrator/ — Step 6 LangGraph pipeline.

Fakes only: no Postgres, no bridge, no real LLM. Covers:
* The graph compiles.
* Each entry point (email / risk / orchestrator) flows through the expected
  nodes (asserted via state["trace"]).
* The pending action dict + run log are built correctly and JSON-safe.
* Calendar results reach the draft step when the recommendation schedules a
  meeting.
* A node failure yields status="failed" + an error, not a crash.
* The follow-up task registry: catalog / learn (hot-loaded instructions) /
  execute, plus the out-of-scope guard.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, fields
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from followup.calendar.reader import FakeCalendarReader
from followup.orchestrator import (
    FOLLOWUP_SCOPE,
    OrchestratorDeps,
    TaskContext,
    build_followup_graph,
    build_task_tools,
)
from followup.profile.dependencies import PipelineDeps
from followup.profile.schemas import ContactSummary, DealContext
from followup.store.repositories import PendingAction


# ===========================================================================
# Fakes
# ===========================================================================


def make_deal(*, risk_score: Optional[float] = 0.2) -> DealContext:
    return DealContext(
        opportunity_id=str(uuid.uuid4()),
        opportunity_name="Acme Expansion",
        deal_stage="PROPOSAL",
        deal_value=50_000.0,
        company_name="Acme",
        profile_narrative="Acme is evaluating an expansion of their seats.",
        contacts=[
            ContactSummary(
                crm_id=str(uuid.uuid4()),
                name="Dana Buyer",
                role="VP Ops",
                email="dana@acme.com",
                facts=[],
            )
        ],
        recent_activities=[],
        key_relationships=[],
        open_concerns=[],
        risk_score=risk_score,
    )


class FakeProfileService:
    def __init__(self, deal: DealContext) -> None:
        self._deal = deal
        self.include_shadows_calls: list[bool] = []

    async def build_deal_context(
        self, opportunity_id: str, *, include_shadows: bool = True
    ) -> DealContext:
        self.include_shadows_calls.append(include_shadows)
        return self._deal


class FakePendingRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, action_data: dict[str, Any]) -> PendingAction:
        self.created.append(action_data)
        names = {f.name for f in fields(PendingAction)}
        return PendingAction(**{k: v for k, v in action_data.items() if k in names})


class RaisingPendingRepo:
    async def create(self, action_data: dict[str, Any]) -> PendingAction:
        raise RuntimeError("db down")


class FakeRunRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(self, run_data: dict[str, Any]) -> dict[str, Any]:
        self.created.append(run_data)
        return run_data


class FakeChatModel:
    def __init__(self, content: str = "{}") -> None:
        self._content = content

    async def ainvoke(self, messages: Any) -> Any:
        return SimpleNamespace(content=self._content)


def make_deps(
    deal: DealContext,
    *,
    pending: Any = None,
    runs: Any = None,
    chat_content: str = '{"type":"buying_signal","urgency":"high",'
    '"requires_next_step":true,"requires_risk":false,"requires_calendar":false}',
    calendar: Any = None,
) -> tuple[OrchestratorDeps, FakePendingRepo, FakeRunRepo, FakeProfileService]:
    pending_repo = pending or FakePendingRepo()
    run_repo = runs or FakeRunRepo()
    profile = FakeProfileService(deal)
    pipeline = PipelineDeps(
        executor=None,
        crm_reader=None,
        crm_orchestrator=None,
        notifier=None,
        calendar_reader=calendar or FakeCalendarReader(),
        chat_llm=FakeChatModel(chat_content),
        pending_actions=pending_repo,
        runs=run_repo,
    )
    deps = OrchestratorDeps(pipeline=pipeline, profile_service=profile)
    return deps, pending_repo, run_repo, profile


async def run_graph(deps: OrchestratorDeps, deal: DealContext, **overrides: Any) -> dict:
    graph = build_followup_graph(deps)
    state: dict[str, Any] = {
        "entry_point": "email",
        "trigger": {},
        "opportunity_id": deal.opportunity_id,
        "workspace_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "status": "running",
        "trace": [],
    }
    state.update(overrides)
    return await graph.ainvoke(state)


@pytest.fixture(autouse=True)
def _stub_extraction(monkeypatch):
    # The email entry node runs the reader+extractor (Step 2), which needs a live
    # CRM/db. Stub it so unit tests exercise the pipeline, not extraction.
    async def _fake_extract(**kwargs):
        return None

    monkeypatch.setattr(
        "followup.profile.extraction.extract_from_email", _fake_extract
    )


# ===========================================================================
# Graph compiles
# ===========================================================================


def test_graph_compiles():
    deps, *_ = make_deps(make_deal())
    graph = build_followup_graph(deps)
    assert graph is not None


# ===========================================================================
# Per-entry-point flow (node order via trace)
# ===========================================================================


async def test_email_flow_trace():
    deal = make_deal()
    deps, pending, runs, profile = make_deps(deal)
    result = await run_graph(deps, deal, entry_point="email", trigger={"id": "e1", "sender_email": "dana@acme.com", "body": "hi"})

    assert result["status"] == "completed"
    assert result["trace"] == [
        "extract",
        "load_profile",
        "assess_risk",
        "classify",
        "plan",
        "run_tasks",
        "create_pending",
    ]
    assert result["plan"] is not None
    assert result["draft"] is not None
    assert result["pending_action"] is not None
    # Profile was loaded with shadows excluded.
    assert profile.include_shadows_calls == [False]


async def test_risk_flow_trace():
    deal = make_deal()
    deps, pending, runs, _ = make_deps(deal)
    result = await run_graph(deps, deal, entry_point="risk", trigger={"id": "r1"})

    assert result["status"] == "completed"
    assert result["trace"] == [
        "load_profile",
        "assess_risk",
        "classify",
        "plan",
        "run_tasks",
        "create_pending",
    ]
    assert result["risk_assessment"] is not None
    assert result["plan"] is not None  # synthesized for the draft


async def test_orchestrator_flow_trace():
    deal = make_deal()
    deps, *_ = make_deps(deal)
    result = await run_graph(
        deps,
        deal,
        entry_point="orchestrator",
        trigger={"id": "o1", "instructions": "set up a meeting", "wants_meeting": True},
    )

    assert result["status"] == "completed"
    assert result["trace"] == [
        "load_profile",
        "assess_risk",
        "classify",
        "plan",
        "run_tasks",
        "create_pending",
    ]


# ===========================================================================
# Calendar reaches drafting
# ===========================================================================


async def test_calendar_checked_for_meeting():
    deal = make_deal()
    deps, *_ = make_deps(deal)
    proposed = datetime(2026, 6, 17, 15, 0, tzinfo=timezone.utc).isoformat()
    result = await run_graph(
        deps,
        deal,
        entry_point="orchestrator",
        trigger={"wants_meeting": True, "proposed_times": [proposed]},
    )

    assert result["plan"].headline_action == "schedule_meeting"
    assert result["calendar"] is not None
    # No busy events configured → the proposed time is available.
    assert result["calendar"].all_busy is False
    assert result["calendar"].available_slots[0].available is True
    assert result["draft"] is not None


# ===========================================================================
# Pending action + run log
# ===========================================================================


async def test_pending_action_is_built_and_json_safe():
    deal = make_deal()
    deps, pending, runs, _ = make_deps(deal)
    await run_graph(deps, deal, entry_point="risk", trigger={"id": "r1"})

    assert len(pending.created) == 1
    action = pending.created[0]
    assert action["trigger_type"] == "risk_alert"
    assert action["status"] == "pending"
    assert action["urgency"] in {"high", "medium", "low"}
    assert isinstance(action["expires_at"], datetime)
    assert action["action_type"]  # non-empty
    # The jsonb columns must serialize.
    json.dumps(action["action_payload"])
    assert action["next_step_result"] is not None
    assert action["risk_assessment"] is not None


async def test_run_log_created_with_invoked_agents():
    deal = make_deal()
    deps, pending, runs, _ = make_deps(deal)
    await run_graph(deps, deal, entry_point="email", trigger={"id": "e1"})

    assert len(runs.created) == 1
    run = runs.created[0]
    assert run["entry_point"] == "email_signal"
    assert run["status"] == "completed"
    assert run["pending_action_id"] is not None
    assert "next_step" in run["agents_invoked"]
    assert "drafting" in run["agents_invoked"]


# ===========================================================================
# Failure handling
# ===========================================================================


async def test_node_failure_sets_failed_status():
    deal = make_deal()
    deps, *_ = make_deps(deal, pending=RaisingPendingRepo())
    # ainvoke must not raise even though the repo blows up.
    result = await run_graph(deps, deal, entry_point="risk", trigger={"id": "r1"})

    assert result["status"] == "failed"
    assert result["error"] and "create_pending" in result["error"]
    assert "create_pending" in result["trace"]


# ===========================================================================
# Task registry — catalog / learn (hot load) / execute / scope guard
# ===========================================================================


async def test_task_catalog_and_learn():
    deps, *_ = make_deps(make_deal())
    tools = {t.name: t for t in build_task_tools(deps.task_registry, FOLLOWUP_SCOPE)}

    catalog = await tools["get_task_catalog"].ainvoke({})
    names = {t["name"] for t in catalog["data"]["tasks"]}
    assert names == {"check_calendar", "draft_email", "write_note", "create_task"}

    learned = await tools["learn_task"].ainvoke({"task_names": ["draft_email"]})
    entry = learned["data"]["tasks"][0]
    assert entry["name"] == "draft_email"
    assert entry["instructions"]  # the hot-loaded instructions

    blocked = await tools["learn_task"].ainvoke({"task_names": ["delete_everything"]})
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "OUT_OF_SCOPE"


async def test_execute_task_runs_handler_and_guards_scope():
    deal = make_deal()
    deps, *_ = make_deps(deal)
    tools = {t.name: t for t in build_task_tools(deps.task_registry, FOLLOWUP_SCOPE)}
    execute = tools["execute_task"]

    from followup.contracts.next_step import NextStepPlan, PlannedStep

    plan = NextStepPlan(
        steps=[PlannedStep(kind="draft_email", intent="advance")],
        headline_action="send_proposal",
        summary="advance",
    )
    ctx = TaskContext(
        state={"workspace_id": "ws", "trigger": {}},
        deal_context=deal,
        plan=plan,
        instructions="draft it",
        classification={},
    )

    ok = await execute.coroutine(task="draft_email", context=ctx)
    assert ok["ok"] is True
    assert "draft" in ok["data"]

    out_of_scope = await execute.coroutine(task="rm_rf", context=ctx)
    assert out_of_scope["ok"] is False
    assert out_of_scope["error"]["code"] == "OUT_OF_SCOPE"


async def test_write_note_targets_carry_ids_and_names():
    # The write path hands the writer resolved id+name references, never bare ids.
    deal = make_deal()
    deps, *_ = make_deps(deal)
    from followup.contracts.next_step import NextStepPlan, PlannedStep

    plan = NextStepPlan(
        steps=[PlannedStep(kind="write_note", intent="escalating")],
        headline_action="escalate",
        summary="at risk",
    )
    ctx = TaskContext(
        state={"workspace_id": "ws", "trigger": {}},
        deal_context=deal,
        plan=plan,
        instructions="note it",
        classification={},
    )
    spec = deps.task_registry.get("write_note")
    out = await spec.handler(ctx)
    payload = out["task_results"]["write_note"]

    assert payload["route"] == "orchestrator->writer"
    types = {t["type"] for t in payload["targets"]}
    assert "opportunity" in types and "person" in types
    for target in payload["targets"]:
        assert target["id"] and target["name"]  # id paired with a name


async def test_note_body_is_authored_not_the_next_step_intent():
    # The note body comes from the follow-up agent's own author LLM, never the
    # next-step agent's step.intent wording.
    deal = make_deal()
    deps, *_ = make_deps(deal, chat_content="Acme wants pricing before Q3.")
    from followup.contracts.next_step import NextStepPlan, PlannedStep

    plan = NextStepPlan(
        steps=[PlannedStep(kind="write_note", intent="escalating")],
        headline_action="escalate",
        summary="at risk",
    )
    ctx = TaskContext(
        state={"workspace_id": "ws", "trigger": {}},
        deal_context=deal,
        plan=plan,
        instructions="note it",
        classification={},
    )
    out = await deps.task_registry.get("write_note").handler(ctx)
    body = out["task_results"]["write_note"]["body"]
    assert body == "Acme wants pricing before Q3."
    assert "escalating" not in body  # not the next-step intent


async def test_create_task_title_authored_at_plan_time():
    # create_task now has a prep handler that authors the title via its own LLM.
    deal = make_deal()
    deps, *_ = make_deps(deal, chat_content="Send the signed SOW to Acme")
    from followup.contracts.next_step import NextStepPlan, PlannedStep

    plan = NextStepPlan(
        steps=[PlannedStep(kind="create_task", intent="ignored")],
        headline_action="follow_up_call",
        summary="follow up",
    )
    ctx = TaskContext(
        state={"workspace_id": "ws", "trigger": {}},
        deal_context=deal,
        plan=plan,
        instructions="task it",
        classification={},
    )
    out = await deps.task_registry.get("create_task").handler(ctx)
    payload = out["task_results"]["create_task"]
    assert payload["title"] == "Send the signed SOW to Acme"
    assert payload["route"] == "orchestrator->writer"


async def test_meeting_brief_is_names_and_reason_only():
    # A book_meeting step yields a minimal people+reason brief (no LLM), which is
    # persisted as the calendar event title — never the next-step intent.
    deal = make_deal()
    deps, *_ = make_deps(deal)
    from followup.contracts.next_step import NextStepPlan, PlannedStep

    plan = NextStepPlan(
        steps=[PlannedStep(kind="book_meeting", intent="next-step wording")],
        headline_action="schedule_meeting",
        summary="meet",
    )
    ctx = TaskContext(
        state={"workspace_id": "ws", "trigger": {}},
        deal_context=deal,
        plan=plan,
        instructions="meet",
        classification={},
    )
    out = await deps.task_registry.get("draft_email").handler(ctx)
    title = out["task_results"]["book_meeting"]["title"]
    assert "Dana Buyer" in title and "Acme Expansion" in title
    assert "next-step wording" not in title
