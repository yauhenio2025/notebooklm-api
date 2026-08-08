"""Recover durable batch-query rows left behind by a stopped process."""

import logging

from sqlalchemy import select

from src.database import async_session
from src.models import Query

logger = logging.getLogger(__name__)

_PROCESS_INTERRUPTED = "ProcessInterrupted"


async def recover_orphaned_batch_queries() -> tuple[int, int]:
    """Fail orphaned batch rows without replaying a possibly submitted query.

    ``running`` is committed immediately before the provider call, so it is
    outcome-ambiguous after a restart. ``pending`` is durably queued but was
    never submitted, and remains safe for a deliberate caller retry.
    """
    running_count = 0
    pending_count = 0

    async with async_session() as db:
        result = await db.execute(
            select(Query).where(
                Query.batch_id.is_not(None),
                Query.status.in_(("pending", "running")),
            )
        )
        queries = list(result.scalars().all())

        for query in queries:
            # Keep the state checks defensive so completed, failed, and
            # non-batch rows can never be changed by recovery.
            if query.batch_id is None:
                continue
            if query.status == "running":
                running_count += 1
                query.status = "failed"
                query.metadata_ = {
                    "error_type": _PROCESS_INTERRUPTED,
                    "outcome_ambiguous": True,
                    "retry_safe": False,
                }
            elif query.status == "pending":
                pending_count += 1
                query.status = "failed"
                query.metadata_ = {
                    "error_type": _PROCESS_INTERRUPTED,
                    "outcome_ambiguous": False,
                    "retry_safe": True,
                }

        if running_count or pending_count:
            await db.commit()
            logger.warning(
                "Recovered orphaned batch queries running_count=%d pending_count=%d",
                running_count,
                pending_count,
            )

    return running_count, pending_count
