# Agent Bridge — Handoff & Next Steps

This explains the bridge that now exists, and the work still to do on top of it,
**split between two people**. Read it with
[CRM_AGENT_TOOL_MAPPING_AND_PLAN.md](../twenty-server/docs/CRM_AGENT_TOOL_MAPPING_AND_PLAN.md)
open — this is the "what's built + who does what next" companion to that plan.

> TL;DR — the plumbing (Node bridge + Python tool layer an LLM can actually call)
> is **done and verified**. What's left divides cleanly in two:
> **Person 1** owns security, tiers, and masking. **Person 2** owns composite
> workflows, session/cache, and the remaining new tools. Find your name in §3/§4.

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
`execute` auto-resolves `userWorkspaceId` from `userId + workspaceId` (DATABASE_CRUD
tools require a user context for actor attribution / row-level perms).

**Security posture is intentional:** the bridge trusts whatever `workspaceId` /
`roleId` / `userId` it is given. That is the whole reason the security layer must
live in Python (Person 1, §3).

### Python side — what an LLM calls
`packages/twenty-ai-service/`

| File | What it is |
| :--- | :--- |
| `bridge_client.py` | one async `forward(path, payload)` to the Node bridge (URL, timeout, error envelope) |
| `routers/bridge.py` | FastAPI proxy `/bridge/*` (snake_case → camelCase) — handy for curl/debugging |
| `agent/crm_tools.py` | **the LLM-facing layer**: `get_crm_tools()` returns LangChain tools |

`agent/crm_tools.py` follows Twenty's own **progressive-disclosure** pattern: an
LLM is never handed 254 tools. It gets four meta-tools and discovers the rest at
runtime:

```
get_tool_catalog(categories?)   # browse
learn_tools(tool_names)         # fetch schemas
execute_tool(tool, tool_args)   # run any tool by name
get_current_user()              # "act as me"
```

Identity (`TWENTY_WORKSPACE_ID` / `TWENTY_ROLE_ID` / `TWENTY_USER_ID`) is injected
server-side in `_identity()` and **never exposed to the model**. This is a
placeholder for real session identity (Person 1, §3).

### Verified
- All **254** tools are discoverable via `catalog` and have generatable schemas via `learn`.
- Reads (31 `find_*`/`get_*`/`list_*`) and a create→delete round-trip work.
- A real OpenAI (`gpt-4o-mini`) agent drove the full loop: `get_tool_catalog → learn_tools → execute_tool → grounded answer`.

> Note: this is the **meta-tool** approach. The plan doc (§2) also lists ~33 named
> semantic wrappers (`search_contacts`, `get_company`, …). Those are *not* built —
> and for most the meta-tools already cover the need. Where named tools still add
> value is the **composite workflows** Person 2 builds (§4).

---

## 2. How to run & test

```bash
# 1. Node (Twenty server) must be up with Postgres + Redis:
bash packages/twenty-utils/setup-dev-env.sh
npx nx start twenty-server                      # serves /agent-bridge on :3000

# 2. Python service:
cd packages/twenty-ai-service
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export TWENTY_WORKSPACE_ID=... TWENTY_ROLE_ID=... TWENTY_USER_ID=...
.venv/bin/uvicorn main:app --reload --port 8000
```

Get test IDs from the DB:
```sql
SELECT id FROM core.workspace WHERE "activationStatus"='ACTIVE';
SELECT id, label FROM core.role WHERE "workspaceId"='<WS>';   -- use Admin for dev
SELECT u.id FROM core."user" u
  JOIN core."userWorkspace" uw ON uw."userId"=u.id WHERE uw."workspaceId"='<WS>';
```

Smoke-test the tools directly:
```python
from agent import get_crm_tools
tools = {t.name: t for t in get_crm_tools()}
await tools["execute_tool"].ainvoke({"tool": "find_people", "tool_args": {"limit": 3}})
```

---

## 3. 👤 PERSON 1 — Security, Tiers & Masking

You own the **guardrails**: nothing reaches the bridge unauthenticated, no raw PII
reaches the model, and every write is tiered and gated. Three parts.

### A. Security (highest priority)
The bridge is open by design; nothing in front of it is. Today anyone who can reach
`:3000/agent-bridge` (or the Python `/bridge/*` proxy) can run any tool in any
workspace by supplying IDs. Close that, in rough priority:

1. **Network boundary.** The Node bridge must bind to localhost / the internal
   network only — never publicly routable. Treat it like an internal DB port.
2. **Authenticate the Python entrypoint.** `routers/bridge.py` and whatever HTTP
   surface the agent is reached through must require auth (API key / session /
   service token). Right now they're open.
3. **Identity must not come from the model — or from the caller as free input.**
   `_identity()` reads env vars as a stand-in. Replace it: derive
   `workspaceId` / `roleId` / `userId` from the **authenticated session**, server
   side. The LLM only ever sees tool names + args.
4. **Least-privilege agent role.** Configure the Twenty role the bridge runs under
   with only the object permissions the agent needs. (Twenty filters tools by role —
   `catalog`/`learn`/`execute` all respect it.)
5. **Tool allow/deny list.** Mirror Twenty's `MCP_EXCLUDED_TOOL_NAMES`
   (`code_interpreter`, `http_request`) and reject anything not on the agent's
   allow-list inside `execute_tool` before forwarding.

**Done when:** an unauthenticated caller can't execute a tool, the model can't
choose its own workspace/role/user, and the agent runs under a least-privilege
role with a tool allow-list.

### B. Masking / unmasking (agent ↔ tool ↔ agent)
PII must never sit in the LLM context in the clear:

```
agent → (tokens) → UNMASK → bridge → Twenty
Twenty → bridge → MASK → (tokens) → agent
```

- **Outbound (tool result → agent):** detect PII in the bridge response, replace
  each value with a **stable token** (`{{person_1}}`, `{{email_3}}`), store
  `token → real value` in a **per-session vault**.
- **Inbound (agent args → tool):** before forwarding `execute_tool` args, swap any
  tokens back to real values from the vault.
- **Detector:** GLiNER NER already exists at `routers/ner.py` (`/ner/extract`) —
  use it for free-text fields; mask known structured fields (emails/phones/names
  from the tool schema) deterministically.
- **Where it lives:** a `masking.py` module that wraps the `forward()` calls used
  by Person 2's tools, so **no tool can bypass it**. Prove it on one tool, then
  make it the wrapper everything inherits.
- **Stable tokens** matter: the same person → same token within a session, so the
  agent can reason ("link {{person_1}} to {{company_2}}") and unmask round-trips.

**Done when:** no raw PII reaches the model, tokens round-trip through a write, and
masking is a wrapper every tool inherits (not per-tool code).

### C. Tiers & write gating
Every write is classified and gated before it executes. Build the catalog first —
the gate tools are lookups against it.

| Piece | Type | Behavior |
| :--- | :--- | :--- |
| **Action catalog** | static table | every write action → its tier, required fields, the tool it maps to, min-info workflow. **Build first.** |
| `lookup_action_tier` | deterministic + catalog | return an action's tier (1/2/3) + required fields. Ambiguous entity → bump tier. Unknown action → fail safe. |
| `check_conflicts` | deterministic (rule-based) | flag order-of-magnitude value changes, **stage regression**, past dates. 15+ tests. *(needs stage order from Person 2's `get_pipeline_stages`.)* |

The **write protocol** these enforce (also uses Person 2's session tools):

```
lookup_action_tier → session_check_duplicate → (Tier 2+) check_conflicts
  → execute  (Tier 1/2)   |   draft + wait for confirmation token → execute  (Tier 3)
  → session_log_write
```

Tier 1 = execute + compact confirm · Tier 2 = execute + diff · Tier 3 = draft,
confirm, then call the gated tool with a confirmation token (e.g.
`advance_deal_stage` to a terminal stage, bulk `delete_*`).

**Done when:** every write action resolves to a tier, conflicts are caught (with
tests), and Tier 1/2/3 + the confirmation-token path work end-to-end.

---

## 4. 👤 PERSON 2 — Workflows, Session & New Tools

You own the **capabilities**: the multi-step tools that make the agent efficient,
the session memory that makes it coherent, and the remaining utility tools. All of
your tools are added to `get_crm_tools()` in the same `StructuredTool` shape and
automatically inherit Person 1's masking wrapper.

### A. Composite workflow tools (chain >1 tool per call)
Right now the agent orchestrates every small read itself. Give it **single tools
that fan out multiple bridge calls and merge**, so one call answers a whole
question. Build **two** to start (both COMPOSITE in the plan, §2):

1. **`get_company_overview(company_id_or_name)`** — company + its people + its
   opportunities in one call:
   - `find_one_company` (or `find_companies {name ilike}` to resolve first)
   - `find_people { company.id: { eq } }`
   - `find_opportunities { company.id: { eq } }`
   - merge into one object, return.
2. **`get_entity_timeline(entity_id)`** (or `search_all_records(query)`) — unified
   activity/search across objects:
   - `find_notes` + `find_tasks` for the entity, merged + sorted by date — *or*
     `find_people` + `find_companies` + `find_opportunities` merged when the type
     is unknown.

Pattern: issue N `forward("execute", …)` calls (`asyncio.gather`), merge in Python,
return. Keep agent-facing args minimal (an id or a query) and resolve the rest
internally.

**Done when:** the agent answers "tell me everything about company X" or "show the
timeline for Y" in a single tool call instead of a multi-step loop.

### B. Session + cache tools (no Twenty backing)
Back these with a state store (in-memory for dev, Redis / a table for real); expose
them as tools shaped like the rest so the agent can't tell they're not Twenty-backed.

| Tool | Behavior |
| :--- | :--- |
| `session_set_topic` / `session_get_topic` | track the current conversation topic for the orchestrator |
| `session_log_write` | append `{tool, args, old_value, new_value, ts}` to a per-session write log |
| `session_get_write_log` | read it back (powers **corrections** — `old_value` makes revert possible) |
| `session_check_duplicate` | compare an intended write against the log to catch repeats |
| `cache_update_lead` / `cache_get_lead_history` | per-lead recency cache, capped at 5, with staleness detection |

Session scoping is the key design choice: everything is keyed by a session id —
it should ride with the authenticated identity (coordinate with Person 1), **not**
come from the model. `session_check_duplicate` + `session_log_write` are the hooks
Person 1's write protocol calls.

**Done when:** the agent can set/recall topic, writes are logged with old values,
duplicates are detectable, and corrections can revert via the log.

### C. Utility tools
| Tool | Type | Behavior |
| :--- | :--- | :--- |
| `resolve_date` | deterministic | relative → absolute ISO (`"next tuesday"` → date). 40+ cases. No DB. |
| `get_pipeline_stages` | METADATA | read Twenty's opportunity `stage` options via `execute_tool("get_field_metadata", …)`. No records, **no masking**. Person 1's `check_conflicts` consumes the stage order from here. |

**Done when:** dates resolve to ISO with tests, and `get_pipeline_stages` returns
ordered stage options the conflict checker can use.

---

## 5. The seams between you (don't surprise each other)

- **Masking wraps Person 2's tools.** Person 1 builds the wrapper around
  `forward()`; Person 2's composite/session tools must call through it, not raw
  `forward()`. Agree on the `masking.py` interface early.
- **`get_pipeline_stages` (P2) feeds `check_conflicts` (P1).** Agree on the shape
  of the returned stage list (ordered, with internal values) up front.
- **Session tools (P2) are called by the write protocol (P1).** Lock
  `session_check_duplicate` and `session_log_write` signatures before wiring.
- **Session id comes from the authenticated identity (P1), used by P2's store.**

---

## 6. Contracts you must not break

- **Bridge envelope:** always `{ ok, data }` / `{ ok, error: { code, message } }`.
- **`execute_tool` arg is `tool_args`, not `args`** — `args` collides with
  `BaseTool.args` and LangChain mangles it to `v__args`.
- **Twenty naming:** `person`/`people` (not "contact"); no "Activity"/"Comment"
  objects — use `note` + `task`. See plan §1.
- **Identity flows server-side**, never from the LLM. Don't add workspace/role/user
  to any agent-facing tool signature.
- **`get_current_user` already ships** as a meta-tool — nobody rebuilds it.

---

## 7. Build order

```
Both parallel, on dev identity (env vars):
  PERSON 1: (A) network boundary + auth + identity  →  (B) masking wrapper on one tool
            →  (C) action catalog → lookup_action_tier → check_conflicts → write protocol
  PERSON 2: (A) get_company_overview  →  (B) session + cache store
            →  (C) resolve_date + get_pipeline_stages

Wire together:
  P2 tools route through P1's masking wrapper
  P1 write protocol calls P2's session_check_duplicate / session_log_write
  P1 check_conflicts consumes P2's get_pipeline_stages
  First real end-to-end: agent → masked tools → bridge → Twenty → masked back. Debug together.
```

**Don't ship** without Person 1's security (§3A) and masking (§3B). Person 2's
work can run end-to-end on dev identity before that, but not in front of real users.

---

## 8. One-line summary per person

- **Person 1:** the guardrails — lock the bridge down (auth + identity + role +
  allow-list), mask/unmask all PII at the boundary, and tier + gate every write.
- **Person 2:** the capabilities — composite workflow tools so the agent doesn't
  micromanage, session/cache memory for coherence + corrections, and the
  `resolve_date` / `get_pipeline_stages` utilities.
