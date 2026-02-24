"""Notebook CRUD endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import NotebookCreate, NotebookDetail, NotebookResponse, SourceResponse
from src.services.notebook_service import (
    create_notebook,
    delete_notebook,
    get_notebook,
    get_notebook_query_count,
    list_notebooks,
    sync_notebook,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/notebooks", response_model=list[NotebookResponse])
async def api_list_notebooks(db: AsyncSession = Depends(get_db)):
    """List all active notebooks."""
    notebooks = await list_notebooks(db)
    return notebooks


@router.post("/notebooks", response_model=NotebookResponse, status_code=201)
async def api_create_notebook(
    body: NotebookCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new NotebookLM notebook."""
    try:
        notebook = await create_notebook(db, body.title)
        return notebook
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create notebook: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create notebook: {e}")


@router.get("/notebooks/{notebook_id}", response_model=NotebookDetail)
async def api_get_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get notebook details including sources."""
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    query_count = await get_notebook_query_count(db, notebook_id)

    return NotebookDetail(
        id=notebook.id,
        title=notebook.title,
        created_at=notebook.created_at,
        last_synced_at=notebook.last_synced_at,
        source_count=notebook.source_count,
        is_active=notebook.is_active,
        sources=[SourceResponse.model_validate(s) for s in notebook.sources],
        query_count=query_count,
    )


@router.delete("/notebooks/{notebook_id}", status_code=204)
async def api_delete_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a notebook from NotebookLM and local database."""
    deleted = await delete_notebook(db, notebook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sync", response_model=NotebookResponse)
async def api_sync_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Sync notebook state from NotebookLM to local database."""
    try:
        notebook = await sync_notebook(db, notebook_id)
        return notebook
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to sync notebook: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")
