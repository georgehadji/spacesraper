# Domain ports — abstract interfaces that infrastructure adapters implement.
# Domain and application code depend on these protocols, never on concrete adapters.

from typing import Optional, List, Protocol, Tuple
from src.domain.models import Job, JobAttempt, JobState, ExtractedRecord, OutboxEvent, OutboxStatus


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


class RecordRepository(Protocol):
    """Port for persisting and querying extracted records."""

    async def create_record(self, record: ExtractedRecord) -> ExtractedRecord:
        """Persist a new extracted record. Raises if record_id already exists."""
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
