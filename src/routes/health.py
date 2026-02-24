"""Health check and status endpoints."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.database import get_db
from src.schemas import AuthRefreshResponse, HealthResponse, StatusResponse

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

            client = await get_notebooklm_client()
            if client:
                notebooks = await client.notebooks.list()
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


@router.post("/api/auth/refresh", response_model=AuthRefreshResponse)
async def refresh_auth():
    """Refresh NotebookLM authentication by extracting fresh cookies from the DigitalOcean droplet.

    This endpoint:
    1. SSHes to the droplet (root@207.154.192.181)
    2. Connects to Chrome via CDP on port 9444
    3. Extracts the current storage state (cookies + localStorage)
    4. Resets the in-memory NotebookLM client with fresh cookies

    Note: This updates the running process only. To persist across deploys,
    update NOTEBOOKLM_AUTH_JSON on Render.

    Requires DROPLET_SSH_KEY env var to be configured.
    """
    from src.services.auth_service import full_auth_refresh

    logger.info("[auth-refresh] Manual auth refresh triggered via API")

    try:
        result = await full_auth_refresh()
        return AuthRefreshResponse(**result)
    except RuntimeError as e:
        logger.error(f"[auth-refresh] Failed: {e}")
        return AuthRefreshResponse(
            status="failed",
            error=str(e),
        )
