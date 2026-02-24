"""Source upload orchestration (Zotero -> NotebookLM)."""

import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Source
from src.notebooklm_client import get_notebooklm_client

logger = logging.getLogger(__name__)


async def list_sources(db: AsyncSession, notebook_id: str) -> list[Source]:
    """List all sources for a notebook."""
    result = await db.execute(
        select(Source)
        .where(Source.notebook_id == notebook_id)
        .order_by(Source.uploaded_at.desc())
    )
    return list(result.scalars().all())


async def upload_file_source(
    db: AsyncSession,
    notebook_id: str,
    file_path: str,
    file_name: str,
    title: str | None = None,
    zotero_key: str | None = None,
) -> Source:
    """Upload a local file as a source to a NotebookLM notebook."""
    client = await get_notebooklm_client()
    if not client:
        raise RuntimeError("NotebookLM client not available")

    logger.info(f"Uploading source to notebook {notebook_id}: {file_name}")

    # Upload to NotebookLM (async) with wait=True to poll for readiness
    src_result = await client.sources.add_file(
        notebook_id, file_path, wait=True, wait_timeout=120.0
    )

    logger.info(f"Source {src_result.id} uploaded and ready")

    # Sync canonical ID: NotebookLM may assign a different ID than the upload returned
    canonical_id = src_result.id
    try:
        nlm_sources = await client.sources.list(notebook_id)
        for nlm_src in nlm_sources:
            nlm_title = getattr(nlm_src, "title", None) or ""
            if nlm_title and file_name and nlm_title.lower() == file_name.lower():
                canonical_id = nlm_src.id
                logger.info(f"Canonical ID resolved: {src_result.id} -> {canonical_id}")
                break
    except Exception as e:
        logger.warning(f"Failed to sync canonical source ID: {e}")

    # Persist to DB with canonical ID
    source = Source(
        id=canonical_id,
        notebook_id=notebook_id,
        title=title or src_result.title or file_name,
        source_type=str(src_result.kind) if hasattr(src_result, "kind") else "pdf",
        zotero_key=zotero_key,
        file_name=file_name,
        status="ready" if src_result.is_ready else "processing",
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)

    logger.info(f"Source persisted: {source.id} - {source.title}")
    return source


async def upload_from_zotero(
    db: AsyncSession,
    notebook_id: str,
    item_keys: list[str],
) -> list[Source]:
    """Download PDFs from Zotero and upload them to a NotebookLM notebook."""
    from src.services.zotero_service import download_pdf, get_item_details

    uploaded = []

    for key in item_keys:
        tmp_dir = None
        try:
            logger.info(f"Processing Zotero item: {key}")

            # Get metadata
            item = await get_item_details(key)
            title = item.get("title", key)
            pdf_filename = item.get("pdf_filename")

            if not pdf_filename:
                logger.warning(f"No PDF attachment found for {key}, skipping")
                continue

            # Check if already uploaded
            existing = await db.execute(
                select(Source).where(
                    Source.notebook_id == notebook_id,
                    Source.zotero_key == key,
                )
            )
            if existing.scalar_one_or_none():
                logger.info(f"Source already uploaded for Zotero key {key}, skipping")
                continue

            # Download PDF using real filename so NotebookLM sees it
            tmp_dir = tempfile.mkdtemp(prefix="zotero_")
            tmp_path = os.path.join(tmp_dir, pdf_filename)
            await download_pdf(key, tmp_path)

            # Upload to NotebookLM
            try:
                source = await upload_file_source(
                    db=db,
                    notebook_id=notebook_id,
                    file_path=tmp_path,
                    file_name=pdf_filename,
                    title=title,
                    zotero_key=key,
                )
                # Store Zotero bibliographic data on the source
                source.authors = ", ".join(item.get("creators", []))
                source.publication_date = item.get("date", "")
                source.item_type = item.get("item_type", "")
                await db.commit()

                uploaded.append(source)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                tmp_dir = None

        except Exception as e:
            logger.error(f"Failed to upload Zotero item {key}: {e}")
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    return uploaded


async def sync_source_ids(db: AsyncSession, notebook_id: str) -> dict:
    """Sync DB source IDs with canonical NotebookLM IDs for an existing notebook.

    Useful for notebooks where sources were uploaded before the canonical ID fix.
    Matches by title/filename and updates the DB record's primary key.
    """
    client = await get_notebooklm_client()
    if not client:
        return {"error": "NotebookLM client not available"}

    nlm_sources = await client.sources.list(notebook_id)
    db_sources = await list_sources(db, notebook_id)

    updated = []
    for db_src in db_sources:
        for nlm_src in nlm_sources:
            nlm_title = getattr(nlm_src, "title", None) or ""
            match = False
            # Match by filename
            if db_src.file_name and nlm_title.lower() == db_src.file_name.lower():
                match = True
            # Match by title
            elif db_src.title and nlm_title.lower() == db_src.title.lower():
                match = True

            if match and nlm_src.id != db_src.id:
                old_id = db_src.id
                # Delete old record and insert with new ID (PK change)
                await db.delete(db_src)
                await db.flush()
                new_source = Source(
                    id=nlm_src.id,
                    notebook_id=db_src.notebook_id,
                    title=db_src.title,
                    source_type=db_src.source_type,
                    zotero_key=db_src.zotero_key,
                    file_name=db_src.file_name,
                    status=db_src.status,
                    uploaded_at=db_src.uploaded_at,
                    authors=db_src.authors,
                    publication_date=db_src.publication_date,
                    item_type=db_src.item_type,
                    metadata_=db_src.metadata_,
                )
                db.add(new_source)
                updated.append({"old_id": old_id, "new_id": nlm_src.id, "title": db_src.title})
                logger.info(f"Synced source ID: {old_id} -> {nlm_src.id} ({db_src.title})")
                break

    if updated:
        await db.commit()

    return {"synced": len(updated), "details": updated}


async def delete_source(db: AsyncSession, notebook_id: str, source_id: str) -> bool:
    """Remove a source from NotebookLM and DB."""
    result = await db.execute(
        select(Source).where(
            Source.id == source_id,
            Source.notebook_id == notebook_id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        return False

    client = await get_notebooklm_client()
    if client:
        try:
            await client.sources.delete(notebook_id, source_id)
        except Exception as e:
            logger.warning(f"Failed to delete source from NotebookLM: {e}")

    await db.delete(source)
    await db.commit()
    logger.info(f"Deleted source {source_id} from notebook {notebook_id}")
    return True
