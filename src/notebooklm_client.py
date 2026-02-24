"""Singleton wrapper around notebooklm-py client.

Initializes lazily from NOTEBOOKLM_AUTH_JSON environment variable.
The JSON is written to a temporary file and loaded via NotebookLMClient.from_storage().
"""

import json
import logging
import tempfile
from pathlib import Path

from src.config import get_settings

logger = logging.getLogger(__name__)

_client = None
_client_initialized = False


def get_notebooklm_client():
    """Get or create the singleton NotebookLM client.

    Returns None if auth is not configured or initialization fails.
    """
    global _client, _client_initialized

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
        storage_file = storage_dir / "storage_state.json"
        storage_file.write_text(json.dumps(auth_data))

        _client = NotebookLMClient.from_storage(str(storage_file))
        _client_initialized = True
        logger.info("NotebookLM client initialized successfully")
        return _client

    except Exception as e:
        logger.error(f"Failed to initialize NotebookLM client: {e}")
        _client_initialized = True
        _client = None
        return None


def reset_client():
    """Force re-initialization on next access (e.g., after auth refresh)."""
    global _client, _client_initialized
    _client = None
    _client_initialized = False
    logger.info("NotebookLM client reset - will reinitialize on next access")
