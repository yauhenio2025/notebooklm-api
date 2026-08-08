"""Application configuration from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = Field(
        default="postgresql://localhost:5432/notebooklm",
        description="PostgreSQL connection string",
    )

    # NotebookLM auth (master-token profile)
    # The library resolves the profile dir from NOTEBOOKLM_HOME / NOTEBOOKLM_PROFILE.
    # master_token_file points at a read-only secret file (Render: /etc/secrets/...)
    # that is seeded into the writable profile dir at client startup.
    master_token_file: str = Field(
        default="",
        description=(
            "Path to master_token.json secret file to seed the auth profile from"
        ),
    )

    # Zotero
    zotero_api_key: str = Field(
        default="",
        description="Zotero API key",
    )
    zotero_group_id: str = Field(
        default="5579237",
        description="Zotero group library ID",
    )

    # Anthropic (for natural language orchestration)
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key for Claude-powered intent parsing",
    )

    # Consumer auth. This protects the wrapper API and is deliberately
    # independent from the Google master token above.
    notebooklm_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Consumer credential required in the X-API-Key header",
    )

    # Browser access is disabled by default. Configure a comma-separated list
    # only when a trusted browser origin genuinely needs direct API access.
    cors_allowed_origins: str = Field(
        default="",
        description="Comma-separated browser origins allowed by CORS",
    )

    # App
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    notebooklm_query_timeout_seconds: int = Field(
        default=1200,
        ge=60,
        le=3600,
        description="Aggregate timeout for one NotebookLM batch query",
    )
    notebooklm_chat_frame_max_bytes: int = Field(
        default=192 * MEBIBYTE,
        ge=16 * MEBIBYTE,
        le=256 * MEBIBYTE,
        description=(
            "Raw safety limit for one retained NotebookLM chat protocol frame."
        ),
    )
    notebooklm_chat_answer_max_bytes: int = Field(
        default=4 * MEBIBYTE,
        ge=64 * 1024,
        le=16 * MEBIBYTE,
        description=(
            "Maximum UTF-8 size of the final prose answer, excluding citations."
        ),
    )
    notebooklm_chat_citation_max_bytes: int = Field(
        default=64 * MEBIBYTE,
        ge=MEBIBYTE,
        le=128 * MEBIBYTE,
        description=(
            "Maximum total UTF-8 size of retained citation passages, separate "
            "from the prose answer."
        ),
    )
    notebooklm_chat_wire_max_bytes: int = Field(
        default=GIBIBYTE,
        ge=256 * MEBIBYTE,
        le=2 * GIBIBYTE,
        description=(
            "Runaway safety ceiling for all decoded bytes received across one "
            "NotebookLM chat stream; this is transport traffic, not answer size."
        ),
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def validate_chat_stream_limits(self) -> "Settings":
        if (
            self.notebooklm_chat_wire_max_bytes
            < self.notebooklm_chat_frame_max_bytes
        ):
            raise ValueError(
                "NOTEBOOKLM_CHAT_WIRE_MAX_BYTES must be at least "
                "NOTEBOOKLM_CHAT_FRAME_MAX_BYTES"
            )
        if self.notebooklm_chat_answer_max_bytes > self.notebooklm_chat_frame_max_bytes:
            raise ValueError(
                "NOTEBOOKLM_CHAT_ANSWER_MAX_BYTES must not exceed "
                "NOTEBOOKLM_CHAT_FRAME_MAX_BYTES"
            )
        if (
            self.notebooklm_chat_citation_max_bytes
            > self.notebooklm_chat_frame_max_bytes
        ):
            raise ValueError(
                "NOTEBOOKLM_CHAT_CITATION_MAX_BYTES must not exceed "
                "NOTEBOOKLM_CHAT_FRAME_MAX_BYTES"
            )
        return self

    @property
    def async_database_url(self) -> str:
        """Ensure the database URL uses asyncpg driver."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        """Return a normalized, explicitly bounded CORS allow-list."""
        origins = [
            origin.strip().rstrip("/")
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]
        if "*" in origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS may not contain '*'; "
                "list trusted origins explicitly"
            )
        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
