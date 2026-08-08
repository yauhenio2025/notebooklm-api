"""Protected, read-only views of the actual Google NotebookLM inventory."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from src.notebooklm_client import get_notebooklm_client
from src.schemas import RemoteNotebookResponse, RemoteSourceResponse
from src.services.remote_inventory_service import (
    list_actual_remote_notebooks,
    list_actual_remote_sources,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def require_remote_client() -> Any:
    """Return the live NotebookLM client or a sanitized availability failure."""
    client = await get_notebooklm_client()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "remote_notebooklm_unavailable",
                "message": "The remote NotebookLM inventory is unavailable",
            },
        )
    return client


def _inventory_failure(operation: str, exc: Exception) -> HTTPException:
    """Log only the error class; provider messages may contain source data."""
    logger.warning(
        "Remote NotebookLM inventory failed operation=%s error_type=%s",
        operation,
        type(exc).__name__,
    )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": "remote_inventory_failed",
            "message": "NotebookLM could not return the requested remote inventory",
        },
    )


@router.get("/remote/notebooks", response_model=list[RemoteNotebookResponse])
async def api_list_remote_notebooks(
    client: Annotated[Any, Depends(require_remote_client)],
) -> list[RemoteNotebookResponse]:
    """List actual remote notebooks, independent of wrapper database state."""
    try:
        return await list_actual_remote_notebooks(client)
    except Exception as exc:  # noqa: BLE001 - sanitize every provider-library failure
        raise _inventory_failure("list_notebooks", exc) from None


@router.get(
    "/remote/notebooks/{notebook_id}/sources",
    response_model=list[RemoteSourceResponse],
)
async def api_list_remote_sources(
    notebook_id: str,
    client: Annotated[Any, Depends(require_remote_client)],
) -> list[RemoteSourceResponse]:
    """List actual remote sources for one notebook, independent of local rows."""
    try:
        return await list_actual_remote_sources(client, notebook_id)
    except Exception as exc:  # noqa: BLE001 - sanitize every provider-library failure
        raise _inventory_failure("list_sources", exc) from None
