"""Source management endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import SourceFromZotero, SourceResponse
from src.services.notebook_service import get_notebook
from src.services.source_service import delete_source, list_sources, upload_from_zotero

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/notebooks/{notebook_id}/sources", response_model=list[SourceResponse])
async def api_list_sources(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
):
    """List all sources in a notebook."""
    sources = await list_sources(db, notebook_id)
    return sources


@router.post(
    "/notebooks/{notebook_id}/sources/from-zotero",
    response_model=list[SourceResponse],
    status_code=201,
)
async def api_upload_from_zotero(
    notebook_id: str,
    body: SourceFromZotero,
    db: AsyncSession = Depends(get_db),
):
    """Upload sources from Zotero to a NotebookLM notebook.

    Provide either item_keys (specific items) or collection_key (all items in collection).
    """
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    item_keys = body.item_keys or []

    # If collection_key provided, get all items from that collection
    if body.collection_key and not item_keys:
        from src.services.zotero_service import list_collection_items

        items = await list_collection_items(body.collection_key)
        item_keys = [item["key"] for item in items if item.get("has_pdf")]

    if not item_keys:
        raise HTTPException(
            status_code=400,
            detail="No items to upload. Provide item_keys or a collection_key with PDF items.",
        )

    try:
        sources = await upload_from_zotero(db, notebook_id, item_keys)
        return sources
    except Exception as e:
        logger.error(f"Zotero upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.delete("/notebooks/{notebook_id}/sources/{source_id}", status_code=204)
async def api_delete_source(
    notebook_id: str,
    source_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Remove a source from a notebook."""
    deleted = await delete_source(db, notebook_id, source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Source not found")
