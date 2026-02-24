"""Natural language notebook builder endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import BuildNotebookRequest, BuildNotebookResponse, ZoteroCollectionTree

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/build-notebook", response_model=BuildNotebookResponse)
async def api_build_notebook(
    body: BuildNotebookRequest,
    db: AsyncSession = Depends(get_db),
):
    """Build a NotebookLM notebook from a natural language instruction.

    Examples:
    - "Make a notebook from the O'Neill papers in jan"
    - "Create a notebook with Castoriadis primary sources"
    - "Build a notebook from _2026_jan / deutschmann"

    Pipeline:
    1. Fetches Zotero collection tree (362 collections)
    2. Uses Claude to parse instruction → resolve collection
    3. Creates NotebookLM notebook
    4. Uploads all PDFs from matched collection

    Requires ANTHROPIC_API_KEY to be configured.
    """
    from src.services.orchestrator_service import build_notebook_from_instruction

    logger.info(f"Build notebook request: {body.instruction!r}")

    try:
        result = await build_notebook_from_instruction(body.instruction, db)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Build notebook failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")

    if result["status"] == "failed":
        raise HTTPException(status_code=422, detail=result)

    return result


@router.get("/zotero/tree", response_model=list[ZoteroCollectionTree])
async def api_get_collection_tree():
    """Get the full Zotero collection tree with hierarchical paths.

    Returns all 362 collections with full_path, depth, and children info.
    Useful for browsing the library structure.
    """
    from src.services.zotero_service import build_collection_tree

    try:
        tree = await build_collection_tree()
        return [ZoteroCollectionTree(**c) for c in tree]
    except Exception as e:
        logger.error(f"Zotero tree failed: {e}")
        raise HTTPException(status_code=502, detail=f"Zotero API error: {e}")
