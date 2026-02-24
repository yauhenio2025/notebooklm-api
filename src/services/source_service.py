"""Source upload orchestration (Zotero -> NotebookLM)."""

import logging
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
    """Upload a local file as a source to a NotebookLM notebook.

    Steps:
    1. Upload via notebooklm-py client
    2. Wait for processing to complete
    3. Persist source record to DB
    """
    client = get_notebooklm_client()
    if not client:
        raise RuntimeError("NotebookLM client not available")

    logger.info(f"Uploading source to notebook {notebook_id}: {file_name}")

    # Upload to NotebookLM
    src_result = client.sources.add_file(notebook_id, file_path)
    source_id = getattr(src_result, "id", None) or str(src_result)

    # Wait for processing
    try:
        if hasattr(src_result, "wait_until_ready"):
            src_result.wait_until_ready(timeout=120)
            logger.info(f"Source {source_id} processed successfully")
    except Exception as e:
        logger.warning(f"Source processing wait failed (may still succeed): {e}")

    # Persist to DB
    source = Source(
        id=source_id,
        notebook_id=notebook_id,
        title=title or file_name,
        source_type="pdf",
        zotero_key=zotero_key,
        file_name=file_name,
        status="ready",
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
    """Download PDFs from Zotero and upload them to a NotebookLM notebook.

    Pipeline for each item:
    1. Fetch item metadata from Zotero API
    2. Download PDF to temp file
    3. Upload to NotebookLM
    4. Record mapping in DB
    5. Clean up temp file
    """
    from src.services.zotero_service import get_item_details, download_pdf

    uploaded = []

    for key in item_keys:
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

            # Download PDF
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
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
                uploaded.append(source)
            finally:
                # Clean up temp file
                Path(tmp_path).unlink(missing_ok=True)

        except Exception as e:
            logger.error(f"Failed to upload Zotero item {key}: {e}")

    return uploaded


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

    client = get_notebooklm_client()
    if client:
        try:
            client.sources.delete(notebook_id, source_id)
        except Exception as e:
            logger.warning(f"Failed to delete source from NotebookLM: {e}")

    await db.delete(source)
    await db.commit()
    logger.info(f"Deleted source {source_id} from notebook {notebook_id}")
    return True
