# twenty-ai-service

The single Python service Twenty's backend calls for model/agent work. One app,
one port (`8001`), with capabilities mounted as routers and selected by route.
Future workflow phases add routers (e.g. `/agent/...`) without changing the
deployment.

## Capabilities

| Route | Purpose |
| --- | --- |
| `POST /ner/extract` | CRM entity recognition (person, company, deal, money, date, …) |
| `GET /health` | Liveness + whether models are loaded |

## NER pipeline

`pipelines/ner_pipeline.py` is ported from `notebooks/CRM_NER_Pipeline_v3.ipynb`
(the source of truth / reference). It is a hybrid of a GLiNER ensemble
(`urchade/gliner_large-v2.1` + `urchade/gliner_medium-v2.1`, zero-shot), regex
extractors (email/phone/money/date) and competitor context rules, followed by a
12-step post-processing chain. The backend additionally receives `start`/`end`
character offsets per entity for robust masked-text replacement.

The ~1.3 GB of GLiNER weights download from HuggingFace on first startup and are
cached under `HF_HOME`. CPU inference is ~880 ms/doc; a GPU is used automatically
if available.

## Run locally

```bash
cd packages/twenty-ai-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8001

# smoke test
curl -s localhost:8001/health
curl -s -X POST localhost:8001/ner/extract \
  -H 'content-type: application/json' \
  -d '{"text":"Hi Sarah, I am James Whitfield from NovaTech Solutions. Budget is $45,000."}'
```

## Run with Docker

Built and wired into `packages/twenty-docker/docker-compose.dev.yml` as the
`ai-service` service. The backend reaches it via the `AI_SERVICE_URL` env var
(default `http://localhost:8001`).
