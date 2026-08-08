"""Application configuration from environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://notebook_lm_db_user:2DPQF8i9MA8xuOk5FyEkCkfXnWYTtg5L@dpg-d6ekteruibrs73df6au0-a.singapore-postgres.render.com/notebook_lm_db",
        description="PostgreSQL connection string",
    )

    # NotebookLM auth (master-token profile)
    # The library resolves the profile dir from NOTEBOOKLM_HOME / NOTEBOOKLM_PROFILE.
    # master_token_file points at a read-only secret file (Render: /etc/secrets/...)
    # that is seeded into the writable profile dir at client startup.
    master_token_file: str = Field(
        default="",
        description="Path to master_token.json secret file to seed the auth profile from",
    )

    # Zotero
    zotero_api_key: str = Field(
        default="ZORwvJIL1PLXLteN0heAJrcA",
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
