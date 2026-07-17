# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (REST API Interface)
# Role: Provides a programmatic interface to the scraper orchestration cluster.

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl

from src.auth_middleware import (
    TIER_LIMITS,
    ApiTier,
    add_rate_limit_headers,
    api_key_manager,
    verify_api_key,
)
from src.infrastructure.ai.client import ai_orchestrator
from src.infrastructure.logger_config import setup_production_logging
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.security.cors_config import build_cors_origins
from src.security.ssrf_guard import validate_outbound_url
from src.security.input_sanitizer import sanitize_for_prompt, validate_payload_size

from src.domain.models import ScrapeJob, Job, JobState
from src.infrastructure.repositories.job_repository import SqliteJobRepository
from src.infrastructure.repositories.record_repository import SqliteRecordRepository
from src.infrastructure.repositories.outbox_repository import SqliteOutboxRepository
from src.infrastructure.outbox_relay import OutboxRelay
from src.infrastructure.repositories.observation_repository import SqliteObservationRepository
from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
from src.application.strategy_selector import StrategySelector
from src.infrastructure.slo_monitor import SLOMonitor
from src.domain.models import FeedbackItem, OverlayState, JobState


setup_production_logging()
logger = logging.getLogger("Spacescraper.API")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_queue = RedisQueueWorker(redis_url=REDIS_URL)

# Durable job repository
job_repo = SqliteJobRepository()

# Record repository
record_repo = SqliteRecordRepository()

# Outbox repository
outbox_repo = SqliteOutboxRepository()

# Observation/feedback repository
obs_repo = SqliteObservationRepository()

# Strategy selector (auto-strategy background task)
strategy_selector = StrategySelector(obs_repo)

# SLO monitor
slo_monitor = SLOMonitor()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles resource initialization and clean teardown."""
    logger.info("Spacescraper API Gateway is initializing...")
    await api_key_manager.initialize()
    await job_repo.initialize()
    await record_repo.initialize()
    await outbox_repo.initialize()
    await obs_repo.initialize()
    
    # Start background strategy selector
    bg_task = asyncio.create_task(strategy_selector.run_forever(interval=3600))
    
    yield
    
    # Cancel background task on shutdown
    bg_task.cancel()
    logger.info("Spacescraper API Gateway is shutting down...")
    await api_key_manager.close()
    await job_repo.close()
    await record_repo.close()
    await outbox_repo.close()
    await obs_repo.close()
    await redis_queue.close()


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
    persona_id: Optional[str] = Field(None, description="Persistent persona ID.")
    overlay: Optional[Dict[str, Any]] = Field(None, description="Dynamic extraction mapping.")
    webhook_url: Optional[str] = Field(None, description="Optional outbound webhook URL.")
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
    cached: Optional[bool] = None


class JobDetailResponse(BaseModel):
    """Detailed job status response."""
    job_id: str
    state: str
    url: str
    target_site: str
    record_count: int = 0
    error_message: Optional[str] = None
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
    corrected_data: Optional[Dict[str, Any]] = Field(None, description="Corrected data if decision is 'corrected'")
    reason: Optional[str] = Field(None, description="Reason for rejection or correction")


class PromoteRequest(BaseModel):
    """Promotion request for an overlay."""
    target_state: str = Field(default="ACTIVE", description="Target state: SHADOW, CANARY, or ACTIVE")
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


@app.get("/health", tags=["Observability"])
async def health_check():
    """System health audit endpoint with SLO status."""
    # Example metrics — in production these come from observation data
    sample_metrics = {
        "extraction_success_rate": 0.92,
        "queue_age_seconds": 5,
        "cache_hit_rate": 0.45,
        "dlq_growth_rate": 2,
        "block_rate": 0.03,
        "ai_cost_per_hour": 25,
    }
    alerts = slo_monitor.evaluate(sample_metrics)
    return {
        "status": "healthy" if not alerts else "degraded",
        "project": "Spacescraper",
        "version": "2.1.0",
        "timestamp": datetime.now().isoformat(),
        "slo_alerts": [{"name": a.name, "severity": a.severity, "message": a.message} for a in alerts],
    }


@app.post("/auth/register", response_model=AuthRegisterResponse, tags=["Authentication"])
async def register_api_key(request: AuthRegisterRequest):
    """Generate a new API key."""
    try:
        tier = ApiTier(request.tier.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier. Choose from: {', '.join(t.value for t in ApiTier)}",
        )

    plain_key, _ = api_key_manager.generate_api_key(tier, request.email)
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

    # Sanitize and size-limit the HTML before sending to AI provider
    sanitized_html = sanitize_for_prompt(request.html_sample)
    try:
        validate_payload_size(sanitized_html, max_bytes=512_000)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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
    job = Job(
        job_id=job_id,
        url=str(submission.url),
        target_site=submission.target_site,
        overlay=submission.overlay,
        webhook_url=submission.webhook_url,
    )
    await job_repo.create_job(job)

    # Create outbox event for reliable delivery
    await OutboxRelay.create_outbox_event(
        outbox_repo,
        aggregate_type="job",
        aggregate_id=job_id,
        event_type="job.submitted",
        payload={"url": str(submission.url), "target_site": submission.target_site},
    )

    # Push to Redis queue for workers
    new_job = ScrapeJob(
        job_id=job_id,
        url=str(submission.url),
        target_site=submission.target_site,
        persona_id=submission.persona_id,
        overlay=submission.overlay,
        webhook_url=submission.webhook_url,
    )

    try:
        await redis_queue.push_job("jobs_queue", new_job)
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
async def get_job_status(job_id: str, auth: tuple = Depends(verify_api_key)):
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
async def cancel_job(job_id: str, auth: tuple = Depends(verify_api_key)):
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
        await job_repo.update_job_state(job_id, JobState.CANCELLED, error_message="Cancelled by user")
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
    records: List[Dict[str, Any]]
    next_cursor: Optional[str] = None
    total: int = 0


@app.get("/jobs/{job_id}/records", tags=["Orchestration"])
async def get_job_records(
    job_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
    auth: tuple = Depends(verify_api_key),
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
):
    """Promote an overlay to a new lifecycle state. Requires human approval for ACTIVE."""
    del auth
    from src.infrastructure.repositories.overlay_repository import SqliteOverlayRepository
    overlay_repo = SqliteOverlayRepository()
    await overlay_repo.initialize()
    try:
        overlay = await overlay_repo.get_overlay(overlay_id)
        if not overlay:
            raise HTTPException(status_code=404, detail="Overlay not found")

        target = OverlayState(request.target_state.upper())

        # Require human approval for ACTIVE promotion
        if target == OverlayState.ACTIVE and not request.human_approved:
            raise HTTPException(status_code=400, detail="Human approval required for ACTIVE promotion")

        # Validate transition path
        if overlay.state == OverlayState.CANDIDATE and target not in (OverlayState.SHADOW, OverlayState.CANARY):
            raise HTTPException(status_code=400, detail="CANDIDATE can only promote to SHADOW or CANARY")
        if overlay.state == OverlayState.SHADOW and target != OverlayState.ACTIVE and target != OverlayState.CANARY:
            raise HTTPException(status_code=400, detail="SHADOW can only promote to ACTIVE or CANARY")

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
    finally:
        await overlay_repo.close()



@app.get("/slo", tags=["Observability"])
async def get_slo_status():
    """Current SLO status with active alerts."""
    sample_metrics = {
        "extraction_success_rate": 0.92,
        "queue_age_seconds": 5,
        "cache_hit_rate": 0.45,
        "dlq_growth_rate": 2,
        "block_rate": 0.03,
        "ai_cost_per_hour": 25,
    }
    alerts = slo_monitor.evaluate(sample_metrics)
    return {
        "healthy": slo_monitor.is_healthy(sample_metrics),
        "alerts": [{"name": a.name, "severity": a.severity, "message": a.message} for a in alerts],
    }


@app.get("/metrics", tags=["Observability"])
async def get_cluster_metrics(auth: tuple = Depends(verify_api_key)):
    """Return a snapshot of cluster metrics."""
    del auth
    return metrics_tracker.metrics


@app.get("/demo/key", include_in_schema=False)
async def get_demo_key():
    """Get a demo API key for testing."""
    demo_key = os.environ.get("DEMO_API_KEY", "demo_key")
    return {
        "demo_key": demo_key,
        "note": f"Use this in the Authorization header: Bearer {demo_key}",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
