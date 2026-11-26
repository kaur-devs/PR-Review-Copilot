##uvicorn src.main:app --reload --port 8000

from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
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


class HealthCheckFilter(logging.Filter):
    """Drop the access-log line for /health, and nothing else.

    The keep-alive pinger asks for /health every five minutes, so without
    this the log is twelve lines an hour of nothing, and the lines that
    matter fall out of the host's short retention. This is on the access
    logger alone: errors, tracebacks and our own logging are untouched, so
    a real problem cannot hide behind it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (
            isinstance(args, tuple) and len(args) >= 3 and args[2] == "/health"
        )


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

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


# Beside src/, so it resolves the same from the repo root and from /app.
LANDING_PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    """The page a person gets. Everything else here is for machines."""
    return FileResponse(LANDING_PAGE)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
