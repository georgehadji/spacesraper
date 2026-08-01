# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Event Bus)
# Role: Kafka-based event-driven messaging with fallback to Valkey.

import json
import logging
from typing import Optional, Callable, Any, Dict, List
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import asyncio

from src.config_settings import settings

logger = logging.getLogger("Spacescraper.EventBus")


@dataclass
class Event:
    """Base event class for all domain events."""
    event_id: str
    event_type: str
    aggregate_id: str
    aggregate_type: str
    payload: Dict[str, Any]
    timestamp: str
    correlation_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    
    @classmethod
    def create(
        cls,
        event_type: str,
        aggregate_id: str,
        aggregate_type: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "Event":
        return cls(
            event_id=f"evt_{datetime.now(tz=timezone.utc).timestamp()}_{hash(str(payload)) & 0xFFFFFF:06x}",
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            payload=payload,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            correlation_id=correlation_id,
            metadata=metadata or {}
        )


class EventBus:
    """
    Spacescraper Event Bus.
    Primary: Apache Kafka for production
    Fallback: Valkey Lists for development/simple deployments
    """
    
    def __init__(self):
        self._kafka_producer = None
        self._kafka_consumer = None
        self._valkey_client = None
        self._use_kafka = settings.features.get("kafka_events", False)
        self._initialized = False
        
    async def initialize(self):
        """Initialize the event bus (Kafka or Valkey)."""
        if self._initialized:
            return
            
        if self._use_kafka:
            try:
                await self._init_kafka()
                logger.info("EventBus: Using Kafka")
            except Exception as e:
                logger.warning(f"EventBus: Kafka unavailable ({e}), falling back to Valkey")
                self._use_kafka = False
                await self._init_valkey()
        else:
            await self._init_valkey()
            logger.info("EventBus: Using Valkey")
        
        self._initialized = True

    async def _init_kafka(self):
        """Initialize Kafka producer/consumer."""
        try:
            from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
            
            self._kafka_producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka.bootstrap_servers,
                client_id=settings.kafka.client_id,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                retries=settings.kafka.retries,
                retry_backoff_ms=settings.kafka.retry_backoff_ms,
            )
            await self._kafka_producer.start()
            
        except ImportError:
            raise RuntimeError("aiokafka not installed")

    async def _init_valkey(self):
        """Initialize Valkey client."""
        import valkey.asyncio as valkey
        self._valkey_client = valkey.from_url(
            str(settings.valkey.url),
            decode_responses=True
        )

    async def publish(self, topic: str, event: Event) -> bool:
        """
        Publish an event to a topic.
        
        Args:
            topic: Topic/queue name
            event: Event to publish
            
        Returns:
            True if published successfully
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            if self._use_kafka and self._kafka_producer:
                await self._kafka_producer.send(
                    topic,
                    value=asdict(event),
                    key=event.aggregate_id.encode()
                )
            else:
                # Valkey fallback
                await self._valkey_client.rpush(
                    f"events:{topic}",
                    json.dumps(asdict(event))
                )
            
            logger.debug(f"EventBus: Published {event.event_type} to {topic}")
            return True
            
        except Exception as e:
            logger.error(f"EventBus: Failed to publish to {topic}: {e}")
            return False

    async def publish_batch(self, topic: str, events: List[Event]) -> int:
        """
        Publish multiple events in batch (more efficient).
        
        Returns:
            Number of successfully published events
        """
        if not self._initialized:
            await self.initialize()
        
        success_count = 0
        
        try:
            if self._use_kafka and self._kafka_producer:
                # Kafka batch send
                for event in events:
                    await self._kafka_producer.send(
                        topic,
                        value=asdict(event),
                        key=event.aggregate_id.encode()
                    )
                success_count = len(events)
            else:
                # Valkey pipeline
                pipe = self._valkey_client.pipeline()
                for event in events:
                    pipe.rpush(f"events:{topic}", json.dumps(asdict(event)))
                results = await pipe.execute()
                success_count = sum(1 for r in results if r)
            
            logger.debug(f"EventBus: Published {success_count}/{len(events)} events to {topic}")
            return success_count
            
        except Exception as e:
            logger.error(f"EventBus: Batch publish failed: {e}")
            return success_count

    async def subscribe(
        self, 
        topic: str, 
        handler: Callable[[Event], Any],
        group_id: Optional[str] = None
    ):
        """
        Subscribe to a topic and process events.
        
        Args:
            topic: Topic to subscribe to
            handler: Async function to process events
            group_id: Consumer group (for Kafka competing consumers)
        """
        if not self._initialized:
            await self.initialize()
        
        if self._use_kafka:
            await self._subscribe_kafka(topic, handler, group_id)
        else:
            await self._subscribe_valkey(topic, handler)

    async def _subscribe_kafka(
        self, 
        topic: str, 
        handler: Callable[[Event], Any],
        group_id: Optional[str] = None
    ):
        """Kafka consumer with consumer groups."""
        from aiokafka import AIOKafkaConsumer
        
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=settings.kafka.bootstrap_servers,
            group_id=group_id or f"{settings.kafka.client_id}-group",
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            auto_offset_reset='earliest',
            enable_auto_commit=True,
            max_poll_records=100,
        )
        await consumer.start()
        
        try:
            async for msg in consumer:
                try:
                    event = Event(**msg.value)
                    await handler(event)
                except Exception as e:
                    logger.error(f"EventBus: Handler error for {topic}: {e}")
                    # Send to DLQ
                    await self.publish("dlq", Event.create(
                        event_type="handler_error",
                        aggregate_id=msg.value.get("event_id", "unknown"),
                        aggregate_type="event",
                        payload={"error": str(e), "original": msg.value},
                        correlation_id=msg.value.get("correlation_id")
                    ))
        finally:
            await consumer.stop()

    async def _subscribe_valkey(self, topic: str, handler: Callable[[Event], Any]):
        """Valkey consumer with blocking pop."""
        queue_key = f"events:{topic}"
        
        while True:
            try:
                # Blocking pop with timeout
                result = await self._valkey_client.blpop(queue_key, timeout=1)
                
                if result:
                    _, payload = result
                    event_data = json.loads(payload)
                    event = Event(**event_data)
                    
                    try:
                        await handler(event)
                    except Exception as e:
                        logger.error(f"EventBus: Handler error for {topic}: {e}")
                        # Send to DLQ
                        await self._valkey_client.rpush(
                            "events:dlq",
                            json.dumps({
                                "error": str(e),
                                "original": event_data,
                                "timestamp": datetime.now(tz=timezone.utc).isoformat()
                            })
                        )
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"EventBus: Consumer error: {e}")
                await asyncio.sleep(1)

    async def close(self):
        """Close connections."""
        if self._use_kafka and self._kafka_producer:
            await self._kafka_producer.stop()
        if self._valkey_client:
            await self._valkey_client.close()
        self._initialized = False


# Domain-specific event creators
class OpportunityEvents:
    """Factory for opportunity-related domain events."""
    
    @staticmethod
    def discovered(opportunity_data: Dict[str, Any], correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="opportunity.discovered",
            aggregate_id=opportunity_data.get("url", "unknown"),
            aggregate_type="opportunity",
            payload=opportunity_data,
            correlation_id=correlation_id
        )
    
    @staticmethod
    def created(opportunity_data: Dict[str, Any], correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="opportunity.created",
            aggregate_id=opportunity_data.get("url", "unknown"),
            aggregate_type="opportunity",
            payload=opportunity_data,
            correlation_id=correlation_id
        )
    
    @staticmethod
    def updated(opportunity_data: Dict[str, Any], changes: Dict[str, Any], correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="opportunity.updated",
            aggregate_id=opportunity_data.get("url", "unknown"),
            aggregate_type="opportunity",
            payload={"opportunity": opportunity_data, "changes": changes},
            correlation_id=correlation_id
        )
    
    @staticmethod
    def classified(opportunity_id: str, classification: str, confidence: float, correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="opportunity.classified",
            aggregate_id=opportunity_id,
            aggregate_type="opportunity",
            payload={"classification": classification, "confidence": confidence},
            correlation_id=correlation_id
        )


class JobEvents:
    """Factory for job-related domain events."""
    
    @staticmethod
    def submitted(job_data: Dict[str, Any]) -> Event:
        return Event.create(
            event_type="job.submitted",
            aggregate_id=job_data.get("job_id", "unknown"),
            aggregate_type="job",
            payload=job_data
        )
    
    @staticmethod
    def started(job_id: str, worker_id: str, correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="job.started",
            aggregate_id=job_id,
            aggregate_type="job",
            payload={"worker_id": worker_id},
            correlation_id=correlation_id
        )
    
    @staticmethod
    def completed(job_id: str, result: Dict[str, Any], duration_ms: int, correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="job.completed",
            aggregate_id=job_id,
            aggregate_type="job",
            payload={"result": result, "duration_ms": duration_ms},
            correlation_id=correlation_id
        )
    
    @staticmethod
    def failed(job_id: str, error: str, error_code: str, correlation_id: Optional[str] = None) -> Event:
        return Event.create(
            event_type="job.failed",
            aggregate_id=job_id,
            aggregate_type="job",
            payload={"error": error, "error_code": error_code},
            correlation_id=correlation_id
        )


# Global event bus instance
event_bus = EventBus()
