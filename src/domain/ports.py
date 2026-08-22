# Domain ports — abstract interfaces that infrastructure adapters implement.
# Domain and application code depend on these protocols, never on concrete adapters.

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from src.domain.fetch import FetchRequest, FetchResult
from src.domain.models import (
    ApiKey,
    DomainProfile,
    EvaluationResult,
    ExtractedRecord,
    ExtractionOverlay,
    ExtractionSchema,
    FeedbackItem,
    Job,
    JobAttempt,
    JobState,
    OutboxEvent,
    OverlayState,
    QueueMessage,
    StrategyObservation,
)


class FetcherPort(Protocol):
    """P1: one fetch tier (HTTP-impersonating or full stealth browser)."""

    async def fetch(self, request: FetchRequest) -> FetchResult:
        ...


class JobRepository(Protocol):
    """Port for persisting and querying job lifecycle records."""

    async def create_job(self, job: Job) -> Job:
        """Persist a new job record. Raises if job_id already exists."""
        ...

    async def get_job(self, job_id: str) -> Job | None:
        """Retrieve a job by its ID, or None if not found."""
        ...

    async def get_by_idempotency_key(self, key: str) -> Job | None:
        """Retrieve a job by its idempotency key, or None if not found."""
        ...

    async def update_job_state(
        self, job_id: str, new_state: JobState, *, expected_version: int,
        error_message: str | None = None
    ) -> Job | None:
        """
        Atomically transition a job to a new state using optimistic concurrency.
        Only succeeds if job.version == expected_version.
        Returns the updated job, or None if version conflict or job not found.
        """
        ...

    async def update_job_record_count(self, job_id: str, count: int) -> None:
        """Update the extracted record count for a job."""
        ...

    async def list_jobs(
        self, state: JobState | None = None, limit: int = 50, offset: int = 0
    ) -> list[Job]:
        """List jobs, ordered by created_at DESC, optionally filtered by state."""
        ...

    async def create_attempt(self, attempt: JobAttempt) -> JobAttempt:
        """Record a new execution attempt."""
        ...

    async def update_attempt(
        self, attempt_id: str, *, state: JobState | None = None,
        finished_at: str | None = None, error_message: str | None = None
    ) -> JobAttempt | None:
        """Update attempt state. Returns the updated attempt or None."""
        ...

    async def get_attempts(self, job_id: str) -> list[JobAttempt]:
        """List all attempts for a job."""
        ...

    async def soft_delete_job(self, job_id: str) -> Job | None:
        """Soft-delete a job by transitioning to DELETED state. Returns the updated job or None."""
        ...

    async def heartbeat(self, job_id: str) -> None:
        """Update last_heartbeat_at for a job to signal worker is alive."""
        ...

    async def find_stale_jobs(self, stale_seconds: int, limit: int = 50) -> list[Job]:
        """Find RUNNING jobs whose last_heartbeat_at is older than stale_seconds, or never heartbeated.
        Returns up to `limit` jobs that are candidates for reaping."""
        ...

    async def purge_expired_jobs(self, retention_days: int = 90) -> int:
        """Hard-delete jobs soft-deleted longer than retention_days ago. Returns count purged."""
        ...


class RecordRepository(Protocol):
    """Port for persisting and querying extracted records."""

    async def create_record(self, record: ExtractedRecord) -> ExtractedRecord:
        """Persist a new extracted record. Raises if record_id already exists."""
        ...

    async def get_record(self, record_id: str) -> ExtractedRecord | None:
        """Retrieve a record by its ID, or None if not found."""
        ...

    async def list_records(
        self, job_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExtractedRecord], str | None]:
        """
        List records for a job with cursor-based pagination.
        Returns (records, next_cursor). next_cursor is None when no more pages.
        Records are ordered by created_at ASC.
        """
        ...

    async def update_record(
        self, record_id: str, *,
        data: dict[str, Any] | None = None,
        change_type: str | None = None,
        last_seen: str | None = None,
    ) -> ExtractedRecord | None:
        """Update a record's mutable fields. Returns the updated record or None."""
        ...

    async def soft_delete_record(self, record_id: str) -> ExtractedRecord | None:
        """Soft-delete a record by setting deleted_at. Returns the updated record or None."""
        ...

    async def purge_expired_records(self, retention_days: int = 90) -> int:
        """Hard-delete records soft-deleted longer than retention_days ago. Returns count purged."""
        ...


class OutboxRepository(Protocol):
    """Port for reliable outbox event delivery."""

    async def create_event(self, event: OutboxEvent) -> OutboxEvent:
        """Persist a new outbox event. Raises if event_id already exists."""
        ...

    async def get_pending_events(
        self, limit: int = 50, min_retry_delay_seconds: int = 10
    ) -> list[OutboxEvent]:
        """
        Get pending events ready for delivery.
        Only returns events where enough time has passed since last attempt.
        """
        ...

    async def mark_delivered(self, event_id: str) -> None:
        """Mark an event as successfully delivered."""
        ...

    async def mark_failed(self, event_id: str, error: str) -> None:
        """Increment retry count and record error. Marks FAILED if max_retries exceeded."""
        ...

    async def get_event(self, event_id: str) -> OutboxEvent | None:
        """Retrieve an event by its ID."""
        ...


class OverlayRepository(Protocol):
    """Port for managing extraction overlays and schemas."""

    async def create_schema(self, schema: ExtractionSchema) -> ExtractionSchema:
        """Persist a new extraction schema."""
        ...

    async def get_schema(self, schema_id: str) -> ExtractionSchema | None:
        """Retrieve a schema by its ID."""
        ...

    async def list_schemas(self) -> list[ExtractionSchema]:
        """List all registered extraction schemas."""
        ...

    async def create_overlay(self, overlay: ExtractionOverlay) -> ExtractionOverlay:
        """Persist a new overlay."""
        ...

    async def get_overlay(self, overlay_id: str) -> ExtractionOverlay | None:
        """Retrieve an overlay by its ID."""
        ...

    async def get_active_overlay(self, domain: str) -> ExtractionOverlay | None:
        """Get the ACTIVE overlay for a domain."""
        ...

    async def update_overlay_state(
        self, overlay_id: str, new_state: OverlayState,
    ) -> ExtractionOverlay | None:
        """Transition an overlay to a new state. Idempotent."""
        ...

    async def list_overlays(self, domain: str | None = None) -> list[ExtractionOverlay]:
        """List overlays, optionally filtered by domain."""
        ...


class ApiKeyRepository(Protocol):
    """Port for durable API key storage (F12) — keys must survive process
    restart and be visible across replicas, which an in-memory dict cannot do."""

    async def create_key(self, key: ApiKey) -> ApiKey:
        """Persist a new API key. Raises if key_hash already exists."""
        ...

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Look up a key by its hash, or None if not found."""
        ...

    async def get_by_key_id(self, key_id: str) -> ApiKey | None:
        """Look up a key by its public key_id (the identifier shown to operators), or None."""
        ...

    async def set_active(self, key_hash: str, is_active: bool) -> ApiKey | None:
        """Activate or revoke a key. Returns the updated key, or None if not found."""
        ...


class MessageBus(Protocol):
    """Port for the queue mechanism (C4). Streams is the sole adapter (ADR pending, W8.2) —
    this Protocol exists so application code depends on queue semantics, not on Valkey."""

    async def connect(self) -> None:
        """Establish (or fall back to an in-memory) connection to the broker."""
        ...

    async def close(self) -> None:
        """Release the broker connection."""
        ...

    async def push(self, stream: str, message: QueueMessage) -> str:
        """Publish a message to a stream. Returns the broker-assigned entry ID."""
        ...

    async def push_dlq(self, stream: str, message: QueueMessage, reason: str) -> str:
        """Publish a message directly to a stream's dead-letter queue."""
        ...

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        callback: Callable[[QueueMessage], Awaitable[bool]],
        *,
        batch_size: int = 10,
        block_ms: int = 2000,
        max_retries: int = 3,
        claim_idle_ms: int = 60_000,
    ) -> None:
        """Long-poll consumer loop. callback returns True on success, False to trigger retry/DLQ."""
        ...

    async def get_allowed_fanout(self, root_job_id: str, requested: int, max_fanout: int) -> int:
        """Atomic fan-out budget check. Returns how many of `requested` child jobs are allowed."""
        ...

    async def get_stream_length(self, stream: str) -> int:
        """Approximate number of entries in a stream."""
        ...

    async def get_dlq_length(self, stream: str) -> int:
        """Number of dead-lettered messages for a stream."""
        ...

    async def get_pending_count(self, stream: str, group: str) -> int:
        """Number of pending (unacknowledged) messages for a consumer group."""
        ...


class ObservationRepository(Protocol):
    """Port for observations, feedback, evaluations, and domain profiles."""

    async def create_observation(self, obs: StrategyObservation) -> StrategyObservation:
        """Record a strategy observation."""
        ...

    async def get_observations(
        self, domain: str | None = None, strategy: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[StrategyObservation]:
        """List observations, optionally filtered."""
        ...

    async def create_feedback(self, fb: FeedbackItem) -> FeedbackItem:
        """Record user feedback."""
        ...

    async def create_evaluation(self, ev: EvaluationResult) -> EvaluationResult:
        """Store an evaluation result."""
        ...

    async def get_or_create_profile(self, domain: str) -> DomainProfile:
        """Get or create a domain profile."""
        ...

    async def update_profile(self, profile: DomainProfile) -> None:
        """Update a domain profile."""
        ...
