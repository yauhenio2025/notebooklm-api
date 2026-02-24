"""SQLAlchemy ORM models."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Notebook(Base):
    __tablename__ = "notebooks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # Relationships
    sources: Mapped[list["Source"]] = relationship(
        back_populates="notebook", cascade="all, delete-orphan"
    )
    queries: Mapped[list["Query"]] = relationship(
        back_populates="notebook", cascade="all, delete-orphan"
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    notebook_id: Mapped[str] = mapped_column(
        String, ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, default="pdf")
    zotero_key: Mapped[str | None] = mapped_column(String, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ready")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    authors: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_date: Mapped[str | None] = mapped_column(String, nullable=True)
    item_type: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # Relationships
    notebook: Mapped["Notebook"] = relationship(back_populates="sources")


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notebook_id: Mapped[str] = mapped_column(
        String, ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    turn_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # Relationships
    notebook: Mapped["Notebook"] = relationship(back_populates="queries")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="query", cascade="all, delete-orphan"
    )


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("queries.id", ondelete="CASCADE"), nullable=False
    )
    citation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_title: Mapped[str | None] = mapped_column(String, nullable=True)
    cited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_char: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_char: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_authors: Mapped[str | None] = mapped_column(String, nullable=True)
    source_date: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    query: Mapped["Query"] = relationship(back_populates="citations")
