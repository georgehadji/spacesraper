# Domain ports — abstract interfaces that infrastructure adapters implement.
# Domain and application code depend on these protocols, never on concrete adapters.

from typing import Optional, List, Protocol, Tuple, Any, Dict, runtime_checkable
from src.domain.models import Job, JobAttempt, JobState, ExtractedRecord, OutboxEvent, OutboxStatus, ExtractionSchema, ExtractionOverlay, OverlayState, SearchHit


class JobRepository(Protocol):
    """Port for persisting and querying job lifecycle records."""

    async def create_job(self, job: Job) -> Job:
        """Persist a new job record. Raises if job_id already exists."""
        ...

    async def get_job(self, job_id: str) -> Optional[Job]:
        """Retrieve a job by its ID, or None if not found."""
        ...

    async def update_job_state(
        self, job_id: str, new_state: JobState, *, error_message: Optional[str] = None
    ) -> Optional[Job]:
        """Atomically transition a job to a new state. Returns the updated job or None."""
        ...

    async def update_job_record_count(self, job_id: str, count: int) -> None:
        """Update the extracted record count for a job."""
        ...

    async def list_jobs(
        self, state: Optional[JobState] = None, limit: int = 50, offset: int = 0
    ) -> List[Job]:
        """List jobs, ordered by created_at DESC, optionally filtered by state."""
        ...

    async def create_attempt(self, attempt: JobAttempt) -> JobAttempt:
        """Record a new execution attempt."""
        ...

    async def update_attempt(
        self, attempt_id: str, *, state: Optional[JobState] = None,
        finished_at: Optional[str] = None, error_message: Optional[str] = None
    ) -> Optional[JobAttempt]:
        """Update attempt state. Returns the updated attempt or None."""
        ...

    async def get_attempts(self, job_id: str) -> List[JobAttempt]:
        """List all attempts for a job."""
        ...


@runtime_checkable
class RecordRepository(Protocol):
    """Port for persisting and querying extracted records."""

    async def create_record(self, record: ExtractedRecord, job_id: str = "") -> ExtractedRecord:
        """
        Persist a new extracted record under the given job_id. Raises if record_id
        already exists. job_id is what list_records(job_id) filters on.
        """
        ...

    async def get_record(self, record_id: str) -> Optional[ExtractedRecord]:
        """Retrieve a record by its ID, or None if not found."""
        ...

    async def list_records(
        self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
    ) -> Tuple[List[ExtractedRecord], Optional[str]]:
        """
        List records for a job with cursor-based pagination.
        Returns (records, next_cursor). next_cursor is None when no more pages.
        Records are ordered by created_at ASC.
        """
        ...

    async def update_record(
        self, record_id: str, *,
        data: Optional[dict] = None,
        change_type: Optional[str] = None,
        last_seen: Optional[str] = None,
    ) -> Optional[ExtractedRecord]:
        """Update a record's mutable fields. Returns the updated record or None."""
        ...


class OutboxRepository(Protocol):
    """Port for reliable outbox event delivery."""

    async def create_event(self, event: OutboxEvent) -> OutboxEvent:
        """Persist a new outbox event. Raises if event_id already exists."""
        ...

    async def get_pending_events(
        self, limit: int = 50, min_retry_delay_seconds: int = 10
    ) -> List[OutboxEvent]:
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

    async def get_event(self, event_id: str) -> Optional[OutboxEvent]:
        """Retrieve an event by its ID."""
        ...


class OverlayRepository(Protocol):
    """Port for managing extraction overlays and schemas."""

    async def create_schema(self, schema: ExtractionSchema) -> ExtractionSchema:
        """Persist a new extraction schema."""
        ...

    async def get_schema(self, schema_id: str) -> Optional[ExtractionSchema]:
        """Retrieve a schema by its ID."""
        ...

    async def list_schemas(self) -> List[ExtractionSchema]:
        """List all registered extraction schemas."""
        ...

    async def create_overlay(self, overlay: ExtractionOverlay) -> ExtractionOverlay:
        """Persist a new overlay."""
        ...

    async def get_overlay(self, overlay_id: str) -> Optional[ExtractionOverlay]:
        """Retrieve an overlay by its ID."""
        ...

    async def get_active_overlay(self, domain: str) -> Optional[ExtractionOverlay]:
        """Get the ACTIVE overlay for a domain."""
        ...

    async def update_overlay_state(
        self, overlay_id: str, new_state: OverlayState,
    ) -> Optional[ExtractionOverlay]:
        """Transition an overlay to a new state. Idempotent."""
        ...

    async def list_overlays(self, domain: Optional[str] = None) -> List[ExtractionOverlay]:
        """List overlays, optionally filtered by domain."""
        ...


class ApiKeyStore(Protocol):
    """Port for persisting and validating API keys."""

    async def save(self, key_hash: str, key_data: Dict[str, Any]) -> None:
        """
        Save a hashed API key with metadata.
        key_hash: SHA-256 hash of the plain key (the only thing stored)
        key_data: metadata dict (tier, owner_email, created_at, expires_at, is_active, etc.)
        """
        ...

    async def get_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
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

    async def search(self, query: str, *, max_results: int = 10) -> List[SearchHit]:
        """
        Execute a search query and return ranked hits.
        Returns an empty list on failure or when the provider is unavailable —
        never raises for a downstream/network failure.
        """
        ...

    async def is_available(self) -> bool:
        """Check if the provider is configured and reachable."""
        ...
