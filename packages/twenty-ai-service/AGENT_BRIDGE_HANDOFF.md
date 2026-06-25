# Agent Bridge — Handoff & Next Steps

This explains the bridge + agent layer that now exists, and the work still to do
on top of it. Read it with
[CRM_AGENT_TOOL_MAPPING_AND_PLAN.md](../twenty-server/docs/CRM_AGENT_TOOL_MAPPING_AND_PLAN.md)
open — this is the "what's built + who does what next" companion to that plan.

> TL;DR — the plumbing (Node bridge + Python meta-tools an LLM can actually call)
> is **done and verified**, and the **Writer agent is now built** on top of it: a
> scope-restricted, tier-gated write worker on a reusable `BaseWorker` foundation.
> What's left: the **Reader worker**, the **Orchestrator** (owns routing, session
> memory, conflict/entity resolution, and corrections), the real **tier catalog**,
> and the cross-cutting **security + masking** layer. Find your piece in §4.

---

## 1. What already exists (done + verified)

### Node side — the unguarded executor
`packages/twenty-server/src/engine/api/agent-bridge/`

A NestJS module modeled on Twenty's MCP module but with **no auth guards**. Four
POST endpoints (mounted at the server root, **no `/api` prefix**):

| Endpoint | Purpose |
| :--- | :--- |
| `POST /agent-bridge/execute` | run any tool: `{ tool, args, workspaceId, roleId, userId?, userWorkspaceId? }` → `ToolRegistryService.resolveAndExecute` |
| `POST /agent-bridge/catalog` | list tools (optionally by `categories`) |
| `POST /agent-bridge/learn` | fetch full JSON input schemas for named tools |
| `POST /agent-bridge/current-user` | resolve the workspace member for a `userId` |

All responses use one envelope: `{ ok: true, data }` or `{ ok: false, error: { code, message } }`.
`execute` auto-resolves `userWorkspaceId` from `userId + workspaceId`. **Twenty
filters tools by `roleId`** — a read-only role only exposes `find/find_one/group_by`;
a write-capable role also exposes `create/update/delete`. The agent layer relies on
this for its final enforcement (see §3).

**Security posture is intentional:** the bridge trusts whatever `workspaceId` /
`roleId` / `userId` it is given. That is the whole reason the security layer must
live in Python (§4C).

### Python side — what an LLM calls
`packages/twenty-ai-service/`

| File | What it is |
| :--- | :--- |
| `bridge_client.py` | one async `forward(path, payload)` to the Node bridge (URL, timeout, error envelope) |
| `routers/bridge.py` | FastAPI proxy `/bridge/*` (snake_case → camelCase) — handy for curl/debugging |
| `agent/crm_tools.py` | **the LLM-facing layer**: `build_crm_tools(scope, write_policy)` returns scoped meta-tools |
| `agent/tool_scope.py` | **capability registry** — read/write/meta classification, scopes, allow/deny config |
| `agent/workers/base_worker.py` | reusable tool-calling loop (the foundation every worker extends) |
| `agent/workers/writer_worker.py` | the **Writer agent** (built) |
| `agent/workers/write_policy.py` | invisible write-gate middleware (tier + confirmation token) |
| `agent/stubs/safety_tools.py` | hardcoded `_lookup_action_tier` + `resolve_date` stubs |

`agent/crm_tools.py` follows Twenty's **progressive-disclosure** pattern: an LLM is
never handed 254 tools. It gets four meta-tools and discovers the rest at runtime:

```
get_tool_catalog(categories?)   # browse (filtered to the worker's scope)
learn_tools(tool_names)         # fetch schemas (rejects out-of-scope names)
execute_tool(tool, tool_args)   # run a tool by name (scope-guarded; write-gated)
get_current_user()              # "act as me"
```

Identity (`TWENTY_WORKSPACE_ID` / role id / `TWENTY_USER_ID`) is injected
server-side and **never exposed to the model**. This is a placeholder for real
session identity (§4C).

### Built: the Writer agent + scope foundation
The agent layer is now a **multi-worker architecture**, not a single agent:

- **`BaseWorker`** — a generic LLM tool-calling loop parametrised by a `ToolScope`,
  a system prompt, and an optional `WritePolicy`. Future agents are just
  `BaseWorker(scope=…, system_prompt=…)` — no new plumbing.
- **`WriterWorker`** — `BaseWorker` + `WRITER_SCOPE` (**write + meta only, no read**)
  + a write-focused prompt + a `WritePolicy`. The LLM sees **5 tools**: the 4
  meta-tools + `resolve_date`.
- **`WritePolicy`** — *invisible middleware* embedded inside `execute_tool`, **not**
  LLM-facing tools. On every write it looks up the action's tier and:
  - **Tier 1/2** → executes transparently.
  - **Tier 3** (and unknown actions, fail-safe) → blocks and returns
    `CONFIRMATION_REQUIRED` + a single-use, 10-minute, action-bound token. The
    write only runs when the token is passed back to `execute_tool`.

### Verified
- All **254** tools discoverable via `catalog`; schemas via `learn`; create→delete round-trip works.
- A real OpenAI agent drove the full meta-tool loop end-to-end.
- **96 unit tests** cover scope guards, catalog filtering, the deny-list/overrides,
  the tier flow, and the full confirmation-token lifecycle (generate → validate →
  single-use → expiry → wrong-action).

---

## 2. How to run & test

```bash
# 1. Node (Twenty server) must be up with Postgres + Redis:
bash packages/twenty-utils/setup-dev-env.sh
npx nx start twenty-server                      # serves /agent-bridge on :3000

# 2. Python service:
cd packages/twenty-ai-service
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export TWENTY_WORKSPACE_ID=... TWENTY_USER_ID=...
export TWENTY_READER_ROLE_ID=...                # read-only Twenty role
export TWENTY_WRITER_ROLE_ID=...                # read+write Twenty role
# (TWENTY_ROLE_ID still works as a fallback for both if you only have one role)
.venv/bin/uvicorn main:app --reload --port 8000
.venv/bin/python -m pytest -q                   # run the unit tests
```

Get test IDs from the DB:
```sql
SELECT id FROM core.workspace WHERE "activationStatus"='ACTIVE';
SELECT id, label FROM core.role WHERE "workspaceId"='<WS>';   -- need a reader + a writer role
SELECT u.id FROM core."user" u
  JOIN core."userWorkspace" uw ON uw."userId"=u.id WHERE uw."workspaceId"='<WS>';
```

Drive the Writer directly:
```python
from agent.workers import WriterWorker
worker = WriterWorker(session_id="session-abc-123")
await worker.run("Create a person named Sarah Connor")
```

---

## 3. Architecture you must build on (don't break these seams)

### Read/write scoping — three enforcement layers
Defined once in `agent/tool_scope.py`. A worker only ever touches tools in its scope:

1. **Catalog filtering** — `get_tool_catalog` drops out-of-scope names.
2. **Execute/learn guard** — `execute_tool` / `learn_tools` reject out-of-scope
   names *before* the bridge call (`OUT_OF_SCOPE`).
3. **Bridge role identity** — the reader forwards `TWENTY_READER_ROLE_ID`, the writer
   `TWENTY_WRITER_ROLE_ID`; Twenty's role permissions are the final backstop.

`classify_tool()` tags each tool by verb prefix: READ (`find_*`, `get_*`,
`group_by_*`, `search_*`, `list_*`), WRITE (`create_*`, `update_*`, `delete_*`,
`advance_*`, `link_*`, …), META (the four meta-tools).
`READER_SCOPE = {read, meta}`, `WRITER_SCOPE = {write, meta}`.

### Configurable allow/deny (edit anytime)
ACTION-category tools (`send_email`, `http_request`, …) have no read/write prefix.
Two editable collections in `tool_scope.py` handle them:

- **`_CAPABILITY_OVERRIDES`** — assign a capability by exact name
  (`send_email`/`draft_email` → WRITE, `search_help_center` → READ). Extend as the
  catalog grows.
- **`DENIED_TOOLS`** — hard deny-list (`code_interpreter`, `http_request`); never
  exposed to any worker, checked before capability. Mirrors `MCP_EXCLUDED_TOOL_NAMES`.

### Writer safety is middleware, not tools
`WritePolicy` (tier lookup + confirmation tokens) lives **inside** `execute_tool`.
Do **not** expose tier/safety/session helpers as LLM tools — the model never sees
them. The writer owns *only* its own structural write-safety; everything
cross-worker (below) is the **orchestrator's** job.

---

## 4. What the team builds — the tool + endpoint layer

**Scope boundary (read this first).** The team builds **tools and endpoints** that any
agent can call — it does **not** build agents. The *Writer* is already built (§1); the
*Reader* (Majid) and the *Orchestrator* (built by the agent owners) are **out of scope
for the team**. Build the capabilities below so those agents have something to call.

Two owners, same as before.

### 👤 Person 1 — Security, Masking & the Tier Catalog

**A. Security (highest priority).** The bridge is open by design; nothing in front of it
is. Close that:
1. **Network boundary** — bind the Node bridge to localhost / internal only.
2. **Authenticate the Python entrypoint** (`routers/bridge.py` + the agent HTTP surface).
3. **Identity from the authenticated session**, server-side — replace the env-var
   placeholder; the LLM only ever sees tool names + args.
4. **Two least-privilege roles** — a read-only role and a write role
   (`TWENTY_READER_ROLE_ID` / `TWENTY_WRITER_ROLE_ID`). The deny-list/overrides in
   `tool_scope.py` (§3) are the per-tool half of this — extend them as needed.

**B. Masking.** Wrap **`forward()`** (so *every* path inherits it, including composite
read tools that call `forward` directly): detect PII outbound and replace with stable
per-session tokens (`{{person_1}}`); swap tokens back inbound. GLiNER NER already exists
at `routers/ner.py`.

**C. Tier catalog + conflict checker.** `agent/stubs/safety_tools.py` ships a hardcoded
`_ACTION_TIER_MAP` + `_lookup_action_tier`. Replace it with the **real action catalog**
(every write action → tier 1/2/3, required fields, ambiguity-escalation), and add a
**conflict checker** (order-of-magnitude value change, stage regression, past dates).
These are **plain functions / endpoints, not LLM tools**: `_lookup_action_tier` is
already wired into the writer's `WritePolicy`; the conflict checker is consumed by the
orchestrator. Keep the `{ ok, data: { tier, required_fields, escalated } }` envelope so
it stays a drop-in for the stub.

### 👤 Person 2 — Session endpoints, composite read tools & utilities

**A. Session endpoints + store.** Build the underlying capability that used to be
sketched as tools — a state store (in-memory for dev, Redis/Postgres later) behind
endpoints the **orchestrator** calls (these are **not** LLM-facing tools, and the
session id rides with the authenticated identity, never the model):
- `session_set_topic` / `session_get_topic`
- `session_log_write` (append `{tool, args, old_value, new_value, ts}`) / `session_get_write_log`
- `session_check_duplicate` (compare an intended write against the log)

**B. Composite read/write tools** (for the Reader/Writer to consume). One call fans out several bridge
reads/writes and merges, e.g. `get_company_overview` (company + people + opportunities),
`get_entity_timeline` (notes + tasks merged + sorted). Pattern: N `forward("execute", …)`
via `asyncio.gather`, merge in Python, return. These auto-classify as READ.
*Open integration point:* composite tools are local `StructuredTool`s, not bridge tools —
agree on how they register into a worker's toolset and stay scope-filtered (today
`base_worker`'s `extra_tools` bypasses the scope filter).

**C. Utilities.**
- `resolve_date` — a stub ships in `safety_tools.py` (hardcoded map). Make it real
  (relative → absolute ISO, 40+ cases). This one **is** LLM-facing (a data helper).
- `get_pipeline_stages` — METADATA read of the opportunity `stage` options (via
  `execute_tool("get_field_metadata", …)`, no masking). Feeds Person 1's conflict checker.

### Not the team's job (don't build these)
- **Reader worker** (`BaseWorker` + `READER_SCOPE`) — Majid.
- **Orchestrator** — routing, session *use*, conflict/entity resolution, corrections.
  It *calls* the endpoints/tools above; the team only provides them.

---

## 5. Contracts you must not break

- **Bridge envelope:** always `{ ok, data }` / `{ ok, error: { code, message } }` — every
  new endpoint/tool returns it too.
- **`execute_tool` args:** `tool_args` (not `args` — collides with LangChain), plus an
  optional `confirmation_token` for tier-3 actions.
- **Confirmation flow is built — don't rebuild it.** Tier-3 writes return
  `{ code: "CONFIRMATION_REQUIRED", confirmation_token, draft }`; the caller/UI surfaces
  the draft and re-sends `execute_tool(..., confirmation_token=…)`. Tokens are
  single-use, action-bound, 10-min TTL.
- **Safety/session/tier are middleware/endpoints, not LLM tools** — never add them to a
  worker's toolset. The model sees only the meta-tools (+ data utilities like
  `resolve_date`).
- **Session id comes from the authenticated identity**, never the model. Lock
  `session_check_duplicate` / `session_log_write` signatures before the orchestrator wires in.
- **Identity flows server-side**, never from the LLM. Don't add workspace/role/user to
  any agent-facing tool signature.
- **Twenty naming:** `person`/`people` (not "contact"); `note` + `task` (no
  "Activity"/"Comment"). See plan §1.

---

## 6. One-line summary

- **Writer (built):** scope-restricted, tier-gated write worker; no read access.
- **Person 1 (team):** lock the bridge (auth + identity + two roles + deny-list), mask
  PII at the `forward()` boundary, and build the real tier catalog + conflict checker.
- **Person 2 (team):** session endpoints + store, composite read tools, and the
  `resolve_date` / `get_pipeline_stages` utilities.
- **Reader (Majid) & Orchestrator (agent owners):** built separately — they consume the
  team's tools/endpoints; the team does not build agents.
