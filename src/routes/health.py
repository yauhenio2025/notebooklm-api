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


def _auth_state() -> str:
    """Describe the master-token auth profile state without touching Google."""
    try:
        from src.notebooklm_client import _profile_paths

        storage_path, master_token_path = _profile_paths()
        if master_token_path.exists():
            return "master_token"
        if storage_path.exists():
            return "storage_state_only"

        settings = get_settings()
        if settings.master_token_file:
            return "secret_file_pending"  # configured but not seeded yet
        return "not_configured"
    except Exception as e:
        logger.error(f"Auth state check failed: {e}")
        return f"error: {type(e).__name__}"


@router.get("/health", response_model=HealthResponse)
async def health_check(
    db: AsyncSession = Depends(get_db),
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

    result.notebooklm_auth = _auth_state()
    if result.notebooklm_auth == "not_configured":
        result.status = "degraded"

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

    # NotebookLM auth: probe the live client
    result.notebooklm_auth = _auth_state()
    if result.notebooklm_auth in ("master_token", "storage_state_only"):
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
    elif result.notebooklm_auth == "not_configured":
        result.status = "degraded"

    # Zotero
    result.zotero_configured = bool(settings.zotero_api_key)

    return result


@router.post("/api/auth/refresh", response_model=AuthRefreshResponse)
async def refresh_auth():
    """Re-mint NotebookLM cookies from the master token and reset the client.

    Headless: mints fresh web cookies from master_token.json in the profile dir
    (seeded from the MASTER_TOKEN_FILE secret on Render). The library also
    self-heals expired sessions automatically; this endpoint is the manual
    recovery path.
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
