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
├── requirements.txt           # Pinned Python dependencies
├── Dockerfile                 # Python 3.11-slim image, port 8001
│
├── routers/                   # One file per capability (thin HTTP layer)
│   └── ner.py                 # POST /ner/extract
│
├── pipelines/                 # Model logic lives here, never in routers
│   └── ner_pipeline.py        # CRM entity extraction pipeline
│
└── notebooks/                 # Research & experimentation (not served)
    └── CRM_NER_Pipeline_v3.ipynb
```

**Design rule:** Routers are thin — they only parse/validate HTTP input and call a pipeline function. All model logic, thresholds, filters, and regex live in `pipelines/`.

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

Target entity types: `person`, `company`, `deal`, `job title`, `date`, `money`, `location`, `product`, `competitor`.

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
