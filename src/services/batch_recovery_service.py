"""Heartbeat- and deadline-gated recovery for claimed batch-query rows."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session
from src.models import Query

logger = logging.getLogger(__name__)

_PROCESS_INTERRUPTED = "ProcessInterrupted"
QUERY_RECOVERY_GRACE_SECONDS = 60
QUERY_HEARTBEAT_INTERVAL_SECONDS = 15
QUERY_HEARTBEAT_STALE_SECONDS = 75
QUERY_RECOVERY_SWEEP_INTERVAL_SECONDS = 15


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


def _query_heartbeat_at(query: Query) -> datetime | None:
    metadata = query.metadata_
    if isinstance(metadata, dict):
        return _utc_datetime(metadata.get("heartbeat_at"))
    return None


async def recover_overdue_running_batch_queries(
    db: AsyncSession,
    *,
    batch_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Fail running rows whose owner heartbeat or hard deadline has expired.

    New claims carry a heartbeat, allowing a stopped process to be detected in
    less than 90 seconds.  Rows created by an older deployment have no
    heartbeat and retain the conservative persisted-deadline behavior; treating
    a missing heartbeat as immediately stale could falsely fail work still
    owned by the old instance during a rolling deploy.  ``pending`` rows are
    never failed and can be safely scheduled again.
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
        heartbeat_at = _query_heartbeat_at(query)
        heartbeat_stale = heartbeat_at is not None and observed_at > (
            heartbeat_at + timedelta(seconds=QUERY_HEARTBEAT_STALE_SECONDS)
        )
        deadline_overdue = deadline_at is not None and observed_at > (
            deadline_at + timedelta(seconds=QUERY_RECOVERY_GRACE_SECONDS)
        )
        if not heartbeat_stale and not deadline_overdue:
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
    """Run one heartbeat- and deadline-gated recovery sweep."""
    async with async_session() as db:
        return await recover_overdue_running_batch_queries(db)


async def run_batch_recovery_supervisor(
    stop: asyncio.Event,
    *,
    interval_seconds: float = QUERY_RECOVERY_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep throughout the process lifetime so recovery needs no status poll."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            await recover_orphaned_batch_queries()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retained monitor must keep running
            # A temporary database failure must not kill the retained monitor.
            logger.warning(
                "Batch recovery sweep failed error_type=%s",
                type(exc).__name__,
            )
