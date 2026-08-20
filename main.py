# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (REST API Interface)
# Role: Provides a programmatic interface to the scraper orchestration cluster.

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl

from src.application.strategy_selector import StrategySelector
from src.auth_middleware import (
    TIER_LIMITS,
    ApiTier,
    add_rate_limit_headers,
    api_key_manager,
    verify_admin_key,
    verify_api_key,
)
from src.config_settings import settings
from src.domain.models import FeedbackItem, Job, JobState, MessageType, OverlayState, ScrapeJob
from src.infrastructure.ai.client import ai_orchestrator
from src.infrastructure.logger_config import setup_production_logging
from src.infrastructure.middleware.correlation import get_request_id
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.outbox_relay import OutboxRelay
from src.infrastructure.queues.stream_queue import ValkeyStreamQueue, make_message
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.infrastructure.slo_monitor import SLOMonitor
from src.security.cors_config import build_cors_origins
from src.security.input_sanitizer import sanitize_for_prompt, validate_payload_size
from src.security.ssrf_guard import validate_outbound_url

setup_production_logging()
logger = logging.getLogger("Spacescraper.API")

VALKEY_URL = os.environ.get("VALKEY_URL", "valkey://localhost:6379")


@dataclass
class AppContainer:
    """Composition root: every repository and the message bus the API wires
    together, built once and handed out through the get_*() Depends()
    providers below instead of read as bare module globals (W4.1/W4.2)."""

    stream_queue: ValkeyStreamQueue
    job_repo: SqliteJobRepository
    record_repo: SqliteRecordRepository
    outbox_repo: SqliteOutboxRepository
    overlay_repo: SqliteOverlayRepository
    obs_repo: SqliteObservationRepository
    strategy_selector: StrategySelector
    outbox_relay: OutboxRelay

    @classmethod
    def build(cls, valkey_url: str) -> "AppContainer":
        stream_queue = ValkeyStreamQueue(valkey_url=valkey_url)
        obs_repo = SqliteObservationRepository()
        outbox_repo = SqliteOutboxRepository()
        return cls(
            stream_queue=stream_queue,
            job_repo=SqliteJobRepository(),
            record_repo=SqliteRecordRepository(),
            outbox_repo=outbox_repo,
            overlay_repo=SqliteOverlayRepository(),
            obs_repo=obs_repo,
            strategy_selector=StrategySelector(obs_repo),
            outbox_relay=OutboxRelay(outbox_repo, stream_queue=stream_queue),
        )

    def repos(self):
        """The five repos with an initialize()/close() lifecycle, for lifespan."""
        return (self.job_repo, self.record_repo, self.outbox_repo, self.obs_repo, self.overlay_repo)


container = AppContainer.build(VALKEY_URL)

# SLO monitor (stateless evaluator, no lifecycle — not part of the container)
slo_monitor = SLOMonitor()


def get_job_repo() -> SqliteJobRepository:
    return container.job_repo


def get_record_repo() -> SqliteRecordRepository:
    return container.record_repo


def get_outbox_repo() -> SqliteOutboxRepository:
    return container.outbox_repo


def get_overlay_repo() -> SqliteOverlayRepository:
    return container.overlay_repo


def get_obs_repo() -> SqliteObservationRepository:
    return container.obs_repo


def get_stream_queue() -> ValkeyStreamQueue:
    return container.stream_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles resource initialization and clean teardown."""
    logger.info("Spacescraper API Gateway is initializing...")
    await api_key_manager.initialize()
    for repo in container.repos():
        await repo.initialize()
    await metrics_tracker.initialize()
    # Verifies the broker and swaps in the offline in-memory queue if it is down.
    # Without this the client stays lazily unconnected and every enqueue 500s.
    await container.stream_queue.connect()

    # Start background strategy selector
    bg_task = asyncio.create_task(container.strategy_selector.run_forever(interval=3600))
    # Start outbox relay (shares stream_queue's connection; do not call
    # outbox_relay.start()/stop(), that would double connect/close it)
    outbox_task = asyncio.create_task(container.outbox_relay.run_forever())

    yield

    # Cancel background tasks on shutdown
    bg_task.cancel()
    outbox_task.cancel()
    logger.info("Spacescraper API Gateway is shutting down...")
    await api_key_manager.close()
    for repo in container.repos():
        await repo.close()
    await metrics_tracker.close()
    await container.stream_queue.close()


app = FastAPI(
    title="Spacescraper API",
    description="""
    Minimal API for dispatching scraping jobs and monitoring cluster health.

    Authentication uses bearer API keys.
    """,
    version="2.1.0",
    contact={
        "name": "Georgios-Chrysovalantis Chatzivantsidis",
        "url": "https://github.com/vibe-coding/scraper",
    },
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=build_cors_origins(),
    allow_credentials=True,  # safe because origins is never wildcard
    allow_methods=["*"],
    allow_headers=["*"],
)


class JobSubmission(BaseModel):
    """Schema for scraping job requests."""

    url: HttpUrl = Field(..., description="The target website URL to scrape.")
    target_site: str = Field("universal", description="Strategy identifier.")
    persona_id: str | None = Field(None, description="Persistent persona ID.")
    overlay: dict[str, Any] | None = Field(None, description="Dynamic extraction mapping.")
    webhook_url: str | None = Field(None, description="Optional outbound webhook URL.")
    force_refresh: bool = Field(False, description="Skip cache and force re-scrape.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://example.com",
                "target_site": "universal",
                "persona_id": "session_01",
                "force_refresh": False,
            }
        }
    }


class JobResponse(BaseModel):
    status: str
    job_id: str
    message: str
    cached: bool | None = None


class JobDetailResponse(BaseModel):
    """Detailed job status response."""
    job_id: str
    state: str
    url: str
    target_site: str
    record_count: int = 0
    error_message: str | None = None
    created_at: str
    updated_at: str
    status_url: str


class CancelResponse(BaseModel):
    status: str
    job_id: str
    message: str


class FeedbackRequest(BaseModel):
    """User feedback for an extracted record."""
    decision: str = Field(..., description="'accepted', 'rejected', or 'corrected'")
    corrected_data: dict[str, Any] | None = Field(None, description="Corrected data if decision is 'corrected'")
    reason: str | None = Field(None, description="Reason for rejection or correction")


class PromoteRequest(BaseModel):
    """Promotion request for an overlay."""
    target_state: str = Field(default="ACTIVE", description="Target state: SHADOW or ACTIVE")
    human_approved: bool = Field(default=False, description="Human approval flag")


class AutographRequest(BaseModel):
    html_sample: str


class AuthRegisterRequest(BaseModel):
    email: str
    tier: str = Field(default="free", description="API tier: free, basic, pro, enterprise")


class AuthRegisterResponse(BaseModel):
    api_key: str
    tier: str
    rate_limit: int
    message: str


@app.middleware("http")
async def add_rate_limit_middleware(request: Request, call_next):
    """Add rate limit headers to all responses."""
    response = await call_next(request)
    if hasattr(request.state, "rate_limit"):
        add_rate_limit_headers(response, request.state.rate_limit)
    return response


async def _current_slo_metrics() -> dict[str, float]:
    """Real metrics from metrics_tracker, in slo_monitor's expected shape.

    Only fields metrics_tracker actually tracks are included; slo_monitor
    skips any SLO whose metric is absent rather than treating it as passing,
    so this reports "no data" honestly instead of fabricating a healthy
    number (F16: a hardcoded 0.92 stayed "healthy" through a total outage).
    """
    stats = await metrics_tracker.get_metrics()
    metrics: dict[str, float] = {
        "extraction_success_rate": (await metrics_tracker.get_success_rate()) / 100.0,
    }
    jobs_total = stats.get("jobs_total", 0)
    if jobs_total > 0:
        metrics["block_rate"] = stats.get("captcha_encountered", 0) / jobs_total
    return metrics


@app.get("/health", tags=["Observability"])
async def health_check():
    """System health audit endpoint with SLO status."""
    alerts = slo_monitor.evaluate(await _current_slo_metrics())
    return {
        "status": "healthy" if not alerts else "degraded",
        "project": "Spacescraper",
        "version": "2.1.0",
        "timestamp": datetime.now().isoformat(),
        "slo_alerts": [{"name": a.name, "severity": a.severity, "message": a.message} for a in alerts],
    }


@app.post(
    "/auth/register",
    response_model=AuthRegisterResponse,
    tags=["Authentication"],
    dependencies=[Depends(verify_admin_key)],
)
async def register_api_key(request: AuthRegisterRequest, http_request: Request):
    """
    Generate a new API key. Admin-only (F11) — see verify_admin_key.
    Also throttled per source IP, independent of admin-key validity.
    """
    try:
        tier = ApiTier(request.tier.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier. Choose from: {', '.join(t.value for t in ApiTier)}",
        )

    client_ip = http_request.client.host if http_request.client else "unknown"
    await api_key_manager.check_registration_rate_limit(client_ip)

    plain_key, _ = await api_key_manager.generate_api_key(tier, request.email)
    return AuthRegisterResponse(
        api_key=plain_key,
        tier=tier.value,
        rate_limit=TIER_LIMITS[tier],
        message="API key generated successfully. Save this key - it will not be shown again!",
    )


@app.post("/autograph", tags=["Intelligence"])
async def generate_schema_overlay(
    request: AutographRequest,
    auth: tuple = Depends(verify_api_key),
):
    """Generate an extraction overlay from an HTML snippet with sanitization."""
    del auth

    # Reject oversize input before doing any work on it. Must run against the
    # raw payload — validating after sanitize_for_prompt would check a value
    # that's already been through injection filtering, defeating the point
    # of an early reject (F15).
    try:
        validate_payload_size(request.html_sample, max_bytes=512_000)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Filter prompt-injection patterns; compact_html_for_prompt (called inside
    # generate_overlay) owns truncation to the actual prompt budget.
    sanitized_html = sanitize_for_prompt(request.html_sample)

    overlay = await ai_orchestrator.generate_overlay(sanitized_html)
    if not overlay:
        raise HTTPException(status_code=500, detail="AI overlay generation failed.")

    return {
        "status": "success",
        "suggested_overlay": overlay,
    }


@app.post("/jobs", response_model=JobResponse, status_code=202, tags=["Orchestration"])
async def submit_job(
    submission: JobSubmission = Body(...),
    auth: tuple = Depends(verify_api_key),
    job_repo: SqliteJobRepository = Depends(get_job_repo),
    outbox_repo: SqliteOutboxRepository = Depends(get_outbox_repo),
    stream_queue: ValkeyStreamQueue = Depends(get_stream_queue),
):
    """Validate, persist, and enqueue a scraping job."""
    del auth

    # SSRF guard: validate the target URL before any processing
    try:
        validate_outbound_url(str(submission.url))
        if submission.webhook_url:
            validate_outbound_url(submission.webhook_url, require_https=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = f"job_{uuid.uuid4().hex[:8]}"

    # Create durable job record
    correlation_id = get_request_id() or None
    job = Job(
        job_id=job_id,
        url=str(submission.url),
        target_site=submission.target_site,
        overlay=submission.overlay,
        webhook_url=submission.webhook_url,
        correlation_id=correlation_id,
    )
    # Unit of work: the job row and its outbox event share job_repo's
    # connection and one transaction, so a failure between them (e.g. a
    # disk-full error on the second insert) rolls back the first instead of
    # leaving an orphaned job with no outbox event (F14).
    assert job_repo._conn is not None
    try:
        await job_repo.create_job(job, commit=False)
        await OutboxRelay.create_outbox_event(
            outbox_repo,
            aggregate_type="job",
            aggregate_id=job_id,
            event_type="job.submitted",
            payload={"url": str(submission.url), "target_site": submission.target_site},
            conn=job_repo._conn,
            commit=False,
        )
        await job_repo._conn.commit()
    except Exception:
        await job_repo._conn.rollback()
        raise

    # Push to the jobs stream for workers
    new_job = ScrapeJob(
        job_id=job_id,
        url=str(submission.url),
        target_site=submission.target_site,
        persona_id=submission.persona_id,
        overlay=submission.overlay,
        webhook_url=submission.webhook_url,
        correlation_id=correlation_id,
    )

    try:
        envelope = make_message(
            MessageType.SCRAPE_JOB,
            new_job.model_dump(mode="json"),
            correlation_id=correlation_id,
            root_job_id=job_id,
        )
        await stream_queue.push("jobs_stream", envelope)
        logger.info("Accepted job %s targeting %s", job_id, submission.target_site)
        return JobResponse(
            status="accepted",
            job_id=job_id,
            message="Task acknowledged. Workers will process it asynchronously.",
            cached=False,
        )
    except Exception as exc:
        logger.error("API submission failed: %s", exc)
        raise HTTPException(status_code=500, detail="Backend orchestration fault during job enqueuing.")


@app.get("/jobs/{job_id}", response_model=JobDetailResponse, tags=["Orchestration"])
async def get_job_status(
    job_id: str,
    auth: tuple = Depends(verify_api_key),
    job_repo: SqliteJobRepository = Depends(get_job_repo),
):
    """Get the current status of a job."""
    del auth
    job = await job_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobDetailResponse(
        job_id=job.job_id,
        state=job.state.value,
        url=job.url,
        target_site=job.target_site,
        record_count=job.record_count,
        error_message=job.error_message,
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        status_url=f"/jobs/{job.job_id}",
    )


@app.post("/jobs/{job_id}/cancel", response_model=CancelResponse, tags=["Orchestration"])
async def cancel_job(
    job_id: str,
    auth: tuple = Depends(verify_api_key),
    job_repo: SqliteJobRepository = Depends(get_job_repo),
):
    """Cancel a job that is QUEUED or RUNNING."""
    del auth
    job = await job_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.DEAD_LETTERED):
        return CancelResponse(
            status="unchanged",
            job_id=job_id,
            message=f"Job is already in terminal state {job.state.value}.",
        )

    try:
        await job_repo.update_job_state(job_id, JobState.CANCELLED, expected_version=job.version, error_message="Cancelled by user")
        logger.info("Cancelled job %s", job_id)
        return CancelResponse(
            status="cancelled",
            job_id=job_id,
            message="Job cancelled successfully.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))



class RecordsResponse(BaseModel):
    """Paginated list of extracted records."""
    records: list[dict[str, Any]]
    next_cursor: str | None = None
    total: int = 0


@app.get("/jobs/{job_id}/records", tags=["Orchestration"])
async def get_job_records(
    job_id: str,
    cursor: str | None = None,
    limit: int = 50,
    auth: tuple = Depends(verify_api_key),
    job_repo: SqliteJobRepository = Depends(get_job_repo),
    record_repo: SqliteRecordRepository = Depends(get_record_repo),
):
    """Get extracted records for a job with cursor pagination."""
    del auth
    job = await job_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    records, next_cursor = await record_repo.list_records(
        job_id, cursor=cursor, limit=min(limit, 200),
    )
    total = await record_repo.get_record_count(job_id)

    return RecordsResponse(
        records=[r.model_dump(mode="json") for r in records],
        next_cursor=next_cursor,
        total=total,
    )



@app.post("/records/{record_id}/feedback", tags=["Orchestration"])
async def submit_feedback(
    record_id: str,
    feedback: FeedbackRequest = Body(...),
    auth: tuple = Depends(verify_api_key),
    obs_repo: SqliteObservationRepository = Depends(get_obs_repo),
):
    """Submit user feedback on an extracted record."""
    del auth
    fb = FeedbackItem(
        feedback_id=f"fb_{uuid.uuid4().hex[:12]}",
        record_id=record_id,
        job_id="",
        decision=feedback.decision,
        corrected_data=feedback.corrected_data,
        reason=feedback.reason,
    )
    await obs_repo.create_feedback(fb)
    return {"status": "recorded", "feedback_id": fb.feedback_id}


@app.post("/overlays/{overlay_id}/promote", tags=["Orchestration"])
async def promote_overlay(
    overlay_id: str,
    request: PromoteRequest = Body(...),
    auth: tuple = Depends(verify_api_key),
    overlay_repo: SqliteOverlayRepository = Depends(get_overlay_repo),
):
    """Promote an overlay to a new lifecycle state. Requires human approval for ACTIVE."""
    del auth
    overlay = await overlay_repo.get_overlay(overlay_id)
    if not overlay:
        raise HTTPException(status_code=404, detail="Overlay not found")

    target = OverlayState(request.target_state.upper())

    # Require human approval for ACTIVE promotion
    if target == OverlayState.ACTIVE and not request.human_approved:
        raise HTTPException(status_code=400, detail="Human approval required for ACTIVE promotion")

    # Validate transition path
    if overlay.state == OverlayState.CANDIDATE and target != OverlayState.SHADOW:
        raise HTTPException(status_code=400, detail="CANDIDATE can only promote to SHADOW")
    if overlay.state == OverlayState.SHADOW and target != OverlayState.ACTIVE:
        raise HTTPException(status_code=400, detail="SHADOW can only promote to ACTIVE")

    # Promote
    updated = await overlay_repo.update_overlay_state(overlay_id, target)

    # If promoting to ACTIVE, set rollback target
    if target == OverlayState.ACTIVE and overlay.state == OverlayState.SHADOW:
        if overlay.rollback_overlay_id:
            # Retire old ACTIVE
            old_active = await overlay_repo.get_overlay(overlay.rollback_overlay_id)
            if old_active:
                await overlay_repo.update_overlay_state(old_active.overlay_id, OverlayState.RETIRED)

    return {
        "status": "promoted",
        "overlay_id": overlay_id,
        "previous_state": overlay.state.value,
        "new_state": target.value,
    }



@app.get("/slo", tags=["Observability"])
async def get_slo_status():
    """Current SLO status with active alerts."""
    current_metrics = await _current_slo_metrics()
    alerts = slo_monitor.evaluate(current_metrics)
    return {
        "healthy": slo_monitor.is_healthy(current_metrics),
        "alerts": [{"name": a.name, "severity": a.severity, "message": a.message} for a in alerts],
    }


@app.get("/metrics", tags=["Observability"])
async def get_cluster_metrics(auth: tuple = Depends(verify_api_key)):
    """Return a snapshot of cluster metrics."""
    del auth
    return metrics_tracker.metrics


@app.get("/demo/key", include_in_schema=False)
async def get_demo_key():
    """
    Return the configured demo API key.

    verify_api_key only accepts a demo key when DEMO_API_KEY is set and the
    environment is development, so handing out a default here would advertise a
    key that every request rejects. Use POST /auth/register instead.
    """
    demo_key = os.environ.get("DEMO_API_KEY")
    if not demo_key or settings.environment != "development":
        raise HTTPException(
            status_code=404,
            detail="No demo key configured. Set DEMO_API_KEY in a development "
                   "environment, or register a key via POST /auth/register.",
        )
    return {
        "demo_key": demo_key,
        "note": f"Use this in the Authorization header: Bearer {demo_key}",
    }


if __name__ == "__main__":
    import uvicorn

    # Auto-reload spawns a supervisor process and watches the tree: useful while
    # developing, wrong for containers and for boot.py, which manages lifecycles
    # itself. Opt in explicitly rather than defaulting it on.
    reload_enabled = os.environ.get("SPACESCRAPER_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "main:app",
        host=os.environ.get("SPACESCRAPER_HOST", "0.0.0.0"),
        port=int(os.environ.get("SPACESCRAPER_PORT", "8000")),
        reload=reload_enabled,
    )
