"""Batch query endpoint."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
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
from src.services.batch_recovery_service import (
    QUERY_HEARTBEAT_INTERVAL_SECONDS,
    recover_overdue_running_batch_queries,
)
from src.services.notebook_service import get_notebook

logger = logging.getLogger(__name__)
router = APIRouter()
_background_tasks: set[asyncio.Task[None]] = set()
_active_batch_tasks: dict[str, asyncio.Task[None]] = {}


class _BatchClientUnavailableError(RuntimeError):
    """The provider client was unavailable before batch submission."""


@dataclass(frozen=True)
class _ClaimedBatchQuery:
    """One pending row exclusively claimed for a single provider attempt."""

    query_id: int
    question: str
    batch_position: int
    claim_owner: str
    started_at: datetime
    deadline_at: datetime

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "batch_position": self.batch_position,
            "claim_owner": self.claim_owner,
            "started_at": self.started_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "heartbeat_at": self.started_at.isoformat(),
        }


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
    claim_metadata: dict[str, object] | None = None,
) -> dict:
    """Return the only failure facts safe to persist or expose."""
    return {
        **(claim_metadata or {}),
        "error_type": _error_type(exc),
        "outcome_ambiguous": outcome_ambiguous,
        "retry_safe": retry_safe,
        "batch_position": batch_position,
    }


def _background_task_done(batch_id: str, task: asyncio.Task[None]) -> None:
    """Release a completed task and retrieve failures without leaking details."""
    _background_tasks.discard(task)
    if _active_batch_tasks.get(batch_id) is task:
        _active_batch_tasks.pop(batch_id, None)
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
    """Schedule at most one local task per batch and retain a strong reference."""
    active = _active_batch_tasks.get(batch_id)
    if active is not None and not active.done():
        return active
    task = asyncio.create_task(
        _process_batch(batch_id, notebook_id, delay_seconds),
        name=f"batch-query-{batch_id}",
    )
    _background_tasks.add(task)
    _active_batch_tasks[batch_id] = task
    task.add_done_callback(lambda completed: _background_task_done(batch_id, completed))
    return task


async def shutdown_batch_tasks() -> int:
    """Cancel and await retained batch work before shared clients are closed.

    Cancellation deliberately leaves an owned row in ``running``.  Its final
    heartbeat then expires and recovery records an ambiguous interruption;
    shutdown must never turn a possibly submitted provider request back into
    replayable pending work.
    """
    tasks = tuple(task for task in _background_tasks if not task.done())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        # Done callbacks normally maintain both registries.  Clear them
        # explicitly as well so shutdown does not depend on callback ordering.
        for task in tasks:
            _background_tasks.discard(task)
            for batch_id, active in tuple(_active_batch_tasks.items()):
                if active is task:
                    _active_batch_tasks.pop(batch_id, None)
    return len(tasks)


async def schedule_pending_batch_queries() -> int:
    """Schedule every durable pending batch found during process startup."""
    async with async_session() as db:
        result = await db.execute(
            select(Query.batch_id, Query.notebook_id)
            .where(Query.batch_id.is_not(None), Query.status == "pending")
            .distinct()
        )
        batches = list(result.all())
    scheduled = 0
    for batch_id, notebook_id in batches:
        if not isinstance(batch_id, str) or not isinstance(notebook_id, str):
            continue
        _start_batch_task(batch_id, notebook_id, 0)
        scheduled += 1
    if scheduled:
        logger.info("Scheduled pending batch queries batch_count=%d", scheduled)
    return scheduled


@router.post(
    "/notebooks/{notebook_id}/batch-query",
    response_model=BatchQueryResponse,
    status_code=202,
)
async def api_batch_query(
    notebook_id: str,
    body: BatchQueryRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency injection
):
    """Submit exactly one durable question to a notebook.

    Returns immediately with a batch_id and one pending query record.
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
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency injection
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

    pending_query = next(
        (query for query in queries if query.status == "pending"),
        None,
    )
    if pending_query is not None:
        _start_batch_task(batch_id, pending_query.notebook_id, 0)

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


async def _claim_pending_query(
    db: AsyncSession,
    *,
    batch_id: str,
    notebook_id: str,
) -> _ClaimedBatchQuery | None:
    """Atomically move one pending row to an owner-bound running state."""
    started_at = datetime.now(timezone.utc)
    deadline_at = started_at + timedelta(
        seconds=get_settings().notebooklm_query_timeout_seconds
    )
    claim_owner = str(uuid.uuid4())
    # The public contract now permits exactly one row.  The ordered scalar
    # target also lets a legacy pending batch drain safely one row at a time.
    target_id = (
        select(Query.id)
        .where(
            Query.batch_id == batch_id,
            Query.notebook_id == notebook_id,
            Query.status == "pending",
        )
        .order_by(Query.turn_number.asc().nulls_last(), Query.id)
        .limit(1)
        .scalar_subquery()
    )
    claim_metadata = {
        "batch_position": 1,
        "claim_owner": claim_owner,
        "started_at": started_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "heartbeat_at": started_at.isoformat(),
    }
    result = await db.execute(
        update(Query)
        .where(Query.id == target_id, Query.status == "pending")
        .values(status="running", metadata_=claim_metadata)
        .returning(Query.id, Query.question, Query.turn_number)
    )
    row = result.one_or_none()
    # End the claiming transaction before any provider call.  A competing
    # process receives no RETURNING row and therefore has no authority to ask.
    await db.commit()
    if row is None:
        return None
    position = (
        row.turn_number
        if isinstance(row.turn_number, int) and row.turn_number > 0
        else 1
    )
    return _ClaimedBatchQuery(
        query_id=row.id,
        question=row.question,
        batch_position=position,
        claim_owner=claim_owner,
        started_at=started_at,
        deadline_at=deadline_at,
    )


def _owned_running_conditions(claim: _ClaimedBatchQuery) -> tuple[object, ...]:
    return (
        Query.id == claim.query_id,
        Query.status == "running",
        Query.metadata_["claim_owner"].as_string() == claim.claim_owner,
    )


async def _renew_claim_heartbeat(
    claim: _ClaimedBatchQuery,
    *,
    now: datetime | None = None,
) -> bool:
    """Refresh one claim's heartbeat while its owner still holds the row.

    Heartbeats use their own short transaction because the processing session
    remains open across the provider request and AsyncSession is not safe for
    concurrent use.  The row lock serializes renewal with recovery, while the
    status and owner checks fence a late process after ownership is lost.
    """
    async with async_session() as heartbeat_db:
        result = await heartbeat_db.execute(
            select(Query).where(*_owned_running_conditions(claim)).with_for_update()
        )
        query = result.scalar_one_or_none()
        metadata = query.metadata_ if query is not None else None
        if (
            query is None
            or query.status != "running"
            or not isinstance(metadata, dict)
            or metadata.get("claim_owner") != claim.claim_owner
        ):
            return False

        observed_at = now or datetime.now(timezone.utc)
        query.metadata_ = {
            **metadata,
            "heartbeat_at": observed_at.astimezone(timezone.utc).isoformat(),
        }
        await heartbeat_db.commit()
        return True


async def _run_claim_heartbeat(
    claim: _ClaimedBatchQuery,
    stop: asyncio.Event,
) -> None:
    """Renew an owned claim until its provider attempt reaches persistence."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=QUERY_HEARTBEAT_INTERVAL_SECONDS,
            )
            return
        except TimeoutError:
            pass

        try:
            renewed = await _renew_claim_heartbeat(claim)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - heartbeat must survive DB faults
            # A transient database failure must not cancel a provider request.
            # If renewal remains impossible, normal lease expiry fences the
            # eventual late result without replaying the request.
            logger.warning(
                "Batch query heartbeat failed query_id=%s error_type=%s",
                claim.query_id,
                _error_type(exc),
            )
            continue

        if not renewed:
            logger.warning(
                "Batch query heartbeat ownership lost query_id=%s",
                claim.query_id,
            )
            return


async def _persist_owned_failure(
    db: AsyncSession,
    claim: _ClaimedBatchQuery,
    exc: BaseException,
    *,
    outcome_ambiguous: bool = True,
    retry_safe: bool = False,
) -> bool:
    terminal_at = datetime.now(timezone.utc)
    failure_metadata = _failure_metadata(
        exc,
        batch_position=claim.batch_position,
        outcome_ambiguous=outcome_ambiguous,
        retry_safe=retry_safe,
        claim_metadata=claim.metadata,
    )
    failure_metadata["heartbeat_at"] = terminal_at.isoformat()
    result = await db.execute(
        update(Query)
        .where(*_owned_running_conditions(claim))
        .values(
            status="failed",
            metadata_=failure_metadata,
        )
        .returning(Query.id)
    )
    changed = result.scalar_one_or_none() is not None
    await db.commit()
    return changed


async def _persist_owned_success(
    db: AsyncSession,
    claim: _ClaimedBatchQuery,
    ask_result: object,
) -> tuple[bool, int, int]:
    references = list(ask_result.references)
    answer = ask_result.answer
    terminal_at = datetime.now(timezone.utc)
    result = await db.execute(
        update(Query)
        .where(*_owned_running_conditions(claim))
        .values(
            answer=answer,
            conversation_id=ask_result.conversation_id,
            turn_number=ask_result.turn_number,
            status="completed",
            answered_at=terminal_at,
            metadata_={
                **claim.metadata,
                "heartbeat_at": terminal_at.isoformat(),
                "citation_count": len(references),
                "answer_length": len(answer),
            },
        )
        .returning(Query.id)
    )
    changed = result.scalar_one_or_none() is not None
    if changed:
        for ref in references:
            db.add(
                Citation(
                    query_id=claim.query_id,
                    citation_number=ref.citation_number,
                    source_id=ref.source_id,
                    cited_text=ref.cited_text,
                    start_char=ref.start_char,
                    end_char=ref.end_char,
                )
            )
    await db.commit()
    return changed, len(references), len(answer)


async def _process_batch(batch_id: str, notebook_id: str, _delay_seconds: float):
    """Claim and process pending rows without ever replaying a claimed query."""
    logger.info("Batch processing started batch_id=%s", batch_id)

    async with async_session() as db:
        try:
            client = await get_notebooklm_client()
            if client is None:
                raise _BatchClientUnavailableError
        except Exception as exc:  # noqa: BLE001 - client boundary is sanitized
            logger.error(
                "Batch client unavailable batch_id=%s error_type=%s",
                batch_id,
                _error_type(exc),
            )
            return

        conversation_id = None
        while True:
            claim = await _claim_pending_query(
                db,
                batch_id=batch_id,
                notebook_id=notebook_id,
            )
            if claim is None:
                break
            logger.info(
                "Batch query running batch_id=%s query_id=%s "
                "batch_position=%d",
                batch_id,
                claim.query_id,
                claim.batch_position,
            )
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                _run_claim_heartbeat(claim, heartbeat_stop),
                name=f"batch-query-heartbeat-{claim.query_id}",
            )
            try:
                try:
                    remaining_seconds = max(
                        0.001,
                        (
                            claim.deadline_at - datetime.now(timezone.utc)
                        ).total_seconds(),
                    )
                    async with asyncio.timeout(remaining_seconds):
                        ask_result = await client.chat.ask(
                            notebook_id,
                            claim.question,
                            conversation_id=conversation_id,
                        )
                except Exception as exc:  # noqa: BLE001 - provider outcome is ambiguous
                    await _persist_owned_failure(db, claim, exc)
                    logger.error(
                        "Batch query outcome ambiguous batch_id=%s query_id=%s "
                        "batch_position=%d error_type=%s",
                        batch_id,
                        claim.query_id,
                        claim.batch_position,
                        _error_type(exc),
                    )
                else:
                    try:
                        completed, citation_count, answer_length = (
                            await _persist_owned_success(
                                db,
                                claim,
                                ask_result,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - persistence must terminate
                        await db.rollback()
                        await _persist_owned_failure(db, claim, exc)
                        logger.error(
                            "Batch query outcome ambiguous batch_id=%s query_id=%s "
                            "batch_position=%d error_type=%s",
                            batch_id,
                            claim.query_id,
                            claim.batch_position,
                            _error_type(exc),
                        )
                    else:
                        if not completed:
                            logger.warning(
                                "Batch query ownership changed before persistence "
                                "batch_id=%s query_id=%s",
                                batch_id,
                                claim.query_id,
                            )
                            continue
                        conversation_id = ask_result.conversation_id
                        logger.info(
                            "Batch query completed batch_id=%s query_id=%s "
                            "batch_position=%d citation_count=%d answer_length=%d",
                            batch_id,
                            claim.query_id,
                            claim.batch_position,
                            citation_count,
                            answer_length,
                        )
            finally:
                # Keep the lease live through terminal persistence.  Signaling
                # handles a sleeping heartbeat; cancellation also interrupts a
                # renewal already blocked in database I/O.
                heartbeat_stop.set()
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    logger.info("Batch processing complete batch_id=%s", batch_id)
