"""Query endpoints - ask questions and view responses."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import QueryListItem, QueryRequest, QueryResponse
from src.services.notebook_service import get_notebook
from src.services.query_service import ask_question, get_query, list_queries

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/notebooks/{notebook_id}/query", response_model=QueryResponse)
async def api_query_notebook(
    notebook_id: str,
    body: QueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ask a question to a NotebookLM notebook and get a response with citations."""
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    try:
        query = await ask_question(db, notebook_id, body.question)
        # Reload with citations
        query = await get_query(db, query.id)
        return query
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")


@router.get("/notebooks/{notebook_id}/queries", response_model=list[QueryListItem])
async def api_list_queries(
    notebook_id: str,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List past queries for a notebook."""
    queries = await list_queries(db, notebook_id, limit=limit, offset=offset)
    return [
        QueryListItem(
            id=q.id,
            question=q.question,
            status=q.status,
            asked_at=q.asked_at,
            answered_at=q.answered_at,
            citation_count=len(q.citations),
        )
        for q in queries
    ]


@router.get("/notebooks/{notebook_id}/queries/{query_id}", response_model=QueryResponse)
async def api_get_query(
    notebook_id: str,
    query_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific query with full response and citations."""
    query = await get_query(db, query_id)
    if not query or query.notebook_id != notebook_id:
        raise HTTPException(status_code=404, detail="Query not found")
    return query
