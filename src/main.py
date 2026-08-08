"""FastAPI application entry point."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.database import close_db, init_db
from src.security import require_consumer_api_key

# Configure structured logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SENSITIVE_TRANSPORT_LOGGERS = ("httpx", "httpcore")


def suppress_sensitive_transport_logs() -> None:
    """Keep request URLs and authentication parameters out of INFO logs."""
    for logger_name in SENSITIVE_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


suppress_sensitive_transport_logs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events.

    No auth keepalive loop: notebooklm-py rotates cookies itself, and with a
    master token in the profile an expired session re-mints in-process.
    """
    # Reassert after the ASGI server and imported providers finish configuring
    # their own loggers. Request URLs can carry Google authentication values.
    suppress_sensitive_transport_logs()
    logger.info("Starting NotebookLM API...")
    await init_db()
    logger.info("Database initialized")

    yield

    logger.info("Shutting down NotebookLM API...")
    from src.notebooklm_client import close_client
    await close_client()
    await close_db()


app = FastAPI(
    title="NotebookLM API",
    description="HTTP API for Google NotebookLM: notebook management, querying with citations, and Zotero integration.",
    version="0.2.0",
    lifespan=lifespan,
)

# Browser access is denied unless an explicit origin allow-list is configured.
# Ganrl's normal server-to-server calls do not require CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Register routes
from src.routes.batch import router as batch_router
from src.routes.export import router as export_router
from src.routes.health import router as health_router
from src.routes.notebooks import router as notebooks_router
from src.routes.orchestrator import router as orchestrator_router
from src.routes.queries import router as queries_router
from src.routes.remote import router as remote_router
from src.routes.sources import router as sources_router
from src.routes.zotero import router as zotero_router

app.include_router(health_router, tags=["Health"])

# Every research, data, and mutation route under /api is protected. Keep the
# dependency at router registration so the policy appears in generated OpenAPI
# and a newly added endpoint cannot accidentally rely on browser CORS as auth.
protected = [Depends(require_consumer_api_key)]
app.include_router(
    notebooks_router, prefix="/api", tags=["Notebooks"], dependencies=protected
)
app.include_router(
    queries_router, prefix="/api", tags=["Queries"], dependencies=protected
)
app.include_router(
    remote_router,
    prefix="/api",
    tags=["Remote inventory"],
    dependencies=protected,
)
app.include_router(
    sources_router, prefix="/api", tags=["Sources"], dependencies=protected
)
app.include_router(
    zotero_router, prefix="/api", tags=["Zotero"], dependencies=protected
)
app.include_router(
    batch_router, prefix="/api", tags=["Batch"], dependencies=protected
)
app.include_router(
    export_router, prefix="/api", tags=["Export"], dependencies=protected
)
app.include_router(
    orchestrator_router, prefix="/api", tags=["Orchestrator"], dependencies=protected
)
