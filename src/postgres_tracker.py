# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (PostgreSQL Tracker)
# Role: Production-grade persistence with SQLAlchemy.

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import select, insert, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database_models import (
    async_session_maker, OpportunityModel, RunModel, 
    DeadLetterModel, EventLogModel, init_db, close_db
)
from src.domain.models import Opportunity, ScrapeJob
from src.config_settings import settings

logger = logging.getLogger("Spacescraper.PostgresTracker")


class PostgresTracker:
    """
    Spacescraper PostgreSQL State Auditor.
    Enterprise-grade persistence with connection pooling and async operations.
    """
    
    def __init__(self):
        self._initialized = False

    async def initialize(self):
        """Provision the intelligence schema."""
        if not self._initialized:
            await init_db()
            self._initialized = True
            logger.info("Spacescraper PostgreSQL tracker initialized")

    async def close(self):
        """Close database connections."""
        await close_db()
        self._initialized = False

    async def get_opportunity_by_id(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific opportunity snapshot for comparison."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(OpportunityModel).where(OpportunityModel.id == opportunity_id)
            )
            model = result.scalar_one_or_none()
            return self._model_to_dict(model) if model else None

    async def get_opportunity_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a opportunity by its external ID."""
        if not external_id:
            return None
        async with async_session_maker() as session:
            result = await session.execute(
                select(OpportunityModel).where(OpportunityModel.external_id == external_id)
            )
            model = result.scalar_one_or_none()
            return self._model_to_dict(model) if model else None

    async def find_similar_opportunities(
        self, 
        title: str, 
        buyer: Optional[str] = None, 
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Query for potential duplicates using PostgreSQL full-text search
        and similarity matching.
        """
        async with async_session_maker() as session:
            # Use ILIKE for case-insensitive pattern matching
            if buyer:
                result = await session.execute(
                    select(OpportunityModel)
                    .where(
                        (OpportunityModel.buyer == buyer) & 
                        (OpportunityModel.title.ilike(f"%{title[:30]}%"))
                    )
                    .order_by(OpportunityModel.last_seen.desc())
                    .limit(limit)
                )
            else:
                result = await session.execute(
                    select(OpportunityModel)
                    .where(OpportunityModel.title.ilike(f"%{title[:30]}%"))
                    .order_by(OpportunityModel.last_seen.desc())
                    .limit(limit)
                )
            
            models = result.scalars().all()
            return [self._model_to_dict(m) for m in models]

    async def upsert_opportunity(self, opportunity: Opportunity) -> bool:
        """
        Persists or updates a opportunity state using PostgreSQL UPSERT.
        Returns True if inserted (new), False if updated.
        """
        async with async_session_maker() as session:
            # Check if exists
            result = await session.execute(
                select(OpportunityModel).where(OpportunityModel.id == opportunity.url)
            )
            exists = result.scalar_one_or_none() is not None
            
            # Prepare values
            values = {
                "id": opportunity.url,
                "source": opportunity.source,
                "external_id": opportunity.external_id,
                "title": opportunity.title,
                "buyer": opportunity.buyer,
                "country": opportunity.country,
                "publication_date": opportunity.publication_date,
                "deadline": opportunity.deadline,
                "estimated_budget": opportunity.estimated_budget,
                "currency": opportunity.currency or "EUR",
                "status": opportunity.status or "OPEN",
                "url": opportunity.url,
                "summary": opportunity.summary,
                "normalized_budget_eur": opportunity.normalized_budget_eur,
                "embedding": opportunity.embedding,
                "content_hash": opportunity.content_hash,
                "change_type": opportunity.change_type or "NEW",
                "duplicate_group_id": opportunity.duplicate_group_id,
                "classification": opportunity.classification,
                "first_seen": opportunity.first_seen or datetime.now(tz=timezone.utc),
                "last_seen": datetime.now(tz=timezone.utc),
            }
            
            # PostgreSQL UPSERT with ON CONFLICT
            stmt = pg_insert(OpportunityModel).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "status": stmt.excluded.status,
                    "deadline": stmt.excluded.deadline,
                    "estimated_budget": stmt.excluded.estimated_budget,
                    "summary": stmt.excluded.summary,
                    "normalized_budget_eur": stmt.excluded.normalized_budget_eur,
                    "embedding": stmt.excluded.embedding,
                    "content_hash": stmt.excluded.content_hash,
                    "last_seen": stmt.excluded.last_seen,
                    "change_type": stmt.excluded.change_type,
                    "duplicate_group_id": stmt.excluded.duplicate_group_id,
                    "classification": stmt.excluded.classification,
                }
            )
            
            await session.execute(stmt)
            await session.commit()
            
            return not exists

    async def upsert_opportunities_batch(self, opportunities: List[Opportunity]) -> Dict[str, int]:
        """
        Batch upsert for improved performance.
        Uses bulk operations for better throughput.
        """
        counts = {"new": 0, "updated": 0}
        
        # Process in batches to avoid huge transactions
        batch_size = 100
        for i in range(0, len(opportunities), batch_size):
            batch = opportunities[i:i + batch_size]
            
            async with async_session_maker() as session:
                for opportunity in batch:
                    # Check existence
                    result = await session.execute(
                        select(OpportunityModel.id).where(OpportunityModel.id == opportunity.url)
                    )
                    exists = result.scalar_one_or_none() is not None
                    
                    if exists:
                        counts["updated"] += 1
                    else:
                        counts["new"] += 1
                    
                    values = {
                        "id": opportunity.url,
                        "source": opportunity.source,
                        "external_id": opportunity.external_id,
                        "title": opportunity.title,
                        "buyer": opportunity.buyer,
                        "country": opportunity.country,
                        "publication_date": opportunity.publication_date,
                        "deadline": opportunity.deadline,
                        "estimated_budget": opportunity.estimated_budget,
                        "currency": opportunity.currency or "EUR",
                        "status": opportunity.status or "OPEN",
                        "url": opportunity.url,
                        "summary": opportunity.summary,
                        "normalized_budget_eur": opportunity.normalized_budget_eur,
                        "embedding": opportunity.embedding,
                        "content_hash": opportunity.content_hash,
                        "change_type": opportunity.change_type or "NEW",
                        "duplicate_group_id": opportunity.duplicate_group_id,
                        "classification": opportunity.classification,
                        "first_seen": opportunity.first_seen or datetime.now(tz=timezone.utc),
                        "last_seen": datetime.now(tz=timezone.utc),
                    }
                    
                    stmt = pg_insert(OpportunityModel).values(values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "status": stmt.excluded.status,
                            "deadline": stmt.excluded.deadline,
                            "estimated_budget": stmt.excluded.estimated_budget,
                            "summary": stmt.excluded.summary,
                            "normalized_budget_eur": stmt.excluded.normalized_budget_eur,
                            "embedding": stmt.excluded.embedding,
                            "content_hash": stmt.excluded.content_hash,
                            "last_seen": stmt.excluded.last_seen,
                            "change_type": stmt.excluded.change_type,
                        }
                    )
                    await session.execute(stmt)
                
                await session.commit()
        
        return counts

    async def log_run(self, run_id: str, source: str, counts: Dict[str, int]):
        """Records a scraper execution session."""
        async with async_session_maker() as session:
            model = RunModel(
                id=run_id,
                source=source,
                new_count=counts.get('NEW', 0),
                updated_count=counts.get('UPDATED', 0),
                total_count=counts.get('TOTAL', 0),
                status="completed"
            )
            session.add(model)
            await session.commit()

    async def get_recent_opportunities(
        self, 
        source: Optional[str] = None, 
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get most recent opportunities with pagination."""
        async with async_session_maker() as session:
            query = select(OpportunityModel).order_by(OpportunityModel.last_seen.desc())
            
            if source:
                query = query.where(OpportunityModel.source == source)
            
            query = query.limit(limit).offset(offset)
            result = await session.execute(query)
            models = result.scalars().all()
            return [self._model_to_dict(m) for m in models]

    async def get_opportunity_stats(self, source: Optional[str] = None) -> Dict[str, Any]:
        """Get aggregate statistics for opportunities."""
        async with async_session_maker() as session:
            # Total count
            count_query = select(func.count(OpportunityModel.id))
            if source:
                count_query = count_query.where(OpportunityModel.source == source)
            result = await session.execute(count_query)
            total = result.scalar()
            
            # Count by change_type
            change_query = (
                select(OpportunityModel.change_type, func.count(OpportunityModel.id))
                .group_by(OpportunityModel.change_type)
            )
            if source:
                change_query = change_query.where(OpportunityModel.source == source)
            result = await session.execute(change_query)
            by_status = dict(result.all())
            
            # Count by classification
            class_query = (
                select(OpportunityModel.classification, func.count(OpportunityModel.id))
                .group_by(OpportunityModel.classification)
            )
            if source:
                class_query = class_query.where(OpportunityModel.source == source)
            result = await session.execute(class_query)
            by_classification = dict(result.all())
            
            return {
                "total": total,
                "by_change_type": by_status,
                "by_classification": by_classification
            }

    async def save_dead_letter(
        self, 
        job: ScrapeJob, 
        error_message: str, 
        error_code: Optional[str] = None
    ):
        """Persist failed job to dead letter queue."""
        async with async_session_maker() as session:
            model = DeadLetterModel(
                job_id=job.job_id,
                url=job.url,
                target_site=job.target_site,
                error_message=error_message,
                error_code=error_code,
                payload=job.model_dump(),
            )
            session.add(model)
            await session.commit()

    async def get_pending_dead_letters(
        self, 
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get pending dead letters for retry."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(DeadLetterModel)
                .where(DeadLetterModel.status == "pending")
                .where(DeadLetterModel.retry_count < DeadLetterModel.max_retries)
                .order_by(DeadLetterModel.created_at)
                .limit(limit)
            )
            models = result.scalars().all()
            return [self._model_to_dict(m) for m in models]

    async def mark_dead_letter_retried(self, dlq_id: str):
        """Mark a dead letter as retried."""
        async with async_session_maker() as session:
            await session.execute(
                update(DeadLetterModel)
                .where(DeadLetterModel.id == dlq_id)
                .values(
                    retry_count=DeadLetterModel.retry_count + 1,
                    last_retry_at=datetime.now(tz=timezone.utc),
                    status="retried"
                )
            )
            await session.commit()

    async def log_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Log an event for audit trail (Event Sourcing)."""
        async with async_session_maker() as session:
            model = EventLogModel(
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                metadata=metadata,
                correlation_id=correlation_id,
                service_name=settings.observability.service_name,
            )
            session.add(model)
            await session.commit()

    def _model_to_dict(self, model) -> Optional[Dict[str, Any]]:
        """Convert SQLAlchemy model to dictionary."""
        if model is None:
            return None
        
        result = {}
        for column in model.__table__.columns:
            value = getattr(model, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value
        return result


# Global tracker instance
postgres_tracker = PostgresTracker()
