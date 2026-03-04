"""Query execution and citation extraction service.

Uses client.chat.ask() which returns AskResult with:
- .answer (str): The AI-generated answer text
- .conversation_id (str): UUID for this conversation
- .turn_number (int): Position in conversation
- .references (list[ChatReference]): Citation data with:
  - .source_id, .citation_number, .cited_text, .start_char, .end_char

Includes auto-retry with auth refresh for transient failures (stale sessions,
RPC errors, timeouts).
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import Citation, Query
from src.notebooklm_client import get_notebooklm_client

logger = logging.getLogger(__name__)

# Error patterns that indicate auth/session staleness (retryable)
_RETRYABLE_PATTERNS = [
    "not available",
    "no result found for rpc",
    "chat request timed out",
    "session",
    "unauthorized",
    "unauthenticated",
]


def _is_retryable(error: Exception) -> bool:
    """Check if an error is likely caused by stale auth/session."""
    msg = str(error).lower()
    return any(pat in msg for pat in _RETRYABLE_PATTERNS)


async def ask_question(
    db: AsyncSession,
    notebook_id: str,
    question: str,
    conversation_id: str | None = None,
    max_retries: int = 2,
) -> Query:
    """Ask a question to a NotebookLM notebook and persist the response.

    Automatically retries with auth refresh on transient failures (stale sessions,
    RPC errors, timeouts). Up to max_retries attempts.
    """
    client = await get_notebooklm_client()
    if not client:
        # Try auth refresh before giving up
        client = await _refresh_and_get_client()
        if not client:
            raise RuntimeError("NotebookLM client not available - check auth configuration")

    # Create pending query
    query = Query(
        notebook_id=notebook_id,
        question=question,
        conversation_id=conversation_id,
        status="pending",
        asked_at=datetime.now(timezone.utc),
    )
    db.add(query)
    await db.commit()
    await db.refresh(query)
    logger.info(f"Query {query.id}: asking '{question[:80]}...'")

    # Attempt with retries on transient failures
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await client.chat.ask(
                notebook_id,
                question,
                conversation_id=conversation_id,
            )
            break  # Success
        except Exception as e:
            last_error = e
            if attempt < max_retries and _is_retryable(e):
                logger.warning(
                    f"Query {query.id}: attempt {attempt} failed with retryable error: {e}. "
                    f"Refreshing auth and retrying in 3s..."
                )
                client = await _refresh_and_get_client()
                if not client:
                    raise RuntimeError(f"Auth refresh failed during query retry: {e}") from e
                await asyncio.sleep(3)
            else:
                raise
    else:
        raise last_error  # All retries exhausted

    try:

        # Extract answer - AskResult has .answer attribute
        query.answer = result.answer
        query.conversation_id = result.conversation_id
        query.turn_number = result.turn_number
        query.status = "completed"
        query.answered_at = datetime.now(timezone.utc)

        # Extract citations from .references (list of ChatReference)
        citations = []
        for ref in result.references:
            citation = Citation(
                query_id=query.id,
                citation_number=ref.citation_number,
                source_id=ref.source_id,
                source_title=None,  # Resolved below
                cited_text=ref.cited_text,
                start_char=ref.start_char,
                end_char=ref.end_char,
            )
            citations.append(citation)
            db.add(citation)

        # Resolve source titles and bibliographic data from our DB
        if citations:
            source_ids = {c.source_id for c in citations if c.source_id}
            if source_ids:
                from src.models import Source
                source_result = await db.execute(
                    select(Source).where(Source.id.in_(source_ids))
                )
                source_map = {s.id: s for s in source_result.scalars().all()}
                for cit in citations:
                    src = source_map.get(cit.source_id)
                    if src:
                        cit.source_title = src.title
                        cit.source_authors = src.authors
                        cit.source_date = src.publication_date

        # Enrich cited_text using fulltext context and resolve missing titles
        await _enrich_citations(client, notebook_id, citations)

        query.metadata_ = {
            "response_type": type(result).__name__,
            "citation_count": len(citations),
            "answer_length": len(result.answer),
            "is_follow_up": result.is_follow_up,
        }

        await db.commit()
        await db.refresh(query)

        logger.info(
            f"Query {query.id}: completed - {len(result.answer)} chars, "
            f"{len(citations)} citations"
        )

    except Exception as e:
        logger.error(f"Query {query.id}: failed - {e}")
        query.status = "failed"
        query.metadata_ = {"error": str(e), "error_type": type(e).__name__}
        await db.commit()
        raise

    return query


async def get_query(db: AsyncSession, query_id: int) -> Query | None:
    """Get a query with its citations."""
    result = await db.execute(
        select(Query)
        .where(Query.id == query_id)
        .options(selectinload(Query.citations))
    )
    return result.scalar_one_or_none()


async def list_queries(
    db: AsyncSession,
    notebook_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[Query]:
    """List queries for a notebook."""
    result = await db.execute(
        select(Query)
        .where(Query.notebook_id == notebook_id)
        .options(selectinload(Query.citations))
        .order_by(Query.asked_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def _enrich_citations(
    client, notebook_id: str, citations: list[Citation], context_chars: int = 300
):
    """Expand short cited_text using SourceFulltext.find_citation_context().

    Also resolves source_title from fulltext when DB lookup missed it.
    Groups citations by source_id to minimize API calls (one get_fulltext per source).

    For citations with NULL cited_text (parser failed to extract text from streaming
    response), attempts to recover text using start_char/end_char as approximate
    positions in the source fulltext.
    """
    by_source: dict[str, list[Citation]] = {}
    for cit in citations:
        if cit.source_id:
            by_source.setdefault(cit.source_id, []).append(cit)

    for source_id, cits in by_source.items():
        try:
            fulltext = await client.sources.get_fulltext(notebook_id, source_id)
        except Exception as e:
            logger.warning(f"Failed to get fulltext for source {source_id}: {e}")
            continue

        ft_content = getattr(fulltext, "content", None) or ""
        ft_title = getattr(fulltext, "title", None)

        for cit in cits:
            if not cit.source_title and ft_title:
                cit.source_title = ft_title

            # Case 1: NULL cited_text — try to recover from fulltext using char positions
            if not cit.cited_text:
                if cit.start_char is not None and ft_content:
                    recovered = _recover_cited_text(
                        ft_content, cit.start_char, cit.end_char, context_chars
                    )
                    if recovered:
                        cit.cited_text = recovered
                        logger.info(
                            f"Recovered NULL citation #{cit.citation_number} "
                            f"from fulltext: {len(recovered)} chars"
                        )
                continue

            # Case 2: short cited_text — expand using substring search
            if len(cit.cited_text) >= context_chars:
                continue
            try:
                matches = fulltext.find_citation_context(
                    cit.cited_text, context_chars=context_chars
                )
                if matches:
                    cit.cited_text = matches[0][0].strip()
                    logger.debug(
                        f"Enriched citation {cit.citation_number}: "
                        f"{len(cit.cited_text)} chars"
                    )
            except Exception as e:
                logger.debug(f"Citation enrichment failed for #{cit.citation_number}: {e}")


def _recover_cited_text(
    content: str, start_char: int, end_char: int | None, context_chars: int = 300
) -> str | None:
    """Try to recover cited text from fulltext using char positions.

    NotebookLM's start_char/end_char reference the internal chunked index, not
    direct fulltext positions, so this is a best-effort heuristic. We try the
    position directly and also search nearby for sentence boundaries.
    """
    content_len = len(content)
    if start_char >= content_len:
        return None

    # Try direct extraction with context window around start_char
    # The char positions often approximate the actual location
    window_start = max(0, start_char - context_chars // 2)
    window_end = min(content_len, start_char + context_chars)

    snippet = content[window_start:window_end].strip()
    if not snippet or len(snippet) < 20:
        return None

    # Trim to sentence boundaries if possible
    # Find first sentence start (capital after period/newline)
    first_period = snippet.find(". ")
    if first_period > 0 and first_period < len(snippet) // 3:
        snippet = snippet[first_period + 2:]

    # Find last sentence end
    last_period = snippet.rfind(".")
    if last_period > len(snippet) * 2 // 3:
        snippet = snippet[: last_period + 1]

    return snippet.strip() if len(snippet.strip()) >= 20 else None


async def reenrich_query_citations(db: AsyncSession, query: Query) -> dict:
    """Re-run enrichment on existing query citations to recover NULL cited_text.

    Returns stats: {query_id, total_citations, null_before, recovered}.
    """
    client = await get_notebooklm_client()
    if not client:
        raise RuntimeError("NotebookLM client not available")

    citations = list(query.citations)
    null_before = sum(1 for c in citations if not c.cited_text)

    logger.info(
        f"Re-enriching query {query.id}: {len(citations)} citations, "
        f"{null_before} with NULL cited_text"
    )

    await _enrich_citations(client, query.notebook_id, citations)
    await db.commit()

    null_after = sum(1 for c in citations if not c.cited_text)
    recovered = null_before - null_after

    logger.info(f"Re-enrichment done: recovered {recovered}/{null_before} NULL citations")

    return {
        "query_id": query.id,
        "total_citations": len(citations),
        "null_before": null_before,
        "recovered": recovered,
    }


async def _refresh_and_get_client():
    """Refresh auth from droplet and return a fresh NotebookLM client.

    Returns None if refresh fails.
    """
    try:
        from src.services.auth_service import full_auth_refresh
        refresh_result = await full_auth_refresh()
        logger.info(
            f"Auth auto-refresh succeeded "
            f"({refresh_result['extraction']['cookie_count']} cookies, "
            f"{refresh_result['total_duration_s']}s)"
        )
        client = await get_notebooklm_client()
        return client
    except Exception as e:
        logger.error(f"Auth auto-refresh failed: {e}")
        return None
