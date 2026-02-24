"""Query execution and citation extraction service.

Uses client.chat.ask() which returns AskResult with:
- .answer (str): The AI-generated answer text
- .conversation_id (str): UUID for this conversation
- .turn_number (int): Position in conversation
- .references (list[ChatReference]): Citation data with:
  - .source_id, .citation_number, .cited_text, .start_char, .end_char
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import Citation, Query
from src.notebooklm_client import get_notebooklm_client

logger = logging.getLogger(__name__)


async def ask_question(
    db: AsyncSession,
    notebook_id: str,
    question: str,
    conversation_id: str | None = None,
) -> Query:
    """Ask a question to a NotebookLM notebook and persist the response."""
    client = await get_notebooklm_client()
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

    try:
        # Send question via client.chat.ask() (the correct API path)
        result = await client.chat.ask(
            notebook_id,
            question,
            conversation_id=conversation_id,
        )

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

        # Use fulltext.title as fallback for unresolved source titles
        ft_title = getattr(fulltext, "title", None)
        for cit in cits:
            if not cit.source_title and ft_title:
                cit.source_title = ft_title

            # Expand short cited_text
            if not cit.cited_text or len(cit.cited_text) >= context_chars:
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
