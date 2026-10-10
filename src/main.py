##uvicorn src.main:app --reload --port 8000

from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.config import get_settings
from src.webhooks.router import router as webhook_router

# Log to the terminal with a readable timestamp.
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
    yield
    logger.info("shutting down")


app = FastAPI(title="PR Review Copilot", version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
