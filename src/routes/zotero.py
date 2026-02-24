"""Zotero browsing endpoints."""

import logging

from fastapi import APIRouter, HTTPException

from src.schemas import ZoteroCollection, ZoteroItem
from src.services.zotero_service import (
    get_item_details,
    list_collection_items,
    list_collections,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/zotero/collections", response_model=list[ZoteroCollection])
async def api_list_collections():
    """List all Zotero collections in the group library."""
    try:
        collections = await list_collections()
        return [ZoteroCollection(**c) for c in collections]
    except Exception as e:
        logger.error(f"Zotero collections list failed: {e}")
        raise HTTPException(status_code=502, detail=f"Zotero API error: {e}")


@router.get(
    "/zotero/collections/{collection_key}/items",
    response_model=list[ZoteroItem],
)
async def api_list_collection_items(collection_key: str):
    """List items in a Zotero collection."""
    try:
        items = await list_collection_items(collection_key)
        return [ZoteroItem(**item) for item in items]
    except Exception as e:
        logger.error(f"Zotero collection items failed: {e}")
        raise HTTPException(status_code=502, detail=f"Zotero API error: {e}")


@router.get("/zotero/items/{item_key}", response_model=ZoteroItem)
async def api_get_item(item_key: str):
    """Get detailed Zotero item metadata including PDF attachment info."""
    try:
        item = await get_item_details(item_key)
        return ZoteroItem(**item)
    except Exception as e:
        logger.error(f"Zotero item details failed: {e}")
        raise HTTPException(status_code=502, detail=f"Zotero API error: {e}")
