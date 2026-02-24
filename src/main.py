"""FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.database import close_db, init_db

# Configure structured logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Background auth keepalive interval (seconds)
AUTH_KEEPALIVE_INTERVAL = 20 * 60  # 20 minutes


async def _auth_keepalive_loop():
    """Periodically refresh NotebookLM auth cookies to prevent session staleness.

    Runs every AUTH_KEEPALIVE_INTERVAL seconds. If refresh fails, logs warning
    and retries on next cycle — doesn't crash the server.
    """
    await asyncio.sleep(30)  # Let startup finish before first check
    while True:
        try:
            await asyncio.sleep(AUTH_KEEPALIVE_INTERVAL)
            logger.info("[auth-keepalive] Refreshing cookies proactively...")
            from src.services.auth_service import full_auth_refresh
            result = await full_auth_refresh()
            logger.info(
                f"[auth-keepalive] Success: {result['extraction']['cookie_count']} cookies "
                f"in {result['total_duration_s']}s"
            )
        except asyncio.CancelledError:
            logger.info("[auth-keepalive] Shutting down")
            break
        except Exception as e:
            logger.warning(f"[auth-keepalive] Refresh failed (will retry next cycle): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting NotebookLM API...")
    await init_db()
    logger.info("Database initialized")

    # Start background auth keepalive
    keepalive_task = asyncio.create_task(_auth_keepalive_loop())
    logger.info(f"Auth keepalive started (every {AUTH_KEEPALIVE_INTERVAL // 60} min)")

    yield

    logger.info("Shutting down NotebookLM API...")
    keepalive_task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
        pass
    from src.notebooklm_client import close_client
    await close_client()
    await close_db()


app = FastAPI(
    title="NotebookLM API",
    description="HTTP API for Google NotebookLM: notebook management, querying with citations, and Zotero integration.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - allow all origins for now (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
from src.routes.health import router as health_router
from src.routes.notebooks import router as notebooks_router
from src.routes.queries import router as queries_router
from src.routes.sources import router as sources_router
from src.routes.zotero import router as zotero_router
from src.routes.batch import router as batch_router
from src.routes.export import router as export_router
from src.routes.orchestrator import router as orchestrator_router

app.include_router(health_router, tags=["Health"])
app.include_router(notebooks_router, prefix="/api", tags=["Notebooks"])
app.include_router(queries_router, prefix="/api", tags=["Queries"])
app.include_router(sources_router, prefix="/api", tags=["Sources"])
app.include_router(zotero_router, prefix="/api", tags=["Zotero"])
app.include_router(batch_router, prefix="/api", tags=["Batch"])
app.include_router(export_router, prefix="/api", tags=["Export"])
app.include_router(orchestrator_router, prefix="/api", tags=["Orchestrator"])
