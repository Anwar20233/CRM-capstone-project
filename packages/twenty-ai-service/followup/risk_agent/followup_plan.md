# Redesign the Follow-Up tab into an intuitive workflow + agent chat

## Context

The first cut of the Follow-Up tab is wrong and must be reworked:
- It dumps everything at once (full health narrative, all actions, full email bodies).
- The **risk score is computed at runtime** and mis-rendered ("7800%"). It must be the
  **daily** score read from `followup_agent.risk_daily_scores`.
- **Accept fails** with a raw `write_confirmation` interrupt — the CRM writer pauses for
  tier-3 approval and the executor treats the pause as a failure instead of confirming.
- The chat was a bespoke UI. The platform's chat already **streams from our ai-service
  orchestrator** (`/agent/chat/stream`, NDJSON) — the follow-up chat must work the same
  way but target the **follow-up agent**, scoped to the opportunity, continuing the same
  session with full deal/workflow context.

Key data facts discovered:
- **One pending action = one multi-step workflow**: `action_payload.steps` is a list of
  `{kind, intent, …}` where kind ∈ create_note / create_task / update_stage / draft_email /
  book_meeting (see `followup/api/execution.py` `_steps_for`). Draft email lives in
  `draft_result.{subject,body}`; meeting slot/invitees in `action_payload.calendar` +
  `action_payload.task_results.book_meeting`. This maps directly to the requested grouping.
- Accept resume exists: `WriterWorker.resume(approved=True)` feeds `Command(resume=True)`;
  reached through the `delegate_write` seam (`agent/orchestrator.py`).
- Daily risk read exists: `RiskDailyScoreRepository.get_latest(opportunity_id)`
  (`followup/store/repositories.py`).
- Streaming chat pattern exists: ai-service `/agent/chat/stream` NDJSON + per-session
  `Orchestrator` (`routers/agent.py`); front-end consumes our orchestrator stream in
  `modules/ai/hooks/useAgentChat.ts`.

### Decisions (confirmed)
- **Chat**: embed the **actual platform chat** (the real `AgentChatProvider` + `AiChatTab`
  components and their twenty-server → external-orchestrator streaming path) inside the
  Follow-Up tab, scoped to the opportunity and routed to the **follow-up agent** (not a
  styled lookalike, not a new bespoke chat). The work is wiring/targeting, not rebuilding UI.
- **Toggle**: deactivated steps are skipped on Accept but stay visible (greyed), re-enableable.

---

## Backend (twenty-ai-service)

**B1 — Fix Accept (write-confirmation auto-approve).**
- `agent/orchestrator.py` `delegate_write(...)`: add `auto_approve: bool = False`. After
  `writer.run(instruction)`, if the result is an interrupt and `auto_approve`, call
  `writer.resume(True)` on the same `WriterWorker` and return that result.
- `followup/api/execution.py` `_execute_via_writer`: pass `auto_approve=True` (the rep's
  Accept *is* the approval); drop the "interrupt → failed" branch.

**B2 — Per-step toggles.**
- `AcceptRequest` (`followup/api/models.py`): add `disabled_step_indices: list[int] = []`.
- Thread it through `accept_action` → accept graph state → `FollowupActionExecutor.execute`
  (skip steps whose index is disabled). Inspect `build_accept_graph` for where the executor
  runs and add `disabled_step_indices` to the invoked state.

**B3 — Risk from the daily table.**
- Extend `GET /followup/profile/{id}` (or add `GET /followup/risk/{id}`) to include the
  latest daily score via `RiskDailyScoreRepository.get_latest`: `risk_score`, `risk_level`,
  `top_factors`, `assessed_at`. The UI shows this, not the runtime narrative score.

**B4 — Structured workflow projection for the UI.**
- Extend `PendingActionResponse.from_db` (or add a richer model) to expose a `steps` array
  the UI can group: per step `{kind, title, detail}` derived from `action_payload.steps`
  plus `draft_result` (email subject/body), `action_payload.calendar` (meeting day/time +
  invitees from `task_results.book_meeting`), and note/task titles. Also expose the
  **source email** (subject/body/sender) from the trigger; confirm its storage on the
  action/inbound-email row during implementation.

**B5 — Route the platform chat to the follow-up agent (reuse the streaming path).**
- The platform chat already streams from our ai-service via
  `agent-orchestrator-client.service.ts` → `/agent/chat/stream`. Make that target the
  follow-up agent when the chat is the opportunity Follow-Up chat: the cleanest seam is to
  carry the opportunity id + a "followup" scope from the chat thread through the
  agent-chat resolver/stream job to the client, and have the ai-service route such sessions
  to the follow-up chat agent (`followup/chat/agent.py`) with deal/workflow context, instead
  of the generic `Orchestrator`. Finalize the exact scoping field against
  `ai-chat/resolvers/agent-chat.resolver.ts`, `jobs/stream-agent-chat.job.ts`, and
  `services/agent-orchestrator-client.service.ts` during implementation.
- Reuse the existing tools (list/accept/reject/revise/create) so chat can change the plan;
  revise/create already re-run the pipeline with prior context (`_run_followup_pipeline`).

---

## Frontend (twenty-front, `modules/followup-intelligence`)

**F1 — Grouped, collapsible workflow UI** (rewrite `FollowupIntelligencePanel.tsx`, split
into small components):
- Header: opportunity name + **risk score** from the daily endpoint, rendered correctly
  (fix `OpportunityHealthPanel` `formatRiskScore` — show e.g. `78 / 100`, not `*100%`).
- Per workflow (pending action) card:
  - Title "Workflow from email" + the **source email collapsed by default** (expand to read).
  - Step rows grouped by kind: `New note: <title>`, `New task: <title>`,
    `Calendar: <day, hour>` (expand → message + invitees), `Email draft: <subject>`
    (expand → full body). Reuse a disclosure/accordion from twenty-ui if present, else a
    minimal expandable.
  - A **toggle per step** (deactivate → greyed, excluded from Accept).
  - **Accept / Reject** for the whole workflow (Accept sends `disabled_step_indices`).
- Replace the "dump everything" layout; only titles show until expanded.

**F2 — Embed the actual platform chat** at the bottom of the tab:
- Render the real `AgentChatProvider` + `AiChatTab` (`modules/ai/components/`) inside the
  Follow-Up tab, bound to an opportunity-scoped thread flagged as the follow-up chat, so it
  routes to the follow-up agent (per B5). Delete the bespoke `useFollowupChat` chat box.
- After a turn that changes the plan, refresh the pinned workflow(s).

**F3 — API/types** (`services/followup-api.ts`, `types/followup-action.ts`): add the
streaming chat call, the daily-risk shape, and the structured `steps`/source-email fields;
update `index.ts` barrel.

---

## Verification
- **Accept**: click Accept on a workflow → CRM writes succeed (no interrupt error); psql
  shows the note/task/event created; `execution_status = completed`.
- **Toggles**: disable a step, Accept → only enabled steps execute.
- **Risk**: header shows the daily `risk_daily_scores` value formatted sanely; confirm it
  matches `SELECT risk_score,risk_level FROM followup_agent.risk_daily_scores WHERE opportunity_id=…`.
- **Grouping**: each step type renders collapsed with a title and expands to detail; source
  email collapsed by default.
- **Chat**: `curl -N POST /followup/chat/stream` streams a reply; in the UI the chat streams,
  continues the session, and "revise the email to…" updates the pending workflow.
- `npx nx typecheck twenty-front` + ai-service import/compile; lint the changed files.

## Notes
- All prior changes from this session are staged, not committed.
- ai-service runs hot-reloaded on :8001; server commands need node 24.
- CORS is `*` (dev only).
