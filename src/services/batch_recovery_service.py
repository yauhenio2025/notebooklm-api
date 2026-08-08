"""Age-gated recovery for batch-query rows left by a stopped process."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import async_session
from src.models import Query

logger = logging.getLogger(__name__)

_PROCESS_INTERRUPTED = "ProcessInterrupted"
QUERY_RECOVERY_GRACE_SECONDS = 60


def _utc_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _query_started_at(query: Query) -> datetime | None:
    metadata = query.metadata_
    if isinstance(metadata, dict):
        started_at = _utc_datetime(metadata.get("started_at"))
        if started_at is not None:
            return started_at
    # Rows created before ``started_at`` was persisted fall back to their
    # durable submission timestamp. This is intentionally conservative for
    # new multi-question rows, which always carry their actual start time.
    return _utc_datetime(query.asked_at)


async def recover_overdue_running_batch_queries(
    db: AsyncSession,
    *,
    batch_id: str | None = None,
    now: datetime | None = None,
    timeout_seconds: int | None = None,
) -> int:
    """Fail only running rows older than the execution bound plus grace.

    ``running`` is committed immediately before the provider call, so it is
    outcome-ambiguous once overdue. ``pending`` rows are never touched: their
    age may reflect a legitimate wait behind earlier questions in the batch.
    """
    statement = select(Query).where(
        Query.batch_id.is_not(None),
        Query.status == "running",
    ).with_for_update()
    if batch_id is not None:
        statement = statement.where(Query.batch_id == batch_id)
    result = await db.execute(statement)
    queries = list(result.scalars().all())

    observed_at = _utc_datetime(now) or datetime.now(timezone.utc)
    configured_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else get_settings().notebooklm_query_timeout_seconds
    )
    overdue_after = timedelta(
        seconds=configured_timeout + QUERY_RECOVERY_GRACE_SECONDS
    )
    recovered_count = 0

    for query in queries:
        if query.batch_id is None or query.status != "running":
            continue
        if batch_id is not None and query.batch_id != batch_id:
            continue
        started_at = _query_started_at(query)
        if started_at is None or observed_at - started_at <= overdue_after:
            continue
        recovered_count += 1
        query.status = "failed"
        query.metadata_ = {
            "error_type": _PROCESS_INTERRUPTED,
            "outcome_ambiguous": True,
            "retry_safe": False,
        }

    if recovered_count:
        await db.commit()
        logger.warning(
            "Recovered overdue running batch queries recovered_count=%d",
            recovered_count,
        )
    return recovered_count


async def recover_orphaned_batch_queries() -> int:
    """Run the age-gated recovery sweep during application startup."""
    async with async_session() as db:
        return await recover_overdue_running_batch_queries(db)
