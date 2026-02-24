"""Auth refresh service — extracts fresh Google cookies from the DigitalOcean droplet.

The droplet runs a persistent Chrome instance with an active NotebookLM session.
Playwright CDP on port 9444 exposes the browser's storage state (cookies + localStorage).

Flow:
1. SSH to droplet as root
2. Run Playwright CDP extraction script
3. Parse the storage state JSON
4. Reset the NotebookLM client singleton with fresh cookies
"""

import asyncio
import json
import logging
import time

from src.config import get_settings

logger = logging.getLogger(__name__)

# Cookie extraction script to run on the droplet via SSH
_EXTRACTION_SCRIPT = '''python3 -c "
import asyncio, json
async def extract():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://127.0.0.1:9444')
        context = browser.contexts[0]
        state = await context.storage_state()
        print(json.dumps(state))
asyncio.run(extract())
"'''


async def refresh_auth_from_droplet() -> dict:
    """SSH to the DigitalOcean droplet and extract fresh cookies via Playwright CDP.

    Returns:
        dict with keys:
            - auth_json: str — the raw JSON string of storage state
            - cookie_count: int — number of cookies extracted
            - origin_count: int — number of origins in localStorage
            - duration_s: float — time taken for the operation

    Raises:
        RuntimeError: if SSH key not configured, SSH connection fails, or extraction fails
    """
    import asyncssh

    settings = get_settings()

    if not settings.droplet_ssh_key:
        raise RuntimeError(
            "DROPLET_SSH_KEY not configured — cannot SSH to droplet for auth refresh"
        )

    start_time = time.time()
    host = settings.droplet_host

    logger.info(f"[auth-refresh] Connecting to droplet at {host}")

    # Parse the private key from the env var string
    try:
        private_key = asyncssh.import_private_key(settings.droplet_ssh_key)
    except Exception as e:
        raise RuntimeError(f"Failed to parse DROPLET_SSH_KEY: {e}") from e

    # Connect and run the extraction script
    try:
        async with asyncssh.connect(
            host,
            username="root",
            client_keys=[private_key],
            known_hosts=None,  # Skip host key verification (private infra)
            connect_timeout=15,
        ) as conn:
            logger.info("[auth-refresh] SSH connected, running cookie extraction")
            result = await asyncio.wait_for(
                conn.run(_EXTRACTION_SCRIPT, check=True),
                timeout=30,
            )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"SSH command timed out after 30s — droplet {host} may be unresponsive"
        )
    except asyncssh.Error as e:
        raise RuntimeError(f"SSH connection to {host} failed: {e}") from e

    # Parse the output
    stdout = result.stdout.strip()
    if not stdout:
        stderr_msg = result.stderr.strip() if result.stderr else "no output"
        raise RuntimeError(
            f"Cookie extraction returned empty output. stderr: {stderr_msg}"
        )

    try:
        storage_state = json.loads(stdout)
    except json.JSONDecodeError as e:
        # Log first 500 chars of output for debugging
        logger.error(f"[auth-refresh] Non-JSON output (first 500 chars): {stdout[:500]}")
        raise RuntimeError(f"Cookie extraction returned invalid JSON: {e}") from e

    cookie_count = len(storage_state.get("cookies", []))
    origin_count = len(storage_state.get("origins", []))
    duration = round(time.time() - start_time, 2)

    logger.info(
        f"[auth-refresh] Extracted {cookie_count} cookies, "
        f"{origin_count} origins in {duration}s"
    )

    return {
        "auth_json": stdout,
        "cookie_count": cookie_count,
        "origin_count": origin_count,
        "duration_s": duration,
    }


async def update_notebooklm_auth(auth_json: str) -> dict:
    """Reset the NotebookLM client singleton with fresh auth cookies.

    This updates the in-memory client — it does NOT persist to the Render env var.
    To persist, update NOTEBOOKLM_AUTH_JSON via Render dashboard or API.

    Args:
        auth_json: JSON string of storage state (cookies + localStorage)

    Returns:
        dict with status info
    """
    import os

    from src.notebooklm_client import get_notebooklm_client, reset_client

    # Validate the JSON before applying
    try:
        parsed = json.loads(auth_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid auth JSON: {e}") from e

    cookie_count = len(parsed.get("cookies", []))
    if cookie_count == 0:
        raise RuntimeError("Auth JSON contains no cookies — refusing to apply")

    # Update the settings in-memory so the client picks up the new auth
    # We modify os.environ so that get_settings() sees the new value on next call
    os.environ["NOTEBOOKLM_AUTH_JSON"] = auth_json

    # Clear the cached settings so it re-reads from env
    from src.config import get_settings
    get_settings.cache_clear()

    # Reset the client singleton — next access will reinitialize with new cookies
    await reset_client()

    # Verify the new client works by initializing it
    client = await get_notebooklm_client()
    if client is None:
        raise RuntimeError(
            "Client failed to initialize with new auth — cookies may be expired or invalid"
        )

    logger.info(
        f"[auth-refresh] NotebookLM client reset with {cookie_count} fresh cookies"
    )

    return {
        "status": "success",
        "cookie_count": cookie_count,
        "client_active": client is not None,
    }


async def full_auth_refresh() -> dict:
    """End-to-end auth refresh: extract from droplet + reset client.

    Returns:
        dict with extraction and update info
    """
    start_time = time.time()

    # Step 1: Extract fresh cookies from droplet
    extraction = await refresh_auth_from_droplet()

    # Step 2: Reset the NotebookLM client with fresh cookies
    update = await update_notebooklm_auth(extraction["auth_json"])

    total_duration = round(time.time() - start_time, 2)

    logger.info(f"[auth-refresh] Full refresh completed in {total_duration}s")

    return {
        "status": "success",
        "extraction": {
            "cookie_count": extraction["cookie_count"],
            "origin_count": extraction["origin_count"],
            "ssh_duration_s": extraction["duration_s"],
        },
        "client_reset": update["client_active"],
        "total_duration_s": total_duration,
    }
