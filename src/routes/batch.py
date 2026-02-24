"""Batch query endpoint."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.models import Query
from src.schemas import (
    BatchQueryRequest,
    BatchQueryResponse,
    BatchStatus,
    QueryListItem,
)
from src.services.notebook_service import get_notebook
from src.services.query_service import ask_question

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/notebooks/{notebook_id}/batch-query", response_model=BatchQueryResponse)
async def api_batch_query(
    notebook_id: str,
    body: BatchQueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Submit a batch of questions to a notebook.

    Questions are processed sequentially with configurable delay between them.
    Returns immediately with batch_id and pending query records.
    """
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    batch_id = str(uuid.uuid4())[:8]
    logger.info(f"Batch {batch_id}: {len(body.questions)} questions for notebook {notebook_id}")

    # Create pending query records
    queries = []
    for i, question in enumerate(body.questions):
        q = Query(
            notebook_id=notebook_id,
            question=question,
            batch_id=batch_id,
            turn_number=i + 1,
            status="pending",
            asked_at=datetime.now(timezone.utc),
        )
        db.add(q)
        queries.append(q)

    await db.commit()
    for q in queries:
        await db.refresh(q)

    # Process in background
    asyncio.create_task(_process_batch(batch_id, notebook_id, body.delay_seconds))

    return BatchQueryResponse(
        batch_id=batch_id,
        notebook_id=notebook_id,
        total_questions=len(body.questions),
        queries=[
            QueryListItem(
                id=q.id,
                question=q.question,
                status=q.status,
                asked_at=q.asked_at,
                citation_count=0,
            )
            for q in queries
        ],
    )


@router.get("/batches/{batch_id}", response_model=BatchStatus)
async def api_batch_status(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get the status of a batch query."""
    result = await db.execute(
        select(Query)
        .where(Query.batch_id == batch_id)
        .options(selectinload(Query.citations))
        .order_by(Query.turn_number)
    )
    queries = list(result.scalars().all())

    if not queries:
        raise HTTPException(status_code=404, detail="Batch not found")

    return BatchStatus(
        batch_id=batch_id,
        total=len(queries),
        completed=sum(1 for q in queries if q.status == "completed"),
        failed=sum(1 for q in queries if q.status == "failed"),
        pending=sum(1 for q in queries if q.status == "pending"),
        queries=[
            QueryListItem(
                id=q.id,
                question=q.question,
                status=q.status,
                asked_at=q.asked_at,
                answered_at=q.answered_at,
                citation_count=len(q.citations),
            )
            for q in queries
        ],
    )


async def _process_batch(batch_id: str, notebook_id: str, delay_seconds: float):
    """Background task: process each question in the batch sequentially."""
    from src.database import async_session

    logger.info(f"Batch {batch_id}: starting background processing")

    async with async_session() as db:
        result = await db.execute(
            select(Query)
            .where(Query.batch_id == batch_id, Query.status == "pending")
            .order_by(Query.turn_number)
        )
        queries = list(result.scalars().all())

        for i, query in enumerate(queries):
            try:
                logger.info(f"Batch {batch_id}: processing question {i + 1}/{len(queries)}")
                await ask_question(db, notebook_id, query.question)

                if i < len(queries) - 1:
                    await asyncio.sleep(delay_seconds)

            except Exception as e:
                logger.error(f"Batch {batch_id}: question {i + 1} failed: {e}")
                query.status = "failed"
                query.metadata_ = {"error": str(e)}
                await db.commit()

    logger.info(f"Batch {batch_id}: processing complete")
