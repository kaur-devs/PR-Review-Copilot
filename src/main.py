##uvicorn src.main:app --reload --port 8000

from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.config import get_settings
from src.recovery import start_recovery
from src.webhooks.router import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

# httpx logs every request at INFO, which drowns out our own lines.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("starting, GitHub App id %s", settings.app_id)

    # Started, not awaited: GitHub allows ten seconds and never retries.
    recovery = start_recovery()

    yield

    # Cancelling leaves it in "processing", which is what recovery looks for.
    if recovery is not None and not recovery.done():
        recovery.cancel()

    logger.info("shutting down")


app = FastAPI(title="PR Review Copilot", version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
