"""Health check and status endpoints."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.database import get_db
from src.schemas import HealthResponse, StatusResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Basic health check for Render monitoring."""
    result = HealthResponse(status="ok", version="0.1.0")

    # Check database
    try:
        await db.execute(text("SELECT 1"))
        result.database = "connected"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        result.database = f"error: {type(e).__name__}"
        result.status = "degraded"

    # Check NotebookLM auth
    if settings.notebooklm_auth_json:
        result.notebooklm_auth = "configured"
    else:
        result.notebooklm_auth = "not_configured"

    return result


@router.get("/status", response_model=StatusResponse)
async def status(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Detailed status including NotebookLM client state and DB tables."""
    result = StatusResponse(status="ok", version="0.1.0")

    # Database connectivity and tables
    try:
        await db.execute(text("SELECT 1"))
        result.database = "connected"

        # List tables
        rows = await db.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        result.database_tables = [row[0] for row in rows.fetchall()]
    except Exception as e:
        logger.error(f"Database status check failed: {e}")
        result.database = f"error: {type(e).__name__}"
        result.status = "degraded"

    # NotebookLM auth status
    if settings.notebooklm_auth_json:
        result.notebooklm_auth = "configured"
        # Try to initialize client and count notebooks
        try:
            from src.notebooklm_client import get_notebooklm_client

            client = get_notebooklm_client()
            if client:
                notebooks = client.notebooks.list()
                result.notebooklm_notebooks = len(notebooks)
                result.notebooklm_auth = "active"
        except Exception as e:
            logger.error(f"NotebookLM client check failed: {e}")
            result.notebooklm_auth = f"error: {type(e).__name__}"
            result.status = "degraded"
    else:
        result.notebooklm_auth = "not_configured"

    # Zotero
    result.zotero_configured = bool(settings.zotero_api_key)

    return result
