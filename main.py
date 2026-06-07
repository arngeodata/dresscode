"""
Dresscode — main FastAPI application entry point.

Starts:
  - FastAPI app with inbound webhook and health routes
  - Background worker that polls async_jobs every N seconds
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routes.inbound import router as inbound_router
from app.routes.health import router as health_router
from app.worker import run_worker, run_daily_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the worker as a background task when the server boots."""
    logger.info("Dresscode starting up...")
    worker_task = asyncio.create_task(run_worker())
    digest_task = asyncio.create_task(run_daily_digest())
    yield
    logger.info("Dresscode shutting down...")
    worker_task.cancel()
    digest_task.cancel()
    for task in (worker_task, digest_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Dresscode",
    description="Email-based CV formatting for recruitment agencies",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(inbound_router)
app.include_router(health_router)


@app.get("/")
async def root():
    return {"service": "Dresscode", "status": "running"}
