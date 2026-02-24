"""Query execution and citation extraction service."""

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
    """Ask a question to a NotebookLM notebook and persist the response.

    1. Creates a pending query record
    2. Sends the question via notebooklm-py
    3. Extracts answer + citations from the response
    4. Persists everything to DB
    """
    client = get_notebooklm_client()
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
        # Send question to NotebookLM
        result = client.notebooks.ask(notebook_id, question)

        # Extract answer text
        answer_text = ""
        if hasattr(result, "text"):
            answer_text = result.text
        elif hasattr(result, "answer"):
            answer_text = result.answer
        elif isinstance(result, str):
            answer_text = result
        else:
            answer_text = str(result)

        query.answer = answer_text
        query.status = "completed"
        query.answered_at = datetime.now(timezone.utc)

        # Extract citations
        citations = []
        if hasattr(result, "references") and result.references:
            for i, ref in enumerate(result.references, 1):
                citation = Citation(
                    query_id=query.id,
                    citation_number=i,
                    source_id=getattr(ref, "source_id", None),
                    source_title=getattr(ref, "source_title", None) or getattr(ref, "title", None),
                    cited_text=getattr(ref, "cited_text", None) or getattr(ref, "text", None),
                    start_char=getattr(ref, "start_index", None),
                    end_char=getattr(ref, "end_index", None),
                )
                citations.append(citation)
                db.add(citation)
        elif hasattr(result, "citations") and result.citations:
            for i, cit in enumerate(result.citations, 1):
                citation = Citation(
                    query_id=query.id,
                    citation_number=i,
                    source_id=getattr(cit, "source_id", None),
                    source_title=getattr(cit, "source_title", None) or getattr(cit, "title", None),
                    cited_text=getattr(cit, "cited_text", None) or getattr(cit, "text", None),
                    start_char=getattr(cit, "start_index", None),
                    end_char=getattr(cit, "end_index", None),
                )
                citations.append(citation)
                db.add(citation)

        # Store raw response metadata
        query.metadata_ = {
            "response_type": type(result).__name__,
            "citation_count": len(citations),
            "answer_length": len(answer_text),
        }

        await db.commit()
        await db.refresh(query)

        logger.info(
            f"Query {query.id}: completed - {len(answer_text)} chars, "
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
