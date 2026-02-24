"""Zotero API client for browsing collections and downloading PDFs.

Supports:
- Paginated collection fetching (handles libraries with 100+ collections)
- Hierarchical tree building with full paths
- Item listing and PDF download
"""

import logging

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

ZOTERO_BASE_URL = "https://api.zotero.org"


def _get_headers() -> dict:
    settings = get_settings()
    return {
        "Zotero-API-Key": settings.zotero_api_key,
        "Zotero-API-Version": "3",
    }


def _group_url() -> str:
    settings = get_settings()
    return f"{ZOTERO_BASE_URL}/groups/{settings.zotero_group_id}"


# ---------------------------------------------------------------------------
# Collection operations
# ---------------------------------------------------------------------------

async def list_collections() -> list[dict]:
    """List all collections in the Zotero group library (paginated)."""
    all_collections = []
    start = 0
    limit = 100

    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"{_group_url()}/collections",
                headers=_get_headers(),
                params={"limit": limit, "start": start, "sort": "title"},
            )
            resp.raise_for_status()

            batch = resp.json()
            for item in batch:
                data = item.get("data", {})
                meta = item.get("meta", {})
                all_collections.append({
                    "key": data.get("key", ""),
                    "name": data.get("name", ""),
                    "parent_key": data.get("parentCollection") or None,
                    "num_items": meta.get("numItems", 0),
                })

            if len(batch) < limit:
                break
            start += limit

    logger.info(f"Fetched {len(all_collections)} Zotero collections")
    return all_collections


async def build_collection_tree() -> list[dict]:
    """Build collection list with full hierarchical paths.

    Returns flat list of collections, each enriched with:
    - full_path: "parent / child / grandchild"
    - depth: nesting level (1 = top-level)
    - children_keys: list of direct child collection keys
    """
    collections = await list_collections()
    by_key = {c["key"]: c for c in collections}

    def _compute_path(col: dict) -> str:
        parts = []
        current = col
        visited = set()
        while current:
            if current["key"] in visited:
                break
            visited.add(current["key"])
            parts.insert(0, current["name"])
            parent_key = current.get("parent_key")
            current = by_key.get(parent_key) if parent_key else None
        return " / ".join(parts)

    for c in collections:
        c["full_path"] = _compute_path(c)
        c["depth"] = c["full_path"].count(" / ") + 1
        c["children_keys"] = []

    for c in collections:
        if c["parent_key"] and c["parent_key"] in by_key:
            by_key[c["parent_key"]]["children_keys"].append(c["key"])

    return collections


# ---------------------------------------------------------------------------
# Item operations
# ---------------------------------------------------------------------------

async def list_collection_items(collection_key: str) -> list[dict]:
    """List items in a Zotero collection (paginated, excludes attachments)."""
    all_items = []
    start = 0
    limit = 100

    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"{_group_url()}/collections/{collection_key}/items",
                headers=_get_headers(),
                params={
                    "limit": limit,
                    "start": start,
                    "sort": "title",
                    "itemType": "-attachment",
                },
            )
            resp.raise_for_status()

            batch = resp.json()
            for item in batch:
                data = item.get("data", {})
                all_items.append(_parse_item(data))

            if len(batch) < limit:
                break
            start += limit

    return all_items


async def list_collection_items_with_pdfs(collection_key: str) -> list[dict]:
    """List items in a collection, checking each for PDF attachments.

    More expensive than list_collection_items (N+1 API calls) but returns
    accurate has_pdf status. Use for upload pipelines.
    """
    items = await list_collection_items(collection_key)
    enriched = []

    async with httpx.AsyncClient() as client:
        for item in items:
            try:
                children_resp = await client.get(
                    f"{_group_url()}/items/{item['key']}/children",
                    headers=_get_headers(),
                )
                children_resp.raise_for_status()

                for child in children_resp.json():
                    child_data = child.get("data", {})
                    if child_data.get("contentType") == "application/pdf":
                        item["has_pdf"] = True
                        item["pdf_filename"] = child_data.get("filename", "")
                        item["pdf_key"] = child_data.get("key", "")
                        break
            except Exception as e:
                logger.warning(f"Failed to check children for {item['key']}: {e}")

            enriched.append(item)

    pdf_count = sum(1 for i in enriched if i.get("has_pdf"))
    logger.info(
        f"Collection {collection_key}: {len(enriched)} items, {pdf_count} with PDFs"
    )
    return enriched


async def get_item_details(item_key: str) -> dict:
    """Get detailed item metadata including PDF attachment info."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_group_url()}/items/{item_key}",
            headers=_get_headers(),
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        result = _parse_item(data)

        children_resp = await client.get(
            f"{_group_url()}/items/{item_key}/children",
            headers=_get_headers(),
        )
        children_resp.raise_for_status()

        for child in children_resp.json():
            child_data = child.get("data", {})
            if child_data.get("contentType") == "application/pdf":
                result["has_pdf"] = True
                result["pdf_filename"] = child_data.get("filename", "")
                result["pdf_key"] = child_data.get("key", "")
                break

        return result


async def download_pdf(item_key: str, dest_path: str):
    """Download a PDF from Zotero to a local file."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        children_resp = await client.get(
            f"{_group_url()}/items/{item_key}/children",
            headers=_get_headers(),
        )
        children_resp.raise_for_status()

        pdf_key = None
        for child in children_resp.json():
            child_data = child.get("data", {})
            if child_data.get("contentType") == "application/pdf":
                pdf_key = child_data.get("key")
                break

        if not pdf_key:
            raise ValueError(f"No PDF attachment found for item {item_key}")

        logger.info(f"Downloading PDF {pdf_key} for item {item_key}")
        file_resp = await client.get(
            f"{_group_url()}/items/{pdf_key}/file",
            headers=_get_headers(),
            timeout=120.0,
        )
        file_resp.raise_for_status()

        with open(dest_path, "wb") as f:
            f.write(file_resp.content)

        logger.info(f"Downloaded PDF to {dest_path} ({len(file_resp.content)} bytes)")


def _parse_item(data: dict) -> dict:
    """Parse a Zotero item data dict into our schema."""
    creators = []
    for c in data.get("creators", []):
        name = c.get("name") or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        if name:
            creators.append(name)

    return {
        "key": data.get("key", ""),
        "title": data.get("title", ""),
        "item_type": data.get("itemType", ""),
        "creators": creators,
        "date": data.get("date", ""),
        "has_pdf": False,
        "pdf_filename": None,
    }
