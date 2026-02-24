"""Natural language notebook builder using Claude API for intent parsing.

Pipeline:
1. Fetch Zotero collection tree
2. Use Claude to parse natural language instruction → structured intent
3. Resolve Zotero collection
4. Create NotebookLM notebook
5. Upload PDFs from collection

Uses claude-haiku-4-5 for fast, cheap intent parsing.
"""

import json
import logging
import time

from src.config import get_settings

logger = logging.getLogger(__name__)


async def parse_notebook_instruction(
    instruction: str,
    collections: list[dict],
) -> dict:
    """Use Claude to parse a natural language instruction into structured intent.

    Args:
        instruction: Natural language like "Make a notebook from the O'Neill papers in jan"
        collections: Full collection tree with paths

    Returns:
        {
            "collection_key": "ABC123",
            "collection_path": "_2026_jan / o'neill",
            "notebook_title": "O'Neill - Jan 2026",
            "confidence": 0.95,
            "reasoning": "Matched 'O'Neill' to collection '_2026_jan / o'neill' (21 items)"
        }
    """
    from anthropic import AsyncAnthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not configured — cannot parse natural language instructions"
        )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Format collections for context — include path, key, and item count
    collection_lines = []
    for c in collections:
        line = f"key={c['key']} | path=\"{c['full_path']}\" | items={c['num_items']}"
        if c.get("children_keys"):
            line += f" | has {len(c['children_keys'])} sub-collections"
        collection_lines.append(line)
    collection_list = "\n".join(collection_lines)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[
            {
                "role": "user",
                "content": f"""You are a Zotero collection resolver. Parse the user's instruction to identify which Zotero collection they want, and suggest a notebook title.

USER INSTRUCTION: "{instruction}"

AVAILABLE COLLECTIONS (362 total):
{collection_list}

RULES:
- Match the user's description to the most likely collection
- Consider fuzzy name matching (e.g., "o'neill" matches "o'neill", "castoriadis" matches "castoriadis_primary")
- If the user mentions a month/date, try to match it to collection path prefixes like "_2026_jan", "_2025_nov", etc.
- If multiple collections match (e.g., castoriadis_primary and castoriadis_secondary), prefer the one with "primary" unless the user specifies otherwise, or if there's a standalone collection without the suffix
- For notebook title, create something concise and descriptive like "O'Neill - Jan 2026" or "Castoriadis Primary Sources"
- If the user explicitly provides a title, use that instead
- If you cannot confidently identify a single collection, set confidence low and explain in reasoning

Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
{{
    "collection_key": "the key of the best matching collection or null",
    "collection_path": "full path of matched collection",
    "notebook_title": "suggested notebook title",
    "confidence": 0.0 to 1.0,
    "reasoning": "brief explanation of match logic",
    "alternatives": [
        {{"key": "...", "path": "...", "reason": "also a possible match"}}
    ]
}}""",
            }
        ],
    )

    text = response.content[0].text.strip()
    # Handle markdown code blocks just in case
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    result = json.loads(text)
    logger.info(
        f"Intent parsed: collection={result.get('collection_path')} "
        f"confidence={result.get('confidence')} "
        f"reasoning={result.get('reasoning')}"
    )
    return result


async def build_notebook_from_instruction(instruction: str, db) -> dict:
    """Full pipeline: parse instruction → find collection → create notebook → upload PDFs.

    Args:
        instruction: Natural language instruction
        db: AsyncSession for database operations

    Returns:
        Structured result with status, notebook info, and uploaded sources
    """
    from src.services.notebook_service import create_notebook
    from src.services.source_service import upload_from_zotero
    from src.services.zotero_service import (
        build_collection_tree,
        list_collection_items_with_pdfs,
    )

    pipeline_start = time.time()
    steps = []

    # Step 1: Fetch collection tree
    step_start = time.time()
    logger.info(f"[build-notebook] Step 1: Fetching Zotero collection tree")
    collections = await build_collection_tree()
    steps.append({
        "step": "fetch_collection_tree",
        "duration_s": round(time.time() - step_start, 2),
        "collections_found": len(collections),
    })

    # Step 2: Parse instruction with Claude
    step_start = time.time()
    logger.info(f"[build-notebook] Step 2: Parsing instruction with Claude")
    try:
        intent = await parse_notebook_instruction(instruction, collections)
    except Exception as e:
        logger.error(f"[build-notebook] Intent parsing failed: {e}")
        return {
            "status": "failed",
            "error": f"Failed to parse instruction: {e}",
            "steps": steps,
        }

    steps.append({
        "step": "parse_instruction",
        "duration_s": round(time.time() - step_start, 2),
        "intent": intent,
    })

    # Check confidence
    if not intent.get("collection_key") or intent.get("confidence", 0) < 0.3:
        return {
            "status": "failed",
            "error": "Could not confidently match instruction to a Zotero collection",
            "intent": intent,
            "suggestion": "Try being more specific about the collection path. "
            "Available paths include: "
            + ", ".join(
                c["full_path"]
                for c in sorted(collections, key=lambda x: x["num_items"], reverse=True)[:20]
            ),
            "steps": steps,
        }

    # Step 3: Get items with PDFs from the matched collection
    step_start = time.time()
    collection_key = intent["collection_key"]
    logger.info(
        f"[build-notebook] Step 3: Listing items with PDFs in {intent.get('collection_path')}"
    )
    items = await list_collection_items_with_pdfs(collection_key)
    items_with_pdfs = [i for i in items if i.get("has_pdf")]

    steps.append({
        "step": "list_collection_items",
        "duration_s": round(time.time() - step_start, 2),
        "total_items": len(items),
        "items_with_pdfs": len(items_with_pdfs),
    })

    if not items_with_pdfs:
        return {
            "status": "failed",
            "error": f"No PDFs found in collection '{intent.get('collection_path')}'",
            "intent": intent,
            "items_found": len(items),
            "steps": steps,
        }

    # Step 4: Create NotebookLM notebook (with auto-retry on auth failure)
    step_start = time.time()
    notebook_title = intent.get("notebook_title", "Untitled Notebook")
    logger.info(f"[build-notebook] Step 4: Creating notebook '{notebook_title}'")

    try:
        notebook = await create_notebook(db, notebook_title)
    except RuntimeError as e:
        if "not available" in str(e).lower():
            # Auth likely expired — attempt auto-refresh
            logger.warning(
                f"[build-notebook] Notebook creation failed with auth error: {e}. "
                "Attempting auto-refresh from droplet..."
            )
            try:
                from src.services.auth_service import full_auth_refresh

                refresh_result = await full_auth_refresh()
                logger.info(
                    f"[build-notebook] Auth auto-refresh succeeded "
                    f"({refresh_result['extraction']['cookie_count']} cookies, "
                    f"{refresh_result['total_duration_s']}s)"
                )
                steps.append({
                    "step": "auth_auto_refresh",
                    "duration_s": refresh_result["total_duration_s"],
                    "cookie_count": refresh_result["extraction"]["cookie_count"],
                })

                # Retry notebook creation once
                logger.info(f"[build-notebook] Retrying notebook creation after auth refresh")
                notebook = await create_notebook(db, notebook_title)

            except Exception as refresh_err:
                logger.error(
                    f"[build-notebook] Auth auto-refresh failed: {refresh_err}"
                )
                return {
                    "status": "failed",
                    "error": (
                        f"Notebook creation failed (auth expired), "
                        f"and auto-refresh also failed: {refresh_err}"
                    ),
                    "original_error": str(e),
                    "intent": intent,
                    "steps": steps,
                }
        else:
            logger.error(f"[build-notebook] Notebook creation failed: {e}")
            return {
                "status": "failed",
                "error": f"Failed to create notebook: {e}",
                "intent": intent,
                "steps": steps,
            }
    except Exception as e:
        logger.error(f"[build-notebook] Notebook creation failed: {e}")
        return {
            "status": "failed",
            "error": f"Failed to create notebook: {e}",
            "intent": intent,
            "steps": steps,
        }

    steps.append({
        "step": "create_notebook",
        "duration_s": round(time.time() - step_start, 2),
        "notebook_id": notebook.id,
        "notebook_title": notebook_title,
    })

    # Step 5: Upload PDFs from Zotero to notebook
    step_start = time.time()
    item_keys = [i["key"] for i in items_with_pdfs]
    logger.info(
        f"[build-notebook] Step 5: Uploading {len(item_keys)} PDFs to notebook {notebook.id}"
    )

    try:
        sources = await upload_from_zotero(db, notebook.id, item_keys)
    except Exception as e:
        logger.error(f"[build-notebook] Upload failed (partial may have succeeded): {e}")
        sources = []

    steps.append({
        "step": "upload_sources",
        "duration_s": round(time.time() - step_start, 2),
        "attempted": len(item_keys),
        "uploaded": len(sources),
    })

    total_duration = round(time.time() - pipeline_start, 2)

    return {
        "status": "success",
        "notebook_id": notebook.id,
        "notebook_title": notebook_title,
        "collection_path": intent.get("collection_path"),
        "collection_key": collection_key,
        "sources_uploaded": len(sources),
        "sources": [
            {
                "title": s.title,
                "file_name": s.file_name,
                "status": s.status,
                "zotero_key": s.zotero_key,
            }
            for s in sources
        ],
        "intent": intent,
        "duration_s": total_duration,
        "steps": steps,
    }
