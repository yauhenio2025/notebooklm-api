"""Auth refresh service — re-mints NotebookLM cookies from the master token.

The master token (master_token.json in the notebooklm-py profile dir) is a
durable Google credential that mints fresh web cookies on demand, headlessly.
The library already self-heals expired sessions in-process (layer-4 recovery);
this service is the explicit/manual path: re-mint + reset the client singleton.

Replaces the old DigitalOcean droplet SSH/CDP cookie extraction.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)


async def full_auth_refresh() -> dict:
    """Re-mint cookies from the master token and reset the client.

    Returns:
        dict with status, cookie_count, client_reset, total_duration_s

    Raises:
        RuntimeError: if no master token is available or the re-mint fails
    """
    from src.notebooklm_client import (
        get_notebooklm_client,
        mint_storage_state,
        reset_client,
        seed_profile_from_secret,
        _profile_paths,
    )

    start_time = time.time()

    master_token_path = seed_profile_from_secret()
    if master_token_path is None:
        raise RuntimeError(
            "No master token available — set MASTER_TOKEN_FILE or run "
            "'notebooklm login --master-token' for this profile"
        )

    try:
        await mint_storage_state()
    except Exception as e:
        raise RuntimeError(f"Master-token re-mint failed: {e}") from e

    storage_path, _ = _profile_paths()
    try:
        cookie_count = len(json.loads(storage_path.read_text()).get("cookies", []))
    except Exception:
        cookie_count = -1

    # Reset the client singleton — next access reinitializes with fresh cookies
    await reset_client()
    client = await get_notebooklm_client()
    if client is None:
        raise RuntimeError("Client failed to initialize after re-mint")

    total_duration = round(time.time() - start_time, 2)
    logger.info(
        f"[auth-refresh] Re-minted {cookie_count} cookies in {total_duration}s"
    )

    return {
        "status": "success",
        "cookie_count": cookie_count,
        "client_reset": True,
        "total_duration_s": total_duration,
    }
