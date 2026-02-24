"""Application configuration from environment variables."""

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://notebook_lm_db_user:2DPQF8i9MA8xuOk5FyEkCkfXnWYTtg5L@dpg-d6ekteruibrs73df6au0-a.singapore-postgres.render.com/notebook_lm_db",
        description="PostgreSQL connection string",
    )

    # NotebookLM auth
    notebooklm_auth_json: str = Field(
        default="",
        description="JSON string of Google auth cookies from notebooklm-py login",
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

    # DigitalOcean droplet for auth refresh
    droplet_host: str = Field(
        default="207.154.192.181",
        description="DigitalOcean droplet IP running Chrome with NotebookLM session",
    )
    droplet_ssh_key: str = Field(
        default="",
        description="SSH private key for root@droplet (PEM format, as string)",
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
