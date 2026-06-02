"""twenty-ai-service — the single Python service Twenty talks to for model work.

One app, one port. Capabilities are mounted as routers and selected by route
(NER today; agent routes in later workflow phases). GLiNER weights are loaded
once during startup via the lifespan handler, never per request.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from pipelines import load_models, models_loaded
from routers import ner


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the ~1.3 GB GLiNER ensemble once before the service accepts traffic.
    load_models()
    yield


app = FastAPI(title="twenty-ai-service", lifespan=lifespan)

app.include_router(ner.router)


@app.get("/health")
def health():
    return {"status": "ok", "modelsLoaded": models_loaded()}
