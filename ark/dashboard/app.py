"""FastAPI application factory with lifespan (start/stop watcher)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import watcher
from .routes import router

logger = logging.getLogger("ark.dashboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting file watcher...")
    task = asyncio.create_task(watcher.poll_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("File watcher stopped.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ARK Dashboard",
        description="Real-time monitoring for ARK research projects",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app
