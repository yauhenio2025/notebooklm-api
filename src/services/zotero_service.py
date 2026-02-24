"""Zotero API client for browsing collections and downloading PDFs."""

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


async def list_collections() -> list[dict]:
    """List all collections in the Zotero group library."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_group_url()}/collections",
            headers=_get_headers(),
            params={"limit": 100, "sort": "title"},
        )
        resp.raise_for_status()
        collections = []
        for item in resp.json():
            data = item.get("data", {})
            meta = item.get("meta", {})
            collections.append({
                "key": data.get("key", ""),
                "name": data.get("name", ""),
                "parent_key": data.get("parentCollection") or None,
                "num_items": meta.get("numItems", 0),
            })
        return collections


async def list_collection_items(collection_key: str) -> list[dict]:
    """List items in a Zotero collection."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_group_url()}/collections/{collection_key}/items",
            headers=_get_headers(),
            params={"limit": 100, "sort": "title", "itemType": "-attachment"},
        )
        resp.raise_for_status()
        items = []
        for item in resp.json():
            data = item.get("data", {})
            items.append(_parse_item(data))
        return items


async def get_item_details(item_key: str) -> dict:
    """Get detailed item metadata including PDF attachment info."""
    async with httpx.AsyncClient() as client:
        # Get main item
        resp = await client.get(
            f"{_group_url()}/items/{item_key}",
            headers=_get_headers(),
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        result = _parse_item(data)

        # Get children (attachments)
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
    """Download a PDF from Zotero to a local file.

    First finds the PDF attachment child item, then downloads the file.
    """
    settings = get_settings()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Find PDF attachment
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

        # Download the PDF file content
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
