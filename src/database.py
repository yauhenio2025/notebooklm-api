"""SQLAlchemy async database setup."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """Dependency that yields an async database session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables and run migrations. Called during app startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Run idempotent column migrations for existing databases
    migrations = [
        "ALTER TABLE sources ADD COLUMN IF NOT EXISTS authors VARCHAR",
        "ALTER TABLE sources ADD COLUMN IF NOT EXISTS publication_date VARCHAR",
        "ALTER TABLE sources ADD COLUMN IF NOT EXISTS item_type VARCHAR",
        "ALTER TABLE citations ADD COLUMN IF NOT EXISTS source_authors VARCHAR",
        "ALTER TABLE citations ADD COLUMN IF NOT EXISTS source_date VARCHAR",
    ]
    async with engine.begin() as conn:
        from sqlalchemy import text
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.debug(f"Migration skipped (may already exist): {e}")

    logger.info("Database tables created/verified with migrations")


async def close_db():
    """Dispose of the engine. Called during app shutdown."""
    await engine.dispose()
    logger.info("Database connections closed")
