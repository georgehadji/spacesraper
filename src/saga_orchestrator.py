# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Saga Pattern)
# Role: Distributed transaction management with compensation.

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.config_settings import settings
from src.event_bus import Event, event_bus
from src.observability_tracing import observability

logger = logging.getLogger("Spacescraper.Saga")


class SagaStatus(Enum):
    """Saga execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


@dataclass
class SagaStep:
    """A single step in a saga."""
    name: str
    action: Callable[[], Awaitable[Any]]
    compensation: Callable[[], Awaitable[Any]] | None = None
    action_result: Any = field(default=None, repr=False)
    error: str | None = None
    status: str = "pending"  # pending, success, failed, compensated


@dataclass
class SagaState:
    """Persistable saga state."""
    saga_id: str
    saga_type: str
    correlation_id: str
    status: SagaStatus
    steps: list[dict[str, Any]]
    context: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class SagaOrchestrator:
    """
    Spacescraper Saga Orchestrator.
    Manages long-running distributed transactions with compensation support.
    
    Example flow for a scraping job:
    1. Scrape page (compensate: mark as failed)
    2. Extract entities (compensate: delete extracted data)
    3. Classify opportunities (compensate: remove classification)
    4. Persist to DB (compensate: delete records)
    5. Send notifications (compensate: send cancellation)
    """
    
    def __init__(self):
        self._active_sagas: dict[str, SagaState] = {}
        self._persistence_enabled = settings.features.get("saga_pattern", False)
    
    async def execute_saga(
        self,
        saga_type: str,
        steps: list[SagaStep],
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None
    ) -> SagaState:
        """
        Execute a saga with compensation support.
        
        Args:
            saga_type: Type of saga (e.g., "scraping_job")
            steps: List of saga steps
            context: Shared context across steps
            correlation_id: Optional correlation ID for tracing
            
        Returns:
            Final saga state
        """
        saga_id = f"saga_{uuid.uuid4().hex[:12]}"
        correlation_id = correlation_id or saga_id
        
        state = SagaState(
            saga_id=saga_id,
            saga_type=saga_type,
            correlation_id=correlation_id,
            status=SagaStatus.RUNNING,
            steps=[],
            context=context or {},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC)
        )
        
        self._active_sagas[saga_id] = state
        
        with observability.span(
            f"saga.{saga_type}",
            attributes={
                "saga.id": saga_id,
                "saga.correlation_id": correlation_id,
                "saga.steps_count": len(steps)
            }
        ):
            logger.info(f"Saga {saga_id}: Starting {saga_type} with {len(steps)} steps")
            
            try:
                for i, step in enumerate(steps):
                    step_result = await self._execute_step(state, step, i)
                    
                    if not step_result:
                        # Step failed, start compensation
                        logger.warning(f"Saga {saga_id}: Step '{step.name}' failed, starting compensation")
                        await self._compensate(state, steps, i)
                        state.status = SagaStatus.COMPENSATED
                        state.error_message = step.error
                        break
                    
                    state.steps.append({
                        "name": step.name,
                        "status": "success",
                        "timestamp": datetime.now(tz=UTC).isoformat()
                    })
                else:
                    # All steps completed successfully
                    state.status = SagaStatus.COMPLETED
                    state.completed_at = datetime.now(tz=UTC)
                    logger.info(f"Saga {saga_id}: Completed successfully")
                    
                    # Publish completion event
                    await event_bus.publish("saga.completed", Event.create(
                        event_type="saga.completed",
                        aggregate_id=saga_id,
                        aggregate_type="saga",
                        payload={
                            "saga_type": saga_type,
                            "correlation_id": correlation_id,
                            "steps_count": len(steps)
                        },
                        correlation_id=correlation_id
                    ))
                
            except Exception as e:
                logger.exception(f"Saga {saga_id}: Unexpected error")
                state.status = SagaStatus.FAILED
                state.error_message = str(e)
                
                # Try to compensate on unexpected errors
                last_completed = len([s for s in state.steps if s.get("status") == "success"])
                await self._compensate(state, steps, last_completed)
            
            finally:
                state.updated_at = datetime.now(tz=UTC)
                if self._persistence_enabled:
                    await self._persist_state(state)
        
        return state
    
    async def _execute_step(
        self, 
        state: SagaState, 
        step: SagaStep, 
        step_index: int
    ) -> bool:
        """Execute a single saga step."""
        with observability.span(
            f"saga.step.{step.name}",
            attributes={
                "saga.id": state.saga_id,
                "step.index": step_index,
                "step.name": step.name
            }
        ):
            try:
                logger.debug(f"Saga {state.saga_id}: Executing step '{step.name}'")
                step.status = "running"
                
                # Execute the action
                result = await step.action()
                step.action_result = result
                step.status = "success"
                
                # Store result in context
                state.context[f"step_{step_index}_result"] = result
                
                logger.debug(f"Saga {state.saga_id}: Step '{step.name}' completed")
                return True
                
            except Exception as e:
                logger.error(f"Saga {state.saga_id}: Step '{step.name}' failed: {e}")
                step.error = str(e)
                step.status = "failed"
                return False
    
    async def _compensate(
        self, 
        state: SagaState, 
        steps: list[SagaStep], 
        last_completed_index: int
    ):
        """
        Compensate completed steps in reverse order.
        
        Args:
            state: Current saga state
            steps: All saga steps
            last_completed_index: Index of last successfully completed step
        """
        state.status = SagaStatus.COMPENSATING
        
        with observability.span(
            "saga.compensate",
            attributes={
                "saga.id": state.saga_id,
                "steps_to_compensate": last_completed_index + 1
            }
        ):
            logger.info(f"Saga {state.saga_id}: Compensating {last_completed_index + 1} steps")
            
            for i in range(last_completed_index, -1, -1):
                step = steps[i]
                
                if step.compensation:
                    try:
                        with observability.span(
                            f"saga.compensate.{step.name}",
                            attributes={
                                "saga.id": state.saga_id,
                                "step.index": i
                            }
                        ):
                            logger.debug(f"Saga {state.saga_id}: Compensating step '{step.name}'")
                            await step.compensation()
                            step.status = "compensated"
                            
                    except Exception as e:
                        # Compensation failure - this is serious
                        logger.critical(
                            f"Saga {state.saga_id}: Compensation failed for step '{step.name}': {e}"
                        )
                        # Continue with other compensations but log the issue
    
    async def _persist_state(self, state: SagaState):
        """Persist saga state to database (for recovery)."""
        # TODO: Implement persistence if needed
        pass
    
    def get_saga_state(self, saga_id: str) -> SagaState | None:
        """Get current state of a saga."""
        return self._active_sagas.get(saga_id)


# Predefined sagas for common workflows
class ScrapingSaga:
    """Saga for complete scraping workflow."""
    
    def __init__(
        self,
        scraper_engine,
        extraction_strategy,
        tracker,
        classifier,
        notifier
    ):
        self.scraper = scraper_engine
        self.strategy = extraction_strategy
        self.tracker = tracker
        self.classifier = classifier
        self.notifier = notifier
        self.orchestrator = SagaOrchestrator()
    
    async def execute(self, job, pipeline) -> SagaState:
        """Execute the scraping saga for a job."""
        extracted_entities = []
        persisted_ids = []
        
        steps = [
            SagaStep(
                name="scrape_page",
                action=lambda: self._scrape_page(job),
                compensation=lambda: self._compensate_scrape(job.job_id)
            ),
            SagaStep(
                name="extract_entities",
                action=lambda: self._extract_entities(job, extracted_entities),
                compensation=lambda: self._compensate_extraction(extracted_entities)
            ),
            SagaStep(
                name="classify_opportunities",
                action=lambda: self._classify_opportunities(extracted_entities),
                compensation=lambda: self._compensate_classification(extracted_entities)
            ),
            SagaStep(
                name="persist_to_db",
                action=lambda: self._persist_entities(extracted_entities, persisted_ids),
                compensation=lambda: self._compensate_persistence(persisted_ids)
            ),
            SagaStep(
                name="send_notifications",
                action=lambda: self._send_notifications(extracted_entities, job),
            )
        ]
        
        return await self.orchestrator.execute_saga(
            saga_type="scraping_job",
            steps=steps,
            context={"job_id": job.job_id, "url": job.url},
            correlation_id=job.job_id
        )
    
    async def _scrape_page(self, job):
        """Step 1: Scrape the page."""
        from src.infrastructure.browser.engine import ScraperEngine
        
        engine = ScraperEngine(context_pool=self.scraper)
        await engine.start(persona_id=job.persona_id)
        try:
            payload = await engine.crawl(job.url)
            return payload
        finally:
            await engine.close()
    
    async def _compensate_scrape(self, job_id: str):
        """Compensation for scrape: Log failure."""
        logger.info(f"Compensating scrape for job {job_id}")
        # Could mark job as failed in tracking system
    
    async def _extract_entities(self, job, container: list):
        """Step 2: Extract entities."""
        # This would be called with the payload from step 1
        # For now, simplified
        pass
    
    async def _compensate_extraction(self, entities: list):
        """Compensation for extraction: Clear extracted entities."""
        entities.clear()
    
    async def _classify_opportunities(self, entities: list):
        """Step 3: Classify opportunities."""
        for entity in entities:
            if hasattr(entity, 'title'):
                entity.classification = self.classifier.classify(entity.title)
    
    async def _compensate_classification(self, entities: list):
        """Compensation for classification: Remove classifications."""
        for entity in entities:
            if hasattr(entity, 'classification'):
                entity.classification = None
    
    async def _persist_entities(self, entities: list, id_container: list):
        """Step 4: Persist to database."""
        for entity in entities:
            await self.tracker.upsert_opportunity(entity)
            id_container.append(entity.url)
    
    async def _compensate_persistence(self, ids: list[str]):
        """Compensation for persistence: Mark as failed."""
        logger.warning(f"Compensating persistence for {len(ids)} entities")
        # In real implementation, could delete or mark as failed
    
    async def _send_notifications(self, entities, job):
        """Step 5: Send notifications."""
        if entities and job.webhook_url:
            # Send webhook
            pass


# Global orchestrator instance
saga_orchestrator = SagaOrchestrator()
