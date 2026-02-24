"""Notebook CRUD operations against NotebookLM + database persistence."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import Notebook, Source, Query
from src.notebooklm_client import get_notebooklm_client

logger = logging.getLogger(__name__)


async def list_notebooks(db: AsyncSession) -> list[Notebook]:
    """List all notebooks from database."""
    result = await db.execute(
        select(Notebook).where(Notebook.is_active == True).order_by(Notebook.created_at.desc())
    )
    return list(result.scalars().all())


async def get_notebook(db: AsyncSession, notebook_id: str) -> Notebook | None:
    """Get a notebook with its sources."""
    result = await db.execute(
        select(Notebook)
        .where(Notebook.id == notebook_id)
        .options(selectinload(Notebook.sources))
    )
    return result.scalar_one_or_none()


async def create_notebook(db: AsyncSession, title: str) -> Notebook:
    """Create a new notebook via NotebookLM and persist to DB."""
    client = get_notebooklm_client()
    if not client:
        raise RuntimeError("NotebookLM client not available - check auth configuration")

    logger.info(f"Creating notebook: {title}")
    nb = client.notebooks.create(title=title)

    notebook = Notebook(
        id=nb.id,
        title=title,
        created_at=datetime.now(timezone.utc),
        source_count=0,
        is_active=True,
    )
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)

    logger.info(f"Notebook created: {notebook.id} - {title}")
    return notebook


async def delete_notebook(db: AsyncSession, notebook_id: str) -> bool:
    """Delete a notebook from NotebookLM and DB."""
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        return False

    client = get_notebooklm_client()
    if client:
        try:
            client.notebooks.delete(notebook_id)
            logger.info(f"Deleted notebook from NotebookLM: {notebook_id}")
        except Exception as e:
            logger.warning(f"Failed to delete from NotebookLM (may already be gone): {e}")

    await db.delete(notebook)
    await db.commit()
    logger.info(f"Deleted notebook from DB: {notebook_id}")
    return True


async def sync_notebook(db: AsyncSession, notebook_id: str) -> Notebook:
    """Sync notebook state from NotebookLM to database.

    Fetches current notebook metadata and source list from NotebookLM,
    updates the local DB record, and reconciles sources.
    """
    client = get_notebooklm_client()
    if not client:
        raise RuntimeError("NotebookLM client not available")

    logger.info(f"Syncing notebook: {notebook_id}")

    # Get current state from NotebookLM
    nb = client.notebooks.get(notebook_id)
    sources_list = client.sources.list(notebook_id)

    # Upsert notebook
    notebook = await get_notebook(db, notebook_id)
    if not notebook:
        notebook = Notebook(
            id=notebook_id,
            title=nb.title if hasattr(nb, "title") else "Synced Notebook",
            created_at=datetime.now(timezone.utc),
        )
        db.add(notebook)

    notebook.last_synced_at = datetime.now(timezone.utc)
    notebook.source_count = len(sources_list)
    notebook.is_active = True

    # Sync sources
    existing_source_ids = set()
    if notebook.sources:
        existing_source_ids = {s.id for s in notebook.sources}

    for src_item in sources_list:
        src_id = src_item.id if hasattr(src_item, "id") else str(src_item)
        if src_id not in existing_source_ids:
            source = Source(
                id=src_id,
                notebook_id=notebook_id,
                title=getattr(src_item, "title", src_id),
                source_type=getattr(src_item, "type", "unknown"),
                status="ready",
            )
            db.add(source)

    await db.commit()
    await db.refresh(notebook)
    logger.info(f"Synced notebook {notebook_id}: {len(sources_list)} sources")
    return notebook


async def get_notebook_query_count(db: AsyncSession, notebook_id: str) -> int:
    """Count queries for a notebook."""
    result = await db.execute(
        select(func.count(Query.id)).where(Query.notebook_id == notebook_id)
    )
    return result.scalar_one()
