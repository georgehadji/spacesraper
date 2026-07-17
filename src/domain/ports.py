# Domain ports — abstract interfaces that infrastructure adapters implement.
# Domain and application code depend on these protocols, never on concrete adapters.

from typing import Optional, List, Protocol
from src.domain.models import Job, JobAttempt, JobState


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
