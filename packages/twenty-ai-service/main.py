"""twenty-ai-service — the single Python service Twenty talks to for model work.

One app, one port. Capabilities are mounted as routers and selected by route
(NER today; agent routes in later workflow phases). GLiNER weights are loaded
once during startup via the lifespan handler, never per request.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from pipelines import load_models, models_loaded
from routers import agent, bridge, ner
from tracing import configure_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Activate LangSmith tracing before anything else so every LLM call,
    # LangGraph execution, and tool invocation is captured from the start.
    configure_tracing()
    # Load the ~1.3 GB GLiNER ensemble once before the service accepts traffic.
    load_models()

    # Initialize the follow-up agent's DB pool + compiled graphs (Step 7).
    # Non-fatal: the rest of the service still works if the followup DB is
    # unavailable (e.g. dev environments without Postgres).
    try:
        from followup.api import dependencies as followup_deps

        await followup_deps.startup()
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "Follow-up API startup failed (non-fatal): %s", exc
        )

    yield

    # Tear down the follow-up DB pool.
    try:
        from followup.api import dependencies as followup_deps

        await followup_deps.shutdown()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title="twenty-ai-service", lifespan=lifespan)

app.include_router(ner.router)
app.include_router(bridge.router)
app.include_router(agent.router)

# Follow-Up Agent REST API (Step 7).
from followup.api import routes as followup_routes

app.include_router(followup_routes.router)


@app.get("/health")
def health():
    return {"status": "ok", "modelsLoaded": models_loaded()}

