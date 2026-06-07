# CRM Agent — Tool Mapping & Build Plan

This document does two things:

1. **Maps** every tool in our CRM Agent registry (the CSV) to the real tools
   Twenty already exposes (documented in [AGENT_TOOLS_REFERENCE.md](AGENT_TOOLS_REFERENCE.md)).
2. **Tells each of the 4 people exactly what to build** under the architecture we
   just locked in.

Read the "Architecture" section first — it changed. Then find your name.

---

## 0. The architecture (what changed)

We are **not** writing CRM tools from scratch. Twenty already generates a full
CRUD tool set per object (`find_people`, `update_opportunity`, `create_company`,
…). Our job is to put a **thin, semantic, masked layer in Python** on top of those
tools, plus build the handful of tools Twenty does *not* have (session, cache,
safety).

```
┌──────────────────────────────────────────────────────────┐
│  PYTHON AGENTS                                            │
│  Orchestrator → Reader / Writer  (Person 2 + You)        │
└───────────────┬──────────────────────────────────────────┘
                │  calls semantic tools (Python functions)
                ▼
┌──────────────────────────────────────────────────────────┐
│  PYTHON SEMANTIC TOOL LAYER  (Person 4)                  │
│  search_contacts(), get_company(), advance_deal_stage()… │
│  + MASKING applied here on every in/out value            │
└──────┬───────────────────────────────────┬───────────────┘
       │ Twenty-backed tools                │ new tools
       │ (Is MCP Tool = Yes)                │ (Is MCP Tool = No)
       ▼                                    ▼
┌─────────────────────────┐      ┌──────────────────────────┐
│ NODE BRIDGE  (You)      │      │ DIRECT DB / STATE         │
│ HTTP endpoint:          │      │ (Person 3)                │
│  { tool, args } → runs  │      │ session, cache, tiers,    │
│  Twenty's execute_tool  │      │ conflicts, dates          │
└──────────┬──────────────┘      └──────────────────────────┘
           ▼
┌─────────────────────────┐
│ TWENTY tool-provider     │
│ find_*/create_*/update_* │
└─────────────────────────┘
```

**Two kinds of tools, two code paths:**

| Path | Which tools | How it's built |
| :--- | :--- | :--- |
| **Twenty-backed** (`Is MCP Tool = Yes`) | Everything that reads/writes CRM records | Python wrapper → **Node bridge** → Twenty's `execute_tool` |
| **New / no backing** (`Is MCP Tool = No`) | session_\*, cache_\*, resolve_date, check_conflicts, lookup_action_tier | Python tool → **direct DB / in-process state**, never touches Twenty mcp |

> The new tools are still *shaped* like Twenty tools (typed name + args + JSON
> result) so the agents can't tell the difference.

---

## 1. The Python ↔ Node bridge contract (You own this)

This is the seam between the Python tool layer and Twenty. Everyone codes against
it, so it's defined here once.

**Request (Python → Node):**
```json
{
  "tool": "update_opportunity",
  "args": { "id": "uuid", "stage": "WON", "amount": { "amountMicros": 350000000000 } },
  "roleId": "the-agent-role-id",
  "workspaceId": "..."
}
```

**Response (Node → Python):**
```json
{ "ok": true, "data": { ...record... } }
// or
{ "ok": false, "error": { "code": "NOT_FOUND", "message": "..." } }
```

Inside Node, `tool` + `args` go straight into Twenty's existing
`ToolRegistryService.resolveAndExecute(name, args, ctx)` (see
[AGENT_TOOLS_REFERENCE.md](AGENT_TOOLS_REFERENCE.md) §"Steps 2 & 3"). You are not
writing CRM logic — you are exposing Twenty's executor over HTTP and enforcing the
agent's role/permissions on every call.

**Naming note that bites everyone:** Twenty says **`person`/`people`**, not
"contact". So `search_contacts` → `find_people`, `get_contact` → `find_one_person`.
Twenty has **no** "Activity" or "Comment" object — activities are `note` + `task`,
and comments are `note`.

---

## 2. Full tool mapping (the CSV → Twenty)

**Mapping types:**
- **DIRECT** — 1:1 with a single Twenty CRUD tool (just rename + mask)
- **FILTER** — a Twenty CRUD tool called with a specific filter/aggregate
- **COMPOSITE** — several Twenty calls fanned out and merged in Python
- **METADATA** — reads Twenty *metadata*, not records
- **NEW-DB** — no Twenty backing; build it (direct DB / state store)
- **DETERMINISTIC** — no DB at all; pure Python logic

Twenty filter operators available on `find_*`: `eq, neq, gt, gte, lt, lte, in,
is, like, ilike, startsWith, containsAny`. Find-by-id is
`filter: { id: { eq: "..." } }`.

### Company
| CSV tool | Type | Twenty tool(s) | Notes |
| :--- | :--- | :--- | :--- |
| `create_company` | DIRECT | `create_company` | mask `name` in/out |
| `get_company` | COMPOSITE | `find_one_company` + `find_people{company.id eq}` + `find_opportunities{company.id eq}` | "full info incl. contacts & opportunities" |
| `update_company` | DIRECT | `update_company` | resolve id via `search_companies` first |
| `search_companies` | FILTER | `find_companies` | `{ name: { ilike } }` or `{ domainName: { ilike } }` |

### Contact (= Twenty `person`)
| CSV tool | Type | Twenty tool(s) | Notes |
| :--- | :--- | :--- | :--- |
| `get_contact` | DIRECT | `find_one_person` | mask name/email/phone |
| `create_contact` | DIRECT | `create_person` | |
| `update_contact` | DIRECT | `update_person` | |
| `search_contacts` | FILTER | `find_people` | `{ name: { ilike } }` / `{ emails… }` |

### Opportunity (= "deal")
| CSV tool | Type | Twenty tool(s) | Notes |
| :--- | :--- | :--- | :--- |
| `get_opportunity` | DIRECT | `find_one_opportunity` | |
| `create_opportunity` | DIRECT | `create_opportunity` | default stage = first pipeline stage |
| `update_opportunity` | DIRECT | `update_opportunity` | partial update |
| `search_opportunities` | FILTER | `find_opportunities` | filter by stage/amount/company/contact |
| `list_opportunities_by_stage` | FILTER | `group_by_opportunities` | group by `stage`, COUNT + SUM(amount) |
| `advance_deal_stage` | FILTER + GATE | `update_opportunity` (stage field) | **terminal stages require confirmation token** |
| `get_pipeline_stages` | METADATA | `get_field_metadata` (opportunity `stage` options) | reads metadata, no records, no masking |

### Activity (no Activity object → `note` + `task`)
| CSV tool | Type | Twenty tool(s) | Notes |
| :--- | :--- | :--- | :--- |
| `create_note` | DIRECT | `create_note` | most common Tier-1 write |
| `create_comment` | DIRECT (alias) | `create_note` | no comment object in Twenty |
| `create_task` | DIRECT | `create_task` | optionally linked via task target |
| `get_tasks` | FILTER | `find_tasks` | by status/assignee/completion |
| `get_entity_activities` | COMPOSITE | `find_notes` + `find_tasks` (by note/task target) | merge + sort by date |
| `filter_activities` | COMPOSITE | `find_notes` + `find_tasks` (filtered) | by type/entity/date range |
| `get_activities` | COMPOSITE | `find_notes` + `find_tasks` | unified timeline |

### Relationship & Resolution
| CSV tool | Type | Twenty tool(s) | Notes |
| :--- | :--- | :--- | :--- |
| `get_company_contacts` | FILTER | `find_people` `{ company.id: { eq } }` | resolve companyId first |
| `get_person_opportunities` | FILTER | `find_opportunities` `{ pointOfContact.id: { eq } }` | requires personId |
| `get_contact_company` | FILTER | `find_one_person` (select company relation) | lightweight resolution lookup |
| `link_opportunity_to_company` | FILTER | `update_opportunity` (set company / pointOfContact) | resolve all ids first |
| `transfer_contact_to_company` | FILTER | `update_person` (change company relation) | `preserveHistory` default true |
| `get_related_entities` | COMPOSITE | nested relation reads via `find_one_*` + `find_*` | core resolution tool |
| `get_relationship_summary` | COMPOSITE | `group_by_*` / counts across relations | counts of contacts/deals/activities |
| `find_orphaned_records` | COMPOSITE | `find_*` with null-relation filters (`{ company.id: { is: NULL } }`) | data hygiene |
| `fuzzy_search` | FILTER | `find_*` with `ilike` | zero-candidate fallback |
| `search_all_records` | COMPOSITE | `find_people` + `find_companies` + `find_opportunities`, merged | type unknown |
| `get_current_user` | SPECIAL | Twenty `currentWorkspaceMember` (GraphQL, not CRUD) | **needs a dedicated Node bridge endpoint** — flag to You |

### New tools — no Twenty backing (`Is MCP Tool = No`)
| CSV tool | Type | Build as |
| :--- | :--- | :--- |
| `session_get_topic` | NEW-DB | session state store |
| `session_set_topic` | NEW-DB | session state store |
| `session_log_write` | NEW-DB | write log table/store |
| `session_get_write_log` | NEW-DB | read write log |
| `session_check_duplicate` | NEW-DB | read write log + compare |
| `cache_update_lead` | NEW-DB | per-lead recency cache (cap 5) |
| `cache_get_lead_history` | NEW-DB | read recency cache + staleness check |
| `resolve_date` | DETERMINISTIC | pure date logic |
| `check_conflicts` | DETERMINISTIC | rule-based (needs `get_pipeline_stages` data) |
| `lookup_action_tier` | DETERMINISTIC | static catalog lookup + escalation |

**Tally:** 33 tools wrap Twenty (Person 1) · 10 are new (Person 2) · plus
`get_current_user` needs a special bridge endpoint (You + Person 1).

---

## 3. What each person builds

### 👤 YOU — Node Bridge + Writer + Stubs

You are the seam between Python and Twenty, **and** the Writer agent.

**A. Node bridge (the connection layer)**
- HTTP endpoint that accepts `{ tool, args, roleId, workspaceId }` and calls
  Twenty's `ToolRegistryService.resolveAndExecute` (see the contract in §1).
- Build/configure the **agent role** with the right object permissions so the
  bridge runs every call under it (Twenty filters tools by role — §"How
  get_tool_catalog assembles" in the reference doc).
- Add the **special `get_current_user` endpoint** (Twenty's
  `currentWorkspaceMember` isn't a CRUD tool).
- Map Twenty errors → the structured `{ ok:false, error }` shape.
- **Done when:** Python can call any Twenty tool by name through one endpoint and
  get masked-ready JSON back.

**B. Writer worker** (Person 4 in the original plan)
- Uses Person 3's Worker framework + Person 1's write wrappers + Person 2's safety
  tools.
- Enforces the write protocol every time: `lookup_action_tier` →
  `session_check_duplicate` → (Tier 2+) `check_conflicts` → execute or draft →
  `session_log_write`.
- Tier 1 = execute + compact confirm · Tier 2 = execute + diff · Tier 3 = draft,
  wait for confirmation, then call the gated tool with a confirmation token.
- Correction handler: `session_get_write_log` → revert old value → apply new → log
  both.
- **Done when:** Tier 1/2/3 + corrections work end-to-end through the bridge.

**C. Follow-up & Researcher stubs** — Worker-shaped stubs with documented payload
contracts.

---

### 👤 PERSON 1 — The Tool Builder (Python semantic layer + masking)

You own **all 33 Twenty-backed wrapped tools** and the masking layer.

1. **Masking layer first.** Every value in args and every value in results passes
   through masking. Build it as a wrapper so it's applied uniformly, then prove it
   on one tool.
2. **DIRECT tools** (the easy ~15): rename + map fields + mask. Each is one bridge
   call. (`create_company`, `get_contact`→`find_one_person`,
   `search_contacts`→`find_people`, etc.)
3. **FILTER tools**: same, but you build the Twenty filter object
   (`{ name: { ilike: "%stc%" } }`, group-by for `list_opportunities_by_stage`,
   etc.).
4. **COMPOSITE tools**: fan out multiple bridge calls and merge in Python
   (`search_all_records`, `get_related_entities`, the `get_*activities` trio,
   `find_orphaned_records`, `get_relationship_summary`, `get_company`).
5. **METADATA / GATE specials**: `get_pipeline_stages` (read field metadata),
   `advance_deal_stage` (reject terminal-stage calls without a confirmation token).
6. **Publish JSON schemas** for every tool to Person 3 (Reader) and You (Writer).
7. **Fill the registry** — complete "Twenty Tool(s)" + "Mapping Type" columns
   (this table is the start).

**Done when:** all 33 wrappers return masked data through the bridge and schemas
are shared. **Start with masking + DIRECT tools so the Reader can come up.**

---

### 👤 PERSON 2 — The Safety Engineer (new DB / deterministic tools)

You own **all 10 tools that do not touch Twenty**. These talk to the DB / state
store directly (or are pure logic), but expose the same typed name+args+JSON shape.

1. **Action catalog** — every write action, its tier, required fields, the
   semantic tool it maps to, and the minimum-info workflow.
2. `lookup_action_tier` — catalog lookup + escalation (ambiguous entity → bump
   tier). Unknown action → fail safe.
3. `check_conflicts` — order-of-magnitude value changes, stage regression (needs
   `get_pipeline_stages` data from Person 1), past dates. 15+ tests.
4. `resolve_date` — relative → absolute ISO. 40+ cases.
5. **Session state** (`session_get_topic`, `session_set_topic`, `session_log_write`,
   `session_get_write_log`, `session_check_duplicate`) — backed by a session
   store. The write log's `old_value` is what makes corrections possible.
6. **Recency cache** (`cache_get_lead_history`, `cache_update_lead`) — per-lead,
   capped at 5, with staleness detection.

**Done when:** all 10 work with typed schemas + unit tests, and the Writer can
call them.

---

### 👤 PERSON 3 — The Orchestrator + Reader (agents)

You build the brains. No CRM logic — you consume Person 1's and Person 2's tools.

1. **Worker framework** — base class that runs an LLM tool-calling loop:
   takes (tools, system_prompt), receives a structured request, returns a
   structured result. **You build this generically — You reuse it for the Writer.**
2. **Reader worker** — Person 1's read wrappers + a Reader system prompt. Owns
   entity resolution (single / multiple / zero candidates) and query synthesis.
   Returns structured data, not prose.
3. **Orchestrator** — receives masked rep input, reasons about intent, routes to
   Reader / Writer / stubs, tracks session topic (via Person 2's `session_*_topic`),
   composes the final response. Never calls Twenty or decides tiers itself.
4. **Query path** — 10+ query scenarios producing grounded answers.

**Done when:** orchestrator routes queries → Reader, writes → Writer, and handles
multi-step flows (resolve via Reader → write via Writer). Mock tools first, then
real ones.

---

## 4. Build order (so nobody blocks)

```
Day 1 — all parallel, all on mocks:
  YOU:      Node bridge skeleton (one tool round-trips) + role setup
  Person 1: masking layer + first DIRECT tool
  Person 2: action catalog + lookup_action_tier + resolve_date
  Person 3: Worker framework + Reader (mock tools) + orchestrator (mock workers)

Critical path:
  P3 Worker framework + P1 DIRECT tools
    → Reader works
    → orchestrator → Reader flow
    → (YOU) orchestrator → Writer flow
    → end-to-end through the Node bridge

Wiring (Phase 2):
  P1 + YOU:  real read tools into Reader / write tools into Writer via the bridge
  P2 + YOU:  safety tools into Writer
  First real end-to-end: orchestrator → Reader → bridge → Twenty → back. Debug together.
```

---

## 5. One-line summary per person

- **You:** the Node bridge that turns `{tool,args}` into a real Twenty call, plus
  the Writer agent and stubs.
- **Person 1:** 33 Python wrappers over Twenty's CRUD + the masking layer.
- **Person 2:** the 10 tools Twenty doesn't have — session, cache, tiers,
  conflicts, dates — straight to the DB.
- **Person 3:** the Worker framework, the Reader, and the orchestrator that routes
  between them.
