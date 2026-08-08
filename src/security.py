"""Consumer authentication for the NotebookLM HTTP API.

Google's master token authenticates this service *to NotebookLM*.  It must not
be confused with the independent consumer key that authenticates Ganrl (or any
other caller) *to this service*.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from src.config import Settings, get_settings

API_KEY_HEADER_NAME = "X-API-Key"

consumer_api_key_header = APIKeyHeader(
    name=API_KEY_HEADER_NAME,
    scheme_name="NotebookLMConsumerKey",
    description=(
        "Consumer credential for this wrapper service. This is separate from "
        "the Google master token used by the service itself."
    ),
    auto_error=False,
)


def _fixed_length_digest(value: str) -> bytes:
    """Normalize arbitrary key lengths before the constant-time comparison."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def api_keys_match(supplied: str, expected: str) -> bool:
    """Compare consumer credentials without a timing-sensitive equality check."""
    return secrets.compare_digest(
        _fixed_length_digest(supplied),
        _fixed_length_digest(expected),
    )


async def require_consumer_api_key(
    supplied_key: Annotated[str | None, Security(consumer_api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Require the configured consumer key, failing closed when it is absent."""
    expected = settings.notebooklm_api_key.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Consumer API authentication is not configured",
        )

    if not api_keys_match(supplied_key or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"A valid {API_KEY_HEADER_NAME} header is required",
        )
