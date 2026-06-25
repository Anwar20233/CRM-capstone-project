# Notion Build Brief — "CRM Agent: Tool Build Plan"

> **For Notion (the builder).** This file is a set of instructions. Build a Notion page and a
> database exactly as described below. Each `Instruction N` block tells you what block/section
> or database to create and the content to put in it. Build it verbatim; don't invent extra
> sections. The audience is two engineers — **Person 1** and **Person 2** — who build the CRM
> agent's tool layer.
>
> This replaces the old Notion table *"CRM Agent — Tool Registry"*, which is misaligned with
> the current architecture. **Instruction 7** says exactly what to delete/edit there.

---

## Instruction 1 — Create the parent page

- **Title:** `CRM Agent — Tool Build Plan`
- **Top callout (ℹ️):**
  > You build **tools and endpoints, not agents.** The **Writer agent** is already built. The
  > **Reader agent** is owned by *Majid*; the **Orchestrator** is owned by the agent owners —
  > neither is in this plan. The **masking / unmasking layer is owned separately (not a team
  > task)** — ignore PII masking entirely.

---

## Instruction 2 — Add section "1. What already exists — DO NOT REBUILD"

Add this text, then the table.

**Plumbing (done + verified):** the Node bridge (`/agent-bridge/{execute,catalog,learn,current-user}`,
envelope `{ ok, data } | { ok, error:{code,message} }`), the Python meta-tools
(`get_tool_catalog → learn_tools → execute_tool → get_current_user`), and the **Writer agent**
(scope-restricted write+meta, tier-gated writes with single-use confirmation tokens).

**The 254 CRM tools already exist and are discoverable** via `get_tool_catalog`. Every basic
CRUD operation is callable today with `execute_tool("<name>", {...})`:

| Category | Count | Contents |
| :--- | :--- | :--- |
| `DATABASE_CRUD` | **192** | per object: `find_*` (48), `find_one_*`, `group_by_*` (24), `create_*`/`create_many_*` (48), `update_*`/`update_many_*` (48), `delete_*`/`delete_many_*` (24) across ~24 objects |
| `VIEW` | 15 | views, filters, sorts CRUD |
| `WORKFLOW` | 15 | workflow steps & triggers |
| `METADATA` | 13 | object/field metadata CRUD |
| `DASHBOARD` | 7 | dashboards, tabs, widgets |
| `ACTION` | 6 | `http_request`, `send_email`, `draft_email`, `search_help_center`, `navigate_app`, `code_interpreter` |
| `VIEW_FIELD` | 6 | view field CRUD |
| **Total** | **254** | |

**Add a callout (⚠️):**
> `create_person`, `update_person`, `delete_person`, `create_company`, `create_opportunity`,
> `create_note`, `create_task`, `create_many_people`, `update_many_*`, `delete_*`, all `find_*`
> / `group_by_*` — **already implemented.** They are part of the 254. Twenty naming: **person /
> people**, **note**, **task** (no "contact", no "comment"). The only work around them is
> **tier classification** (Person 1).

---

## Instruction 3 — Add a callout "🔑 The Golden Rule: one unified discovery"

Add this as a highlighted callout, then the numbered list.

> Every new **LLM-facing** tool (composite reads, `resolve_date`, `get_pipeline_stages`) must be
> discoverable through the **same** `get_tool_catalog → learn_tools → execute_tool` flow as the
> 254 Twenty tools. **No separate, parallel tool list.** One catalog, one learn, one execute.

**How to hook a new tool in (recommended):**
1. Add it to a Python **local tool registry** (`agent/tool_registry.py`) as a `LocalTool`
   descriptor: `{ name, category, capability(read|write), description, input_schema, handler }`.
2. `get_tool_catalog` **merges** the registry into the bridge catalog under each tool's
   `category`, then applies the existing scope filter.
3. `learn_tools` returns the local `input_schema` if the name is local, else forwards.
4. `execute_tool` runs the local `handler` if the name is local (still scope-guarded +
   write-gated), else forwards to the bridge.
5. Register each tool's `capability` in `tool_scope.py` so it routes to the right worker
   automatically.

Organize new tools into categories: `COMPOSITE` (fan-out reads) and `UTILITY`
(`resolve_date`, `get_pipeline_stages`). Adding a tool = one registry entry; nothing else
changes.

**Not discoverable (by design):** tier / conflict / session / cache helpers are **middleware
and endpoints, not LLM tools** — never in the catalog. The Writer's `WritePolicy` and the
Orchestrator call them directly.

---

## Instruction 4 — Create a database "New Tools to Build"

Create a Notion **database** (table view) with these **properties**:

| Property | Type | Options |
| :--- | :--- | :--- |
| Name | Title | — |
| Owner | Select | `Person 1`, `Person 2` |
| Category | Select | `COMPOSITE`, `UTILITY`, `Middleware`, `Endpoint` |
| LLM-facing? | Select | `Yes (in catalog)`, `No` |
| Tier | Select | `1`, `2`, `3`, `n/a` |
| Status | Select | `Planned`, `In progress`, `Done` |
| Built from / Notes | Text | — |

Seed it with these rows:

| Name | Owner | Category | LLM-facing? | Tier | Built from / Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `get_company_overview` | Person 2 | COMPOSITE | Yes (in catalog) | n/a | `find_one_company` + `find_people{company.id}` + `find_opportunities{company.id}`, merged |
| `get_entity_timeline` | Person 2 | COMPOSITE | Yes (in catalog) | n/a | `find_notes` + `find_tasks` for the entity, merged & sorted by date |
| `search_all_records` | Person 2 | COMPOSITE | Yes (in catalog) | n/a | `find_people` + `find_companies` + `find_opportunities`, merged (type unknown) |
| `get_related_entities` | Person 2 | COMPOSITE | Yes (in catalog) | n/a | relation reads merged; resolution helper |
| `resolve_date` | Person 2 | UTILITY | Yes (in catalog) | n/a | relative → absolute ISO; deterministic, no DB; 40+ cases (stub exists) |
| `get_pipeline_stages` | Person 2 | UTILITY | Yes (in catalog) | n/a | ordered `stage` options via `get_field_metadata`; feeds `check_conflicts` |
| Action tier catalog + `lookup_action_tier` | Person 1 | Middleware | No | n/a | replace stub `_ACTION_TIER_MAP`; returns `{tier, required_fields, escalated}` |
| `check_conflicts` | Person 1 | Middleware | No | n/a | rule-based: order-of-magnitude, stage regression, past dates; 15+ tests |
| `session_set_topic` / `session_get_topic` | Person 2 | Endpoint | No | n/a | conversation topic store, keyed by session id |
| `session_log_write` / `session_get_write_log` | Person 2 | Endpoint | No | n/a | append `{tool,args,old_value,new_value,ts}`; powers corrections |
| `session_check_duplicate` | Person 2 | Endpoint | No | n/a | compare intended write vs the log |
| `cache_update_lead` / `cache_get_lead_history` | Person 2 | Endpoint | No | n/a | per-lead recency cache, capped at 5 |

---

## Instruction 5 — Add section "Person 1 — Security & Tiers"

Add as a checklist (to-do blocks).

**A. Security (highest priority)**
- [ ] Bind the Node bridge to localhost / internal network only (never public).
- [ ] Authenticate the Python entrypoint (`routers/bridge.py` + the agent HTTP surface).
- [ ] Derive `workspaceId` / `roleId` / `userId` from the **authenticated session**, server-side
      (replace the env-var placeholder in `_identity()`). The LLM never sees identity.
- [ ] Two least-privilege Twenty roles: read-only → `TWENTY_READER_ROLE_ID`, read+write →
      `TWENTY_WRITER_ROLE_ID` (today both point at Admin in dev — split them).
- [ ] Maintain the deny/override config in `agent/tool_scope.py` (`DENIED_TOOLS`,
      `_CAPABILITY_OVERRIDES`): deny `code_interpreter`/`http_request`; `send_email`/`draft_email`
      → write; `search_help_center` → read; review `navigate_app`.

**B. Tier catalog + conflict checker (middleware, NOT LLM tools)**
- [ ] Replace the stub `_ACTION_TIER_MAP` + `_lookup_action_tier` (`agent/stubs/safety_tools.py`)
      with the real action catalog: every write action → tier (1/2/3), required fields,
      ambiguity escalation. Keep the `{ ok, data:{ tier, required_fields, escalated } }` envelope.
- [ ] Build `check_conflicts(action, args)` — consumes `get_pipeline_stages` (Person 2). 15+ tests.

**Tier baseline (encode in the catalog):**

| Tier | Behavior | Examples |
| :--- | :--- | :--- |
| 1 | execute immediately | `create_note`, `create_task`, `create_person` |
| 2 | execute (Orchestrator may diff) | `create_opportunity`, `update_opportunity`, `update_company`, `create_many_people` |
| 3 | draft → confirmation token | `delete_*`, `advance_deal_stage` (terminal), `update_many_*` (bulk) |

---

## Instruction 6 — Add section "Person 2 — Composite reads, Utilities & Session endpoints"

Add as a checklist.

**A. Composite read tools** (LLM-facing → register per the Golden Rule; category `COMPOSITE`,
capability `read`; one call fans out several bridge reads via `asyncio.gather` and merges):
- [ ] `get_company_overview`, `get_entity_timeline`, `search_all_records`, `get_related_entities`
      (see the database rows for what each is built from).
- [ ] Skip thin 1:1 renames (`search_contacts`, `get_contact`). The agent uses the real Twenty
      tools directly. Only build a composite when it saves multiple round-trips.

**B. Utilities** (LLM-facing → register; category `UTILITY`):
- [ ] `resolve_date(text, reference_date?)` — make the stub real, 40+ cases.
- [ ] `get_pipeline_stages()` — ordered stage options via `get_field_metadata`.

**C. Session + cache endpoints** (NOT LLM tools — for the Orchestrator; state store, in-memory
for dev → Redis/Postgres later; session id from the authenticated identity, never the model):
- [ ] `session_set_topic` / `session_get_topic`
- [ ] `session_log_write` / `session_get_write_log`
- [ ] `session_check_duplicate`
- [ ] `cache_update_lead` / `cache_get_lead_history`
- [ ] **Lock `session_check_duplicate` / `session_log_write` signatures** before the Orchestrator wires in.

---

## Instruction 7 — Add section "Clean up the old 'Tool Registry' table"

Render this as a table.

| In the old table | Action |
| :--- | :--- |
| `create_contact`, `get_contact`, `search_contacts`, `update_contact` | **Rename** to `create_person` / `find_one_person` / `find_people` / `update_person`, and mark **Implemented** (no "contact" object). |
| `create_comment` | **Delete** — no Comment object; use `create_note`. |
| All `delete_*`, `*_many_*`, basic CRUD / `find_*` / `group_by_*` rows | **Mark "Implemented"** (part of the 254); only add a **Tier** value. |
| "Masking" column | **Delete it.** Masking/unmasking is owned separately — not a team task. |
| Owners "Person 3" / "Person 4" | **Reassign** to **Person 1** / **Person 2** only. Writes are the Writer agent (built), not a person. |
| `lookup_action_tier`, `check_conflicts` | **Reclassify** as *middleware* (not LLM tools); owner Person 1; not in catalog. |
| `session_*`, `cache_*` | **Reclassify** as *endpoints* (not LLM tools); owner Person 2; not in catalog. |
| `resolve_date` | Keep as the only LLM-facing utility from that group; owner Person 2. |
| `get_current_user` | **Mark "Implemented"** — ships as a meta-tool. |
| "Tier = TBD" | Replace with real tiers (Instruction 5 table). |
| "Is MCP Tool" column | Replace with **"LLM-facing? (in catalog)"** yes/no. |

---

## Instruction 8 — Add section "Definition of done"

- **Person 1:** unauthenticated callers can't execute a tool; identity comes from the session;
  two least-privilege roles; every write action resolves to a tier and conflicts are caught (tests).
- **Person 2:** composite reads answer "tell me everything about company X" in one call and
  appear in `get_tool_catalog`; `resolve_date` + `get_pipeline_stages` discoverable and tested;
  session/cache endpoints exist with locked signatures.
- **Both:** every new LLM-facing tool is reached through `get_tool_catalog → learn_tools →
  execute_tool` — no separate path — and every response uses the `{ ok, data|error }` envelope.
