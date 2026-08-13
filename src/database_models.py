# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (SQLAlchemy Models)
# Role: PostgreSQL ORM models for production-scale persistence.

import uuid
from datetime import UTC, datetime


def _sa_utcnow():
    """Timezone-aware UTC now for SQLAlchemy default/onupdate."""
    return datetime.now(tz=UTC)
from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config_settings import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


class OpportunityModel(Base):
    """
    PostgreSQL Opportunity Entity.
    Optimized for concurrent access and complex queries.
    """
    __tablename__ = "opportunities"
    
    # Primary key using URL (natural key for deduplication)
    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    
    # Source tracking
    source: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    
    # Core opportunity data
    title: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    buyer: Mapped[str | None] = mapped_column(String(255), index=True)
    country: Mapped[str | None] = mapped_column(String(100))
    
    # Dates
    publication_date: Mapped[str | None] = mapped_column(String(50))
    deadline: Mapped[str | None] = mapped_column(String(50))
    
    # Financial
    estimated_budget: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(10), default="EUR")
    normalized_budget_eur: Mapped[float | None] = mapped_column(Float)
    
    # Status and classification
    status: Mapped[str] = mapped_column(String(50), default="OPEN", index=True)
    classification: Mapped[str | None] = mapped_column(String(100))
    
    # URLs
    url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    
    # Enrichment
    summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    
    # Metadata
    change_type: Mapped[str] = mapped_column(String(20), default="NEW")
    duplicate_group_id: Mapped[str | None] = mapped_column(String(100), index=True)
    
    # Timestamps
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=_sa_utcnow,
        index=True
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=_sa_utcnow,
        onupdate=_sa_utcnow,
        index=True
    )
    
    # Relationships
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True
    )
    run: Mapped[Optional["RunModel"]] = relationship(back_populates="opportunities")
    
    # Composite indexes for common query patterns
    __table_args__ = (
        Index('idx_opportunities_source_lastseen', 'source', 'last_seen'),
        Index('idx_opportunities_buyer_deadline', 'buyer', 'deadline'),
        Index('idx_opportunities_classification_status', 'classification', 'status'),
    )


class RunModel(Base):
    """
    Scraping execution run tracking.
    """
    __tablename__ = "runs"
    
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=_sa_utcnow,
        index=True
    )
    source: Mapped[str] = mapped_column(String(100), index=True)
    
    # Counters
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    
    # Metadata
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(50), default="running")
    error_message: Mapped[str | None] = mapped_column(Text)
    
    # Relationships
    opportunities: Mapped[list["OpportunityModel"]] = relationship(back_populates="run")


class DeadLetterModel(Base):
    """
    Dead Letter Queue persistence for failed jobs.
    """
    __tablename__ = "dead_letters"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Original job data
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(512))
    target_site: Mapped[str] = mapped_column(String(100))
    
    # Error details
    error_message: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(50))
    
    # Retry tracking
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=_sa_utcnow
    )
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    
    # Raw payload for replay
    payload: Mapped[dict] = mapped_column(JSON)
    
    # Status
    status: Mapped[str] = mapped_column(
        String(50), 
        default="pending",
        index=True
    )


class EventLogModel(Base):
    """
    Event sourcing log for audit trail.
    """
    __tablename__ = "event_logs"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[str] = mapped_column(String(255), index=True)
    
    # Event data
    payload: Mapped[dict] = mapped_column(JSON)
    # Declarative reserves the `metadata` attribute for Base.metadata, so the
    # attribute is renamed while the database column keeps its original name.
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=_sa_utcnow,
        index=True
    )
    
    # Source
    service_name: Mapped[str] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


# Database Engine & Session Factory
engine = create_async_engine(
    str(settings.database.url),
    pool_size=settings.database.pool_size,
    max_overflow=settings.database.max_overflow,
    pool_pre_ping=settings.database.pool_pre_ping,
    echo=settings.database.echo,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI to get DB sessions."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Close database connections."""
    await engine.dispose()
