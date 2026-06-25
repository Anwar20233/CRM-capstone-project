# Deploying Twenty + AI service to Railway (single-demo setup)

Goal: a public `https://ripple-production.up.railway.app` URL your client can open,
cheap to keep up (App Sleeping), and trivial to tear down to stop billing.

## Architecture (one Railway project)

| Service       | Source                              | Public? | Internal address                     |
|---------------|-------------------------------------|---------|--------------------------------------|
| `ripple`      | image `twentycrm/twenty:latest`     | YES     | `ripple.railway.internal:3000`       |
| `worker`      | image `twentycrm/twenty:latest`     | no      | —                                    |
| `ai-service`  | build this repo's Dockerfile        | no      | `ai-service.railway.internal:8001`   |
| Postgres      | Railway plugin                      | no      | provided as `${{Postgres.*}}`        |
| Redis         | Railway plugin                      | no      | provided as `${{Redis.REDIS_URL}}`   |

Private services talk over `*.railway.internal` (IPv6). The AI service binds `::`
(see Dockerfile) so it's reachable there.

---

## One-time setup

1. **Create project & databases**
   - New Project → "Empty Project". Rename it `ripple`.
   - `+ New` → Database → **Postgres**. Repeat → **Redis**.

2. **Service: `ripple` (server, the public one)**
   - `+ New` → Docker Image → `twentycrm/twenty:latest`.
   - Rename the service to **`ripple`** (this is what puts "ripple" in the URL).
   - Settings → Networking → **Generate Domain** (port `3000`).
   - Variables (see block below). Set `SERVER_URL` to the generated domain.

3. **Service: `worker`**
   - `+ New` → Docker Image → `twentycrm/twenty:latest`. Rename `worker`.
   - Settings → Deploy → **Custom Start Command**: `yarn worker:prod`
   - Same variables as `ripple` (copy them). No domain.

4. **Service: `ai-service`**
   - `+ New` → GitHub Repo (this repo) → set **Root Directory** to
     `packages/twenty-ai-service` so Railway uses its Dockerfile.
   - Rename `ai-service`. No public domain (internal only).
   - Variables: AI block below.

5. **Optional but recommended — HF model cache volume**
   - On `ai-service`: add a **Volume** mounted at `/app/.cache/huggingface`.
   - Stops it re-downloading the spaCy/transformer weights on every cold start.

6. **Enable App Sleeping (cost saver)**
   - On `ripple` → Settings → **Serverless / App Sleeping: ON**.
   - It scales to zero when idle and wakes on the client's first request
     (a few seconds cold start). Leave `worker` on if you need background jobs
     to keep running; turn it off too if the demo only needs on-demand use.

---

## Variables

### `ripple` and `worker` (identical)
```
NODE_PORT=3000
PG_DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
SERVER_URL=https://ripple-production.up.railway.app   # <-- the generated domain
APP_SECRET=<run: openssl rand -base64 32>
STORAGE_TYPE=local
DISABLE_DB_MIGRATIONS=false
DISABLE_CRON_JOBS_REGISTRATION=false
# Point the CRM at the AI service over the private network:
AI_SERVICE_URL=http://ai-service.railway.internal:8001
```
Note: Twenty's image expects the DB named `default`. If the plugin's
`DATABASE_URL` ends in `/railway`, either create a `default` database in the
Postgres plugin or append `?...`/change the path to `/default`. Easiest: in the
Postgres plugin run `CREATE DATABASE "default";` then set
`PG_DATABASE_URL=${{Postgres.DATABASE_URL}}` rewritten to end in `/default`.

### `ai-service`
```
PORT=8001
LLM_PROVIDER=openrouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=<your OpenRouter key>
LLM_MODEL=qwen/qwen3-next-80b-a3b-instruct
ORCHESTRATOR_MODEL=openai/gpt-4o-mini
HF_HOME=/app/.cache/huggingface

# Talk back to the CRM over the private network (NOT localhost):
NODE_BRIDGE_BASE_URL=http://ripple.railway.internal:3000/agent-bridge
AI_SERVICE_URL=http://ai-service.railway.internal:8001
AI_AGENT_USE_EXTERNAL_ORCHESTRATOR=true

# Twenty identity / roles (from your local .env):
TWENTY_WORKSPACE_ID=ccc3198c-eeb1-43f3-849f-fc72aeffb0a2
TWENTY_USER_ID=711b5b56-3fdf-4d54-8454-737adbab2e65
TWENTY_ROLE_ID=581b27df-2b2a-41d6-9540-73a7f5bcc5b2
TWENTY_READER_ROLE_ID=581b27df-2b2a-41d6-9540-73a7f5bcc5b2
TWENTY_WRITER_ROLE_ID=581b27df-2b2a-41d6-9540-73a7f5bcc5b2

# Optional tracing:
LANGSMITH_API_KEY=<your key>
LANGCHAIN_PROJECT=twenty-ai-service
LANGCHAIN_TRACING_V2=true
```
DO NOT set the leaked `OPENAI_API_Key` from the local .env — OpenRouter is used,
and that key should be rotated.

---

## After first deploy
- The `ripple` server runs DB migrations on boot (`DISABLE_DB_MIGRATIONS=false`).
- Seed/identity data: the workspace/user/role UUIDs above must exist in this fresh
  DB. If they don't, create a workspace via the UI and update the `ai-service`
  IDs, or run your seeding step against `${{Postgres.DATABASE_URL}}`.

## Spin DOWN (stop billing)
- Cheapest while keeping it reachable: App Sleeping (already on) — near-zero idle cost.
- Fully off: Project → each service → **Remove**, or delete the whole project.
  Recreate later by repeating the steps above (config lives here in the repo).
- Keep just the Postgres plugin if you want to preserve data between demos; delete
  it too if you'll re-seed.
```bash
# CLI alternative (after `railway link` to this project):
railway down          # tears down the current environment's deploys
```
