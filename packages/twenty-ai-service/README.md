# twenty-ai-service

> **The dedicated AI layer** sitting on top of the Twenty CRM platform.  
> One Python process, one port (`8001`), owning everything that is model-related — NER, agentic workflows, and the RAG pipeline.

---

## System Architecture Overview

The system is composed of **three independent servers**, each with a clear, non-overlapping responsibility. The AI service is the new layer we built on top of Twenty's existing infrastructure.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            CLIENT / BROWSER                             │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │  HTTP / WebSocket
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    twenty-front  (React / TypeScript)                   │
│                         Port 3000  —  Vite / Yarn                       │
│                                                                         │
│  • Renders the CRM UI (records, pipelines, chat)                        │
│  • Sends user messages to the backend via GraphQL / REST                │
│  • Displays masked/unmasked entities highlighted in the chat UI         │
└───────────────┬──────────────────────────────────┬──────────────────────┘
                │  GraphQL / REST                  │
                ▼                                  │
┌──────────────────────────────┐                   │
│  twenty-server  (NestJS/TS)  │                   │
│       Port 3000 (API)        │                   │
│                              │                   │
│  • Auth, multi-tenancy       │                   │
│  • CRM data logic            │                   │
│  • PostgreSQL via TypeORM    │                   │
│  • Metadata engine           │                   │
│  • Calls AI service for      │                   │
│    model work (HTTP)         │                   │
└──────────────────────────────┘                   │
        │   HTTP  (AI_SERVICE_URL)                  │
        ▼                                          │
┌─────────────────────────────────────────────────────────────────────────┐
│                  twenty-ai-service  (FastAPI / Python)                  │
│                            Port 8001                                    │
│                                                                         │
│  ┌──────────────┐   ┌─────────────────────┐   ┌─────────────────────┐  │
│  │  NER / NLP   │   │  Agent Workflows    │   │   RAG Pipeline      │  │
│  │  Pipeline    │   │  (planned)          │   │   (planned)         │  │
│  │              │   │                     │   │                     │  │
│  │ GLiNER large │   │ LLM orchestration   │   │ Vector store        │  │
│  │ GLiNER medium│   │ Tool calling        │   │ Embeddings          │  │
│  │ Regex layers │   │ Memory / context    │   │ Retrieval chain     │  │
│  └──────────────┘   └─────────────────────┘   └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  PostgreSQL  ←── owned exclusively by twenty-server, never touched
  (port 5432)      directly by the AI service
```

---

## What Each Server Owns

### `twenty-front` — UI Layer
- Renders all CRM views: contacts, companies, deals, pipelines.
- Hosts the AI chat interface where users interact with the agent.
- Highlights NER-extracted entities in the conversation UI (`UserMessageWithHighlights`).
- Manages the masking session state on the client side (`agentChatMaskingSessionByThreadIdState`).
- **Does not** call the AI service directly — all AI calls are routed through the backend.

### `twenty-server` — CRM & Orchestration Layer
- The authoritative source of all CRM business logic.
- Manages the PostgreSQL database (contacts, companies, deals, activities, notes).
- Owns user auth, workspace isolation, and the metadata engine.
- **Is the only service that calls `twenty-ai-service`** via an internal HTTP client (`AI_SERVICE_URL` env var, default `http://localhost:8001`).
- Exposes a `/api/text-masking/mask` endpoint that the frontend uses — the backend sends the raw text to the AI service, receives entities back, and applies the masking logic before returning to the client.
- Stores masking session data and entity-alias mappings in Postgres (`masking_session`, `entity_mask_alias` tables) for later reversal.

### `twenty-ai-service` — AI Layer *(this service)*
- The **entire AI brain** of the system lives here.
- Owns all model weights, inference, pipeline logic, and (in future phases) agentic workflows and the RAG knowledge base.
- Stateless by design — no direct database connection. All CRM state it needs is passed in the request body by `twenty-server`.
- Scales independently of the CRM (can be moved to a GPU node, a separate container, or swapped for a different model without touching the backend).

---

## Internal Structure of `twenty-ai-service`

```
packages/twenty-ai-service/
├── main.py                    # FastAPI app + lifespan model loader
├── bridge_client.py           # Shared async forward() to the Node bridge
├── requirements.txt           # Pinned Python dependencies
├── requirements-agent.txt     # Minimal agent deps (no torch/gliner)
├── pyproject.toml             # pytest config
├── .env.example               # Environment variable template
├── Dockerfile                 # Python 3.11-slim image, port 8001
│
├── agent/                     # LLM-facing layer
│   ├── crm_tools.py           # Scoped meta-tool factory: build_crm_tools(scope)
│   ├── tool_scope.py          # Capability registry, ToolScope, filter/guard
│   ├── llm_client.py          # OpenRouter LLM client
│   ├── workers/               # Agent workers (reusable loops)
│   │   ├── base_worker.py     # BaseWorker — foundation for all workers
│   │   ├── writer_worker.py   # WriterWorker — write-focused specialisation
│   │   └── write_policy.py    # Tier/safety write protocol
│   └── stubs/                 # Stubbed tools (swap for real later)
│       └── safety_tools.py    # Safety, session, date stubs
│
├── routers/                   # One file per capability (thin HTTP layer)
│   ├── ner.py                 # POST /ner/extract
│   └── bridge.py              # POST /bridge/* (proxy to Node bridge)
│
├── pipelines/                 # Model logic lives here, never in routers
│   └── ner_pipeline.py        # CRM entity extraction pipeline
│
├── tests/                     # Test suite
│   ├── test_tool_scope.py     # Scope classification + filtering
│   ├── test_crm_tools.py      # Scoped meta-tool guards + role isolation
│   ├── test_writer_worker.py  # Write policy, toolset, stubs
│   └── test_llm_client.py     # LLM client config
│
└── notebooks/                 # Research & experimentation (not served)
    └── CRM_NER_Pipeline_v3.ipynb
```

**Design rule:** Routers are thin — they only parse/validate HTTP input and call a pipeline function. All model logic, thresholds, filters, and regex live in `pipelines/`.

---

## Agent Architecture — Tool Scoping & Workers

### Defense-in-Depth: Three Enforcement Layers

Each agent worker is structurally restricted to its allowed tools — a reader **cannot** write, a writer **cannot** read — enforced at three independent levels:

1. **Catalog filtering** — `get_tool_catalog` only returns tool names within the worker's scope. The reader never even *sees* `create_*`/`update_*`/`delete_*` tools, and the writer never sees `find_*`/`get_*` tools.
2. **Execute guard** — `execute_tool` rejects out-of-scope tool names *before* the bridge is contacted. The bridge is never called.
3. **Bridge role identity** — each scope carries a dedicated Twenty role (`TWENTY_READER_ROLE_ID` vs `TWENTY_WRITER_ROLE_ID`). Even if layers 1–2 are bypassed, Twenty's role permissions block the operation.

### Tool Capability Registry (`agent/tool_scope.py`)

Every bridge tool is classified into a capability by its verb prefix:

| Capability | Verb prefixes | In scope? |
|---|---|---|
| `read` | `find_*`, `find_one_*`, `group_by_*`, `get_*`, `search_*`, `list_*` | Reader only |
| `write` | `create_*`, `update_*`, `delete_*`, `advance_*`, `link_*`, `transfer_*`, `merge_*`, `restore_*` | Writer only |
| `meta` | `get_tool_catalog`, `learn_tools`, `execute_tool`, `get_current_user` | Both |
| `internal` | Safety, session, tier tools | **Neither** — invisible middleware |

Pre-built scopes:

| Scope | Capabilities | Role env var |
|---|---|---|
| `READER_SCOPE` | read, meta | `TWENTY_READER_ROLE_ID` |
| `WRITER_SCOPE` | write, meta | `TWENTY_WRITER_ROLE_ID` |

### Workers (`agent/workers/`)

**`BaseWorker`** is the reusable foundation — a parametrised tool-calling loop:
```python
from agent import BaseWorker, READER_SCOPE

reader = BaseWorker(scope=READER_SCOPE, system_prompt="You are a read-only agent.")
```

**`WriterWorker`** is `BaseWorker` with `WRITER_SCOPE`, a write-focused system prompt, and a `WritePolicy` embedded as invisible middleware inside `execute_tool`. The writer receives specific write instructions from the orchestrator — it does not read or browse.

### Write Protocol — Invisible Middleware (`agent/workers/write_policy.py`)

The LLM never calls safety/tier/session tools directly. They are structural enforcement **inside** `execute_tool`. When the writer calls `execute_tool("delete_person", {...})`:

```
[invisible] lookup_action_tier → tier 1/2/3
[invisible] session_check_duplicate → catch repeats
[invisible] (tier >= 2) check_conflicts → flag value jumps, regressions
   tier 1/2: execute transparently, auto-log the write
   tier 3: return CONFIRMATION_REQUIRED + single-use token
           → user clicks Confirm → agent re-calls with token → executes
```

The agent only sees the result or a `CONFIRMATION_REQUIRED` error with a token it passes back.

### Dual-Role Setup (Part 4)

Create two roles in Twenty's Settings → Roles:

1. **agent-reader** — read-only object permissions (`canReadPerson`, `canReadCompany`, etc.)
2. **agent-writer** — read + create + update permissions (no delete unless required)

```bash
# Get role IDs from the database:
SELECT id, label FROM core.role WHERE "workspaceId"='<WS>';

# Set in .env:
TWENTY_READER_ROLE_ID=<reader-role-uuid>
TWENTY_WRITER_ROLE_ID=<writer-role-uuid>
```

The bridge already accepts `roleId` per call — no bridge changes needed.

---

## Current Capabilities

| Route | Method | Purpose |
|---|---|---|
| `/ner/extract` | `POST` | CRM entity extraction (NER) |
| `/health` | `GET` | Liveness check + model load status |

### NER Pipeline — How It Works

The NER pipeline (`pipelines/ner_pipeline.py`) is a hybrid system purpose-built for CRM text (sales notes, call summaries, email threads):

**Stage 1 — GLiNER Ensemble**  
Two zero-shot transformer models run in parallel:
- `urchade/gliner_large-v2.1` (~800 MB)
- `urchade/gliner_medium-v2.1` (~500 MB)

Target entity types: `person`, `company`, `job title`, `date`, `money`, `location`, `product`, `competitor`.

**Stage 2 — Regex & Rule Layers**  
Pattern-based extractors handle things GLiNER gets wrong or misses:
- Emails (`user@domain.com`)
- Phone numbers (international formats)
- Money (`$45k`, `AED 500,000`, `six figures`)
- Dates (`Q3 2025`, `end of month`, `next Friday`)
- Competitors (context-window rules + known-name dictionary)

**Stage 3 — 12-Step Post-Processing Chain**

| Step | What it does |
|---|---|
| 1 | Per-label confidence threshold (different threshold per entity type) |
| 2 | Exact deduplication by `(label, text)` |
| 3 | Merge adjacent spans of the same label into a single entity |
| 4 | Cross-entity validation (e.g. remove a "person" that is just an email local-part) |
| 5 | Product context filter (only keep products if product-signal words are nearby) |
| 6 | Pronoun company filter (removes "we", "they", "our" tagged as companies) |
| 7 | Person pronoun filter |
| 8 | Money word filter (drops money with no digits or currency symbol) |
| 9 | Location filter (removes generic words like "office", "remote") |
| 10 | Job title filter (removes non-titles like "main contact", "follow-up") |
| 11 | Product single-word blocklist filter |
| 12 | Containment deduplication (drops spans fully contained in a longer span) |

**Output:** A list of entity objects with `label`, `text`, `score`, `start` (char offset), `end` (char offset). The `start`/`end` offsets allow `twenty-server` to do robust text substitution for the masking feature.

---

## Planned Capabilities (Next Phases)

These will be added as additional routers on the same app without changing the deployment:

### `/agent/...` — Agentic Workflows
- LLM-driven multi-step reasoning over CRM context.
- Tool calling (look up a contact, update a deal stage, summarise a thread).
- Conversation memory and session management.
- The AI service will receive the CRM context it needs from the backend per request — it will never query PostgreSQL itself.

### `/rag/...` — Retrieval-Augmented Generation
- Vector embeddings of CRM records and documents.
- A retrieval chain that fetches relevant context before generating a response.
- The vector store (e.g. pgvector or a dedicated store) will be owned and populated by this service.

---

## Seeding Test Data (`seed_data.py`)

`seed_data.py` populates your **local** Twenty database with a realistic, interconnected
dataset for testing the Follow-Up Intelligence Agent — five deal scenarios (Airbnb, Stripe,
Notion, Figma, Datadog) with full email threads, call notes, tasks, a week of calendar
events, plus the agent's own derived profile/shadow/risk data.

It writes into Twenty's **real** schema (no invented CRM tables):

| Data | Twenty tables |
|---|---|
| Companies / contacts / deals | `company` / `person` / `opportunity` |
| Emails | `message`, `messageThread`, `messageParticipant`, `messageChannelMessageAssociation`, `connectedAccount`, `messageChannel` |
| Calls & notes | `note`, `noteTarget` |
| Meetings | `calendarEvent`, `calendarEventParticipant`, `calendarChannel` |
| Tasks | `task`, `taskTarget` |
| Sales reps (Sarah Chen, Marcus Webb) | `core.user`, `core.userWorkspace`, `workspaceMember` |
| Agent-derived data (profile facts, relationships, shadow entities, risk snapshots) | new `followup_agent` schema, **created automatically in the same DB** |

It is **safe and idempotent** — deterministic `uuid5` ids + `ON CONFLICT (id) DO NOTHING`.
Re-running never duplicates rows. It also **reconciles with Twenty's demo data**: if your
workspace already ships the Airbnb/Stripe/Figma/Notion companies (unique domain) or any
matching person (unique email), it reuses those rows instead of erroring, and attaches the
new activities to them. The target workspace schema and id are **discovered at runtime**, so
it works against any local workspace without editing the script.

### How a teammate runs it on their local DB

```bash
# 1. Make sure Postgres + Redis are up and the workspace is initialized.
#    (auto-detects local services vs Docker; idempotent)
bash packages/twenty-utils/setup-dev-env.sh

# 2. From the AI service, install deps (asyncpg is included in requirements.txt).
cd packages/twenty-ai-service
python -m venv .venv && source .venv/bin/activate   # if you don't have one yet
pip install -r requirements.txt                      # or: pip install asyncpg

# 3. Load PG_DATABASE_URL from the server env, then seed.
set -a; source ../twenty-server/.env; set +a
python seed_data.py
```

The script reads `PG_DATABASE_URL` (falls back to
`postgres://postgres:postgres@localhost:5432/default`). On success it prints the discovered
workspace, the per-table row counts, and how many existing records it reused.

> **Note:** this connects directly to Postgres. The AI service at runtime is stateless and
> needs no DB credentials — `seed_data.py` is a standalone dev/test utility, not part of the
> request path.

### Verifying the seed

```bash
# Opportunities + owners
psql "$PG_DATABASE_URL" -c "SELECT name, stage FROM \"<workspace_schema>\".opportunity WHERE name LIKE '%—%';"
# Agent-derived shadow entities
psql "$PG_DATABASE_URL" -c "SELECT name, status FROM followup_agent.shadow_entities;"
```

Find `<workspace_schema>` with:
`psql "$PG_DATABASE_URL" -c "SELECT table_schema FROM information_schema.tables WHERE table_name='person' AND table_schema LIKE 'workspace_%';"`

---

## Running the Service

### Local (development)

```bash
cd packages/twenty-ai-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8001 --reload

# Health check
curl http://localhost:8001/health

# NER smoke test
curl -s -X POST http://localhost:8001/ner/extract \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hi Sarah, I am James Whitfield from NovaTech. Budget is $45,000. Call on Thursday."}'
```

### Docker (via docker-compose)

The service is wired into `packages/twenty-docker/docker-compose.dev.yml` as the `ai-service` container. The backend reaches it via:

```
AI_SERVICE_URL=http://ai-service:8001   # inside Docker network
AI_SERVICE_URL=http://localhost:8001    # when running locally
```

GLiNER model weights (~1.3 GB) are downloaded from HuggingFace on first startup and cached under `HF_HOME`. Mount a Docker volume at `/app/.cache/huggingface` to persist them across container restarts.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HF_HOME` | `/app/.cache/huggingface` | HuggingFace model cache directory |
| `PORT` | `8001` | Port the uvicorn server listens on |
| `LLM_PROVIDER` | — | Must be `openrouter` |
| `LLM_BASE_URL` | — | OpenRouter API base URL |
| `LLM_API_KEY` | — | OpenRouter API key |
| `LLM_MODEL` | — | Model name (e.g. `openai/gpt-4o`) |
| `TWENTY_WORKSPACE_ID` | — | Twenty workspace UUID (shared across scopes) |
| `TWENTY_USER_ID` | — | Twenty user UUID (shared across scopes) |
| `TWENTY_ROLE_ID` | — | Fallback role UUID (used if scope-specific var is unset) |
| `TWENTY_READER_ROLE_ID` | — | Reader agent role UUID (read-only permissions) |
| `TWENTY_WRITER_ROLE_ID` | — | Writer agent role UUID (read + write permissions) |
| `NODE_BRIDGE_BASE_URL` | `http://localhost:3000/agent-bridge` | Node bridge base URL |

The AI service itself needs **no database credentials** — all CRM data it needs arrives in the request body, supplied by `twenty-server`.

---

## Key Design Decisions

**Why a separate Python service?**  
The ML ecosystem (PyTorch, HuggingFace, GLiNER, future LangChain/LlamaIndex) is Python-first. Keeping it isolated from the NestJS backend avoids dependency conflicts, allows independent scaling, and lets us swap or upgrade models without touching the CRM codebase.

**Why stateless?**  
The AI service holds no database connection and no session state. This makes it horizontally scalable, easy to restart (e.g. after a GPU OOM), and simple to test in isolation. All persistence (masking sessions, entity aliases) is handled by `twenty-server` in PostgreSQL.

**Why load models once at startup?**  
GLiNER weights are ~1.3 GB. Loading them per-request would make every API call take 5–10 seconds. The `lifespan` handler in `main.py` loads both models before the server accepts any traffic, keeping inference at ~880 ms/doc on CPU and much faster on GPU.

**Why one port for all capabilities?**  
Adding a new AI feature (agent, RAG) is a matter of writing a new router and `include_router()`-ing it in `main.py`. No new containers, no new service discovery, no new env vars on the backend side.
