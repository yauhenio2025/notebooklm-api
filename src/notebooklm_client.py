"""Singleton wrapper around notebooklm-py async client.

Auth model (master-token, headless):
- The notebooklm-py auth profile lives at $NOTEBOOKLM_HOME/profiles/<profile>/
  (library default ~/.notebooklm). The profile dir must be WRITABLE — the client
  rotates cookies into storage_state.json during normal operation.
- master_token.json in the profile dir is the durable credential. When present,
  an expired session re-mints fresh cookies in-process (library layer-4
  recovery) — no browser, no droplet, no manual refresh.
- On Render the durable credential arrives as a read-only secret file
  (MASTER_TOKEN_FILE, e.g. /etc/secrets/master_token.json). At startup we seed
  it into the profile dir, then mint storage_state.json from it if missing.

All notebooklm-py methods are async coroutines. The client is used as an async
context manager, so we keep a persistent instance alive for the process lifetime.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from src.config import get_settings

logger = logging.getLogger(__name__)

_client = None
_client_initialized = False
_init_lock = asyncio.Lock()


def _profile_paths() -> tuple[Path, Path]:
    """Resolve (storage_state.json, master_token.json) for the active profile."""
    from notebooklm import paths

    return Path(paths.get_storage_path()), Path(paths.get_master_token_path())


def seed_profile_from_secret() -> Path | None:
    """Copy the master-token secret file into the writable profile dir.

    Returns the profile's master_token.json path if a token is available
    (seeded now or already present), else None.
    """
    settings = get_settings()
    storage_path, master_token_path = _profile_paths()

    secret = Path(settings.master_token_file) if settings.master_token_file else None
    if secret and secret.exists():
        master_token_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(secret, master_token_path)
        os.chmod(master_token_path, 0o600)
        logger.info(f"Seeded master token into profile: {master_token_path.parent}")
    elif secret:
        logger.warning(f"MASTER_TOKEN_FILE set but not found: {secret}")

    return master_token_path if master_token_path.exists() else None


async def mint_storage_state() -> None:
    """Mint fresh cookies from the master token into storage_state.json.

    Uses the same no-prompt re-mint the CLI's --master-token-refresh runs.
    """
    from notebooklm.cli.services.login.master_token import refresh

    storage_path, master_token_path = _profile_paths()
    await refresh(storage_path=storage_path, master_token_path=master_token_path)
    logger.info(f"Minted fresh storage state at {storage_path}")


async def get_notebooklm_client():
    """Get or create the singleton NotebookLM client.

    Returns None if auth is not configured or initialization fails.
    Must be called from an async context.
    """
    global _client, _client_initialized

    if _client_initialized:
        return _client

    async with _init_lock:
        if _client_initialized:
            return _client

        try:
            from notebooklm import NotebookLMClient

            storage_path, _ = _profile_paths()
            master_token_path = seed_profile_from_secret()

            if master_token_path is None and not storage_path.exists():
                logger.warning(
                    "No master token and no storage state — client unavailable. "
                    "Set MASTER_TOKEN_FILE or run 'notebooklm login --master-token'."
                )
                _client_initialized = True
                return None

            # A master token can always mint a fresh session; do it at boot so
            # the client never starts on cold-dead cookies (ephemeral disk).
            if master_token_path is not None:
                await mint_storage_state()

            _client = await NotebookLMClient.from_storage(str(storage_path))
            # Enter the async context manager to keep the session alive
            await _client.__aenter__()

            _client_initialized = True
            logger.info(
                f"NotebookLM client initialized from {storage_path} "
                f"(master token: {'yes' if master_token_path else 'no'})"
            )
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
