# Domain ports — abstract interfaces that infrastructure adapters implement.
# Domain and application code depend on these protocols, never on concrete adapters.

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

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
    OutboxStatus,
    OverlayState,
    QueueMessage,
    ResearchPlan,
    SearchHit,
    StrategyObservation,
)


class FetcherPort(Protocol):
    """P1: one fetch tier (HTTP-impersonating or full stealth browser)."""

    async def fetch(self, request: FetchRequest) -> FetchResult:
        ...


class RobotsPort(Protocol):
    """P2: fail-closed robots.txt evaluation, per domain."""

    async def is_allowed(self, url: str) -> bool:
        ...

    async def crawl_delay_seconds(self, url: str) -> float | None:
        ...


class ProxyProviderPort(Protocol):
    """P3: hands out a proxy URL for a new session lease."""

    def next_proxy(self) -> str | None:
        ...


class JobRepository(Protocol):
    """Port for persisting and querying job lifecycle records."""

    async def create_job(self, job: Job, *, conn: Any = None) -> Job:
        """Persist a new job record. Raises if job_id already exists.

        conn, when given, must be the value yielded by this repo's own
        transaction() — the write joins that transaction instead of
        auto-committing on its own (R-W1/F14)."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[Any]:
        """Opens a transaction scope. The yielded value is opaque to callers
        outside this module — pass it straight through to create_job's and
        OutboxRepository.create_event's conn= parameter so a job insert and
        its outbox event commit or roll back together (F14). Each backend's
        yielded value has a different concrete type; nothing outside a
        repository's own create_job/create_event should inspect it."""
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


@runtime_checkable
class RecordRepository(Protocol):
    """Port for persisting and querying extracted records."""

    async def create_record(self, record: ExtractedRecord, job_id: str) -> ExtractedRecord:
        """Persist a new extracted record, attributed to the job that produced it.
        job_id is what list_records(job_id) filters on.

        job_id is required, not defaulted: ExtractedRecord carries no job_id field
        itself, so a caller written against a defaulted signature would silently
        orphan the record — it would never appear in list_records(job_id=...) or
        get_record_count(job_id).
        """
        ...

    async def get_record_count(self, job_id: str) -> int:
        """Count records persisted for a job."""
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


class OutboxRepository(Protocol):
    """Port for reliable outbox event delivery."""

    async def create_event(self, event: OutboxEvent, *, conn: Any = None) -> OutboxEvent:
        """Persist a new outbox event. Raises if event_id already exists.

        conn, when given, must be the value yielded by JobRepository's own
        transaction() — see JobRepository.transaction()'s docstring."""
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

    async def list_schemas(self, limit: int = 50, offset: int = 0) -> list[ExtractionSchema]:
        """List registered extraction schemas, ordered by created_at DESC."""
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

    async def list_overlays(
        self, domain: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ExtractionOverlay]:
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
class ApiKeyStore(Protocol):
    """Port for persisting and validating API keys."""

    async def save(self, key_hash: str, key_data: dict[str, Any]) -> None:
        """
        Save a hashed API key with metadata.
        key_hash: SHA-256 hash of the plain key (the only thing stored)
        key_data: metadata dict (tier, owner_email, created_at, expires_at, is_active, etc.)
        """
        ...

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """
        Retrieve API key data by its hash.
        Returns None if key not found or revoked.
        """
        ...

    async def revoke(self, key_hash: str) -> None:
        """
        Mark an API key as revoked.
        Revoked keys return 403 (Forbidden) on the read path.
        """
        ...


class SearchProvider(Protocol):
    """Port for query-to-URL discovery (search engine adapters)."""

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]:
        """
        Execute a search query and return ranked hits.
        Returns an empty list on failure or when the provider is unavailable —
        never raises for a downstream/network failure.
        """
        ...

    async def is_available(self) -> bool:
        """Check if the provider is configured and reachable."""
        ...


class ResearchPlanRepository(Protocol):
    """Port for persisting and querying discovery (research) plans."""

    async def create_plan(self, plan: ResearchPlan) -> ResearchPlan:
        """Persist a new research plan. Raises if plan_id already exists."""
        ...

    async def get_plan(self, plan_id: str) -> ResearchPlan | None:
        """Retrieve a plan by its ID, or None if not found."""
        ...

    async def update_plan_state(
        self, plan_id: str, new_state: JobState, *, error_message: str | None = None
    ) -> ResearchPlan | None:
        """Transition a plan to a new state. Returns the updated plan or None."""
        ...

    async def set_child_job_ids(self, plan_id: str, child_job_ids: list[str]) -> ResearchPlan | None:
        """Record the ScrapeJob IDs enqueued from this plan."""
        ...

    async def set_serp_artifact_sha(self, plan_id: str, sha256: str) -> ResearchPlan | None:
        """Link the plan to its archived raw SERP artifact, for replay."""
        ...
