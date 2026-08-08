"""Deadline-gated recovery for claimed batch-query rows."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


def _query_deadline_at(query: Query) -> datetime | None:
    metadata = query.metadata_
    if isinstance(metadata, dict):
        return _utc_datetime(metadata.get("deadline_at"))
    return None


async def recover_overdue_running_batch_queries(
    db: AsyncSession,
    *,
    batch_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Fail only running rows past their persisted deadline plus grace.

    The deadline belongs to the task that atomically claimed the row.  Current
    configuration and ``asked_at`` are deliberately irrelevant: a rolling
    deploy must not shorten an in-flight owner's lease, while legacy running
    rows without a trustworthy deadline remain ambiguous for manual handling.
    ``pending`` rows are never failed and can be safely scheduled again.
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
    recovered_count = 0

    for query in queries:
        if query.batch_id is None or query.status != "running":
            continue
        if batch_id is not None and query.batch_id != batch_id:
            continue
        deadline_at = _query_deadline_at(query)
        if deadline_at is None or observed_at <= deadline_at + timedelta(
            seconds=QUERY_RECOVERY_GRACE_SECONDS
        ):
            continue
        recovered_count += 1
        query.status = "failed"
        query.metadata_ = {
            **(query.metadata_ if isinstance(query.metadata_, dict) else {}),
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
