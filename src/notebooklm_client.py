"""Singleton wrapper around notebooklm-py async client.

Initializes lazily from NOTEBOOKLM_AUTH_JSON environment variable.
The JSON is written to a temporary file and loaded via NotebookLMClient.from_storage().

All notebooklm-py methods are async coroutines. The client is used as an async
context manager, so we keep a persistent instance alive for the process lifetime.
"""

import json
import logging
import tempfile
from pathlib import Path

from src.config import get_settings

logger = logging.getLogger(__name__)

_client = None
_client_initialized = False
_storage_file = None


async def get_notebooklm_client():
    """Get or create the singleton NotebookLM client.

    Returns None if auth is not configured or initialization fails.
    Must be called from an async context.
    """
    global _client, _client_initialized, _storage_file

    if _client_initialized:
        return _client

    settings = get_settings()
    if not settings.notebooklm_auth_json:
        logger.warning("NOTEBOOKLM_AUTH_JSON not set - client unavailable")
        _client_initialized = True
        return None

    try:
        from notebooklm import NotebookLMClient

        # Write auth JSON to a temp file that persists for the process lifetime
        auth_data = json.loads(settings.notebooklm_auth_json)
        storage_dir = Path(tempfile.mkdtemp(prefix="notebooklm_"))
        _storage_file = storage_dir / "storage_state.json"
        _storage_file.write_text(json.dumps(auth_data))

        # from_storage() is async and returns a client ready to use
        _client = await NotebookLMClient.from_storage(str(_storage_file))
        # Enter the async context manager to keep the session alive
        await _client.__aenter__()

        _client_initialized = True
        logger.info("NotebookLM client initialized successfully")
        return _client

    except Exception as e:
        logger.error(f"Failed to initialize NotebookLM client: {e}")
        _client_initialized = True
        _client = None
        return None


async def close_client():
    """Close the client's async session. Called during app shutdown."""
    global _client, _client_initialized
    if _client:
        try:
            await _client.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"Error closing NotebookLM client: {e}")
    _client = None
    _client_initialized = False
    logger.info("NotebookLM client closed")


async def reset_client():
    """Force re-initialization on next access (e.g., after auth refresh)."""
    await close_client()
    logger.info("NotebookLM client reset - will reinitialize on next access")
