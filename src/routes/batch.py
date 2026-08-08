"""Batch query endpoint."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config import get_settings
from src.database import async_session, get_db
from src.models import Citation, Query
from src.notebooklm_client import get_notebooklm_client
from src.schemas import (
    BatchQueryRequest,
    BatchQueryResponse,
    BatchStatus,
    QueryListItem,
)
from src.services.batch_recovery_service import recover_overdue_running_batch_queries
from src.services.notebook_service import get_notebook

logger = logging.getLogger(__name__)
router = APIRouter()
_background_tasks: set[asyncio.Task[None]] = set()


class _BatchClientUnavailableError(RuntimeError):
    """The provider client was unavailable before batch submission."""


def _error_type(exc: BaseException) -> str:
    """Return a bounded class label, never an exception message."""
    name = type(exc).__name__
    if name.isascii() and name.isidentifier():
        return name[:128]
    return "Exception"


def _failure_metadata(
    exc: BaseException,
    *,
    batch_position: int,
    outcome_ambiguous: bool = True,
    retry_safe: bool = False,
) -> dict:
    """Return the only failure facts safe to persist or expose."""
    return {
        "error_type": _error_type(exc),
        "outcome_ambiguous": outcome_ambiguous,
        "retry_safe": retry_safe,
        "batch_position": batch_position,
    }


def _background_task_done(task: asyncio.Task[None]) -> None:
    """Release a completed task and retrieve failures without leaking details."""
    _background_tasks.discard(task)
    if task.cancelled():
        logger.warning("Batch background task cancelled task_name=%s", task.get_name())
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.warning("Batch background task cancelled task_name=%s", task.get_name())
        return
    if exc is not None:
        logger.error(
            "Batch background task failed task_name=%s error_type=%s",
            task.get_name(),
            _error_type(exc),
        )


def _start_batch_task(
    batch_id: str,
    notebook_id: str,
    delay_seconds: float,
) -> asyncio.Task[None]:
    """Schedule batch work while retaining a strong process-lifetime reference."""
    task = asyncio.create_task(
        _process_batch(batch_id, notebook_id, delay_seconds),
        name=f"batch-query-{batch_id}",
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_task_done)
    return task


@router.post(
    "/notebooks/{notebook_id}/batch-query",
    response_model=BatchQueryResponse,
    status_code=202,
)
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

    batch_id = str(uuid.uuid4())
    logger.info(
        "Batch submitted batch_id=%s notebook_id=%s question_count=%d",
        batch_id,
        notebook_id,
        len(body.questions),
    )

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
    _start_batch_task(batch_id, notebook_id, body.delay_seconds)

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
                outcome_ambiguous=q.outcome_ambiguous,
                retry_safe=q.retry_safe,
                error_type=q.error_type,
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
    await recover_overdue_running_batch_queries(db, batch_id=batch_id)
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
        # Keep the established ``pending`` field as the outstanding count so
        # existing pollers do not stop while a provider request is running.
        pending=sum(1 for q in queries if q.status in {"pending", "running"}),
        queries=[
            QueryListItem(
                id=q.id,
                question=q.question,
                status=q.status,
                asked_at=q.asked_at,
                answered_at=q.answered_at,
                citation_count=len(q.citations),
                outcome_ambiguous=q.outcome_ambiguous,
                retry_safe=q.retry_safe,
                error_type=q.error_type,
            )
            for q in queries
        ],
    )


async def _process_batch(batch_id: str, notebook_id: str, delay_seconds: float):
    """Background task: process each pending query in the batch sequentially.

    Updates existing Query records (created by the endpoint) rather than
    creating new ones, to avoid duplicates.
    """
    logger.info("Batch processing started batch_id=%s", batch_id)

    async with async_session() as db:
        result = await db.execute(
            select(Query)
            .where(Query.batch_id == batch_id, Query.status == "pending")
            .order_by(Query.turn_number)
        )
        queries = list(result.scalars().all())

        try:
            client = await get_notebooklm_client()
            if client is None:
                raise _BatchClientUnavailableError
        except Exception as exc:
            for position, query in enumerate(queries, start=1):
                query.status = "failed"
                query.metadata_ = _failure_metadata(
                    exc,
                    batch_position=position,
                    outcome_ambiguous=False,
                    retry_safe=True,
                )
            await db.commit()
            logger.error(
                "Batch client unavailable batch_id=%s error_type=%s",
                batch_id,
                _error_type(exc),
            )
            return

        conversation_id = None  # Use same conversation for the batch
        query_ids = [query.id for query in queries]
        query_timeout_seconds = get_settings().notebooklm_query_timeout_seconds

        for i, query_id in enumerate(query_ids):
            batch_position = i + 1
            query_result = await db.execute(select(Query).where(Query.id == query_id))
            query = query_result.scalar_one()
            query.status = "running"
            query.metadata_ = {
                "batch_position": batch_position,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            # This durable transition is the at-most-once boundary. A process
            # restart may leave ``running`` outstanding, but it must never
            # automatically replay an outcome-ambiguous provider request.
            await db.commit()

            logger.info(
                "Batch query running batch_id=%s query_id=%s "
                "batch_position=%d batch_size=%d",
                batch_id,
                query_id,
                batch_position,
                len(query_ids),
            )
            try:
                async with asyncio.timeout(query_timeout_seconds):
                    ask_result = await client.chat.ask(
                        notebook_id,
                        query.question,
                        conversation_id=conversation_id,
                    )
            except Exception as exc:
                query.status = "failed"
                query.metadata_ = _failure_metadata(
                    exc,
                    batch_position=batch_position,
                )
                await db.commit()
                logger.error(
                    "Batch query outcome ambiguous batch_id=%s query_id=%s "
                    "batch_position=%d error_type=%s",
                    batch_id,
                    query_id,
                    batch_position,
                    _error_type(exc),
                )
            else:
                try:
                    references = list(ask_result.references)
                    query.answer = ask_result.answer
                    query.conversation_id = ask_result.conversation_id
                    query.turn_number = ask_result.turn_number
                    query.status = "completed"
                    query.answered_at = datetime.now(timezone.utc)

                    for ref in references:
                        citation = Citation(
                            query_id=query_id,
                            citation_number=ref.citation_number,
                            source_id=ref.source_id,
                            cited_text=ref.cited_text,
                            start_char=ref.start_char,
                            end_char=ref.end_char,
                        )
                        db.add(citation)

                    query.metadata_ = {
                        "citation_count": len(references),
                        "answer_length": len(ask_result.answer),
                        "batch_position": batch_position,
                    }

                    await db.commit()
                except Exception as exc:
                    # Result persistence can leave the transaction failed.
                    # Roll back and reload before the best-effort terminal
                    # status commit; the provider request is never replayed.
                    await db.rollback()
                    failed_result = await db.execute(
                        select(Query).where(Query.id == query_id)
                    )
                    query = failed_result.scalar_one()
                    query.status = "failed"
                    query.metadata_ = _failure_metadata(
                        exc,
                        batch_position=batch_position,
                    )
                    await db.commit()
                    logger.error(
                        "Batch query outcome ambiguous batch_id=%s query_id=%s "
                        "batch_position=%d error_type=%s",
                        batch_id,
                        query_id,
                        batch_position,
                        _error_type(exc),
                    )
                else:
                    conversation_id = ask_result.conversation_id
                    logger.info(
                        "Batch query completed batch_id=%s query_id=%s "
                        "batch_position=%d citation_count=%d answer_length=%d",
                        batch_id,
                        query_id,
                        batch_position,
                        len(references),
                        len(ask_result.answer),
                    )

            if i < len(query_ids) - 1:
                await asyncio.sleep(delay_seconds)

    logger.info("Batch processing complete batch_id=%s", batch_id)
