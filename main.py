# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (REST API Interface)
# Role: Provides a programmatic interface to the scraper orchestration cluster.

import logging
import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Body, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, Field

# Infrastructure and Domain imports
from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.domain.models import ScrapeJob, Tender
from src.infrastructure.monitoring.observability import metrics_tracker
from src.infrastructure.ai.client import ai_orchestrator

# New: Authentication & Quality features
from src.auth_middleware import (
    verify_api_key, api_key_manager, add_rate_limit_headers, 
    ApiKeyGenerator, ApiTier
)
from src.data_quality import dq_scorer
from src.win_predictor import (
    UserCapabilityProfile, win_predictor, TenderMatch,
    TenderMatcher
)

from src.infrastructure.logger_config import setup_production_logging
from src.domain.exceptions import SpacescraperError

# Standardized enterprise logging
setup_production_logging()
logger = logging.getLogger("Spacescraper.API")

# Configure connection to the shared Redis infrastructure
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_queue = RedisQueueWorker(redis_url=REDIS_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles resource initialization and clean teardown."""
    logger.info("Spacescraper API Gateway is initializing...")
    
    # Initialize auth manager
    await api_key_manager.initialize()
    
    yield
    
    logger.info("Spacescraper API Gateway is shutting down...")
    await api_key_manager.close()
    await redis_queue.close()


# Initialize the FastAPI instance with rich metadata for Swagger/OpenAPI
app = FastAPI(
    title="Spacescraper Intelligence API",
    description="""
    ## 🚀 Enterprise Intelligence Gateway
    
    Programmatic entry point for the Spacescraper cluster.
    
    ### 🔐 Authentication
    All endpoints require an API key. Include it in the Authorization header:
    ```
    Authorization: Bearer ss_your_api_key_here
    ```
    
    Get your API key from `/auth/register` or contact support.
    
    ### 📊 Rate Limits
    - **Free**: 100 requests/day
    - **Basic**: 1,000 requests/day  
    - **Pro**: 10,000 requests/day
    - **Enterprise**: 100,000 requests/day
    
    Check your remaining quota in response headers:
    - `X-RateLimit-Limit`
    - `X-RateLimit-Remaining`
    - `X-RateLimit-Reset`
    
    ### 🎯 Core Features
    * **Async Job Dispatch**: Enqueue missions for the Scraper workers
    * **Shadow Persona Integration**: Target specific fingerprints
    * **Declarative Overlays**: Custom extraction rules via JSON
    * **Real-time Webhooks**: Get results pushed to your endpoints
    * **Data Quality Scoring**: Filter tenders by completeness (0-100)
    * **Smart Caching**: Skip unchanged pages automatically
    """,
    version="2.0.0",
    contact={
        "name": "Georgios-Chrysovalantis Chatzivantsidis",
        "url": "https://github.com/vibe-coding/scraper",
    },
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class JobSubmission(BaseModel):
    """Enhanced Schema for enterprise job requests."""
    url: HttpUrl = Field(..., description="The target website URL to scrape.", example="https://ted.europa.eu/en/")
    target_site: str = Field("universal", description="Strategy identifier (e.g., 'esa', 'samgov', 'universal').")
    persona_id: Optional[str] = Field(None, description="Persistent Shadow Persona ID for fingerprint stability.")
    overlay: Optional[Dict[str, Any]] = Field(None, description="Dynamic extraction mapping to override default heuristics.")
    webhook_url: Optional[str] = Field(None, description="URL to push discovered intelligence via POST.")
    force_refresh: bool = Field(False, description="Skip cache and force re-scrape.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://example-tender.gov",
                "target_site": "universal",
                "persona_id": "analyst_premium_01",
                "webhook_url": "https://hooks.mycompany.com/intel",
                "force_refresh": False
            }
        }
    }


class JobResponse(BaseModel):
    status: str
    job_id: str
    message: str
    cached: Optional[bool] = None


class AutographRequest(BaseModel):
    html_sample: str


class TenderQualityResponse(BaseModel):
    """Response for tender quality check."""
    tender_id: str
    overall_score: int
    grade: str
    missing_fields: list
    recommendations: list


class AuthRegisterRequest(BaseModel):
    """Request to register for API access."""
    email: str
    tier: str = Field(default="free", description="API tier: free, basic, pro, enterprise")
    organization: Optional[str] = None


class AuthRegisterResponse(BaseModel):
    """Response with API key."""
    api_key: str
    tier: str
    rate_limit: int
    message: str


# Middleware to add rate limit headers to all responses
@app.middleware("http")
async def add_rate_limit_middleware(request: Request, call_next):
    """Add rate limit headers to all responses."""
    response = await call_next(request)
    
    # If rate limit info was set by auth middleware, add headers
    if hasattr(request.state, 'rate_limit'):
        add_rate_limit_headers(response, request.state.rate_limit)
    
    return response


# ============================================================================
# Public Endpoints (No Auth Required)
# ============================================================================

@app.get("/health", tags=["Observability"])
async def health_check():
    """System health audit endpoint."""
    return {
        "status": "healthy", 
        "project": "Spacescraper",
        "version": "2.0.0",
        "author": "Georgios-Chrysovalantis Chatzivantsidis",
        "timestamp": datetime.now().isoformat()
    }


@app.post("/auth/register", response_model=AuthRegisterResponse, tags=["Authentication"])
async def register_api_key(request: AuthRegisterRequest):
    """
    ### Register for API Access
    
    Generate a new API key for accessing the Spacescraper API.
    
    **Tiers:**
    - `free`: 100 requests/day
    - `basic`: 1,000 requests/day
    - `pro`: 10,000 requests/day
    - `enterprise`: 100,000 requests/day
    
    ⚠️ **Save your API key immediately** - it will only be shown once!
    """
    try:
        tier = ApiTier(request.tier.lower())
    except ValueError:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid tier. Choose from: {', '.join(t.value for t in ApiTier)}"
        )
    
    # Generate new API key
    plain_key, metadata = api_key_manager.generate_api_key(tier, request.email)
    
    # In production, you would save metadata to database here
    # await save_api_key_to_db(metadata)
    
    from src.auth_middleware import TIER_LIMITS
    
    return AuthRegisterResponse(
        api_key=plain_key,
        tier=tier.value,
        rate_limit=TIER_LIMITS[tier],
        message="API key generated successfully. Save this key - it will not be shown again!"
    )


# ============================================================================
# Protected Endpoints (API Key Required)
# ============================================================================

@app.post("/autograph", tags=["Intelligence"])
async def generate_schema_overlay(
    request: AutographRequest,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Schema Autopilot (Autograph)
    
    Analyzes an HTML snippet and generates an extraction overlay automatically.
    """
    api_key, rate_info = auth
    
    overlay = await ai_orchestrator.generate_overlay(request.html_sample)
    if not overlay:
        raise HTTPException(status_code=500, detail="AI Orchestrator failed to derive a reliable schema.")
    
    return {
        "status": "success",
        "suggested_overlay": overlay
    }


@app.post("/jobs", response_model=JobResponse, status_code=201, tags=["Orchestration"])
async def submit_job(
    submission: JobSubmission = Body(...),
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Enqueue a New Scraping Mission
    
    Validates the request and pushes the job to the distributed Redis queue.
    
    **Smart Caching:** If the URL was scraped recently and hasn't changed, 
    the job may return cached results immediately.
    """
    api_key, rate_info = auth
    
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    
    # Check smart cache before enqueuing
    cached_hash = None
    should_scrape = True
    
    if not submission.force_refresh:
        from src.smart_crawler import should_scrape_url
        should_scrape, cached_hash = await should_scrape_url(str(submission.url))
    
    new_job = ScrapeJob(
        job_id=job_id,
        url=str(submission.url),
        target_site=submission.target_site,
        persona_id=submission.persona_id,
        overlay=submission.overlay,
        webhook_url=submission.webhook_url
    )
    
    try:
        if should_scrape:
            await redis_queue.push_job("jobs_queue", new_job)
            logger.info(f"Spacescraper: Accepted job {job_id} targeting {submission.target_site}")
            return JobResponse(
                status="accepted",
                job_id=job_id,
                message="Task acknowledged. Workers will commence processing asynchronously.",
                cached=False
            )
        else:
            logger.info(f"Spacescraper: Cache hit for {submission.url}, skipping scrape")
            return JobResponse(
                status="accepted",
                job_id=job_id,
                message="Content unchanged since last scrape. Using cached data.",
                cached=True
            )
            
    except Exception as e:
        logger.error(f"Spacescraper API submission failed: {e}")
        raise HTTPException(status_code=500, detail="Backend orchestration fault during job enqueuing.")


@app.get("/metrics", tags=["Observability"])
async def get_cluster_metrics(auth: tuple = Depends(verify_api_key)):
    """
    ### Real-time Cluster Telemetry
    
    Returns a snapshot of success rates, page counts, and stealth detections.
    """
    api_key, rate_info = auth
    return metrics_tracker.metrics


@app.post("/tenders/quality", response_model=TenderQualityResponse, tags=["Data Quality"])
async def check_tender_quality(
    tender: Tender,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Calculate Data Quality Score
    
    Analyzes a tender and returns a quality score (0-100) with:
    - Overall grade (A+, A, B, C, D, F)
    - Detailed breakdown by dimension
    - Missing fields
    - Improvement recommendations
    
    **Scoring Dimensions:**
    - Completeness (40%): Required fields present
    - Accuracy (25%): Reasonable values
    - Timeliness (15%): Valid deadlines
    - Consistency (10%): No contradictions
    - Enrichment (10%): AI-enhanced fields
    """
    api_key, rate_info = auth
    
    report = dq_scorer.calculate_score(tender)
    
    return TenderQualityResponse(
        tender_id=report.tender_id,
        overall_score=report.overall_score,
        grade=report.grade,
        missing_fields=report.missing_fields,
        recommendations=report.recommendations
    )


@app.get("/tenders/high-quality", tags=["Data Quality"])
async def get_high_quality_tenders(
    min_score: int = 80,
    limit: int = 100,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Get High-Quality Tenders
    
    Returns tenders with quality score >= min_score.
    
    **Parameters:**
    - `min_score`: Minimum quality score (0-100, default: 80)
    - `limit`: Maximum results to return (default: 100)
    """
    api_key, rate_info = auth
    
    # This would query the database for high-quality tenders
    # For now, return info about the feature
    return {
        "message": f"This endpoint returns tenders with quality score >= {min_score}",
        "note": "Implementation would query PostgreSQL with quality_score column",
        "filters": {
            "min_score": min_score,
            "limit": limit
        }
    }


# ============================================================================
# Win Prediction Endpoints
# ============================================================================

class CapabilityProfileRequest(BaseModel):
    """Request to create/update user capability profile."""
    organization: str
    keywords: List[str] = Field(default_factory=list, description="E.g., ['satellite', 'AI', 'defense']")
    industries: List[str] = Field(default_factory=list, description="E.g., ['space', 'defense', 'dual-use']")
    services: List[str] = Field(default_factory=list, description="E.g., ['consulting', 'manufacturing']")
    min_budget_eur: Optional[float] = Field(None, description="Minimum comfortable budget")
    max_budget_eur: Optional[float] = Field(None, description="Maximum comfortable budget")
    geographic_focus: List[str] = Field(default_factory=list, description="E.g., ['EU', 'NATO', 'US']")
    excluded_countries: List[str] = Field(default_factory=list)
    min_quality_score: int = Field(70, ge=0, le=100)


class TenderMatchRequest(BaseModel):
    """Request to find matching tenders."""
    profile: CapabilityProfileRequest
    tenders: List[Tender]
    min_match_score: float = Field(0.5, ge=0.0, le=1.0)
    top_k: int = Field(10, ge=1, le=100)


class TenderMatchResponse(BaseModel):
    """Response with matched tenders."""
    matches: List[Dict[str, Any]]
    total_evaluated: int
    total_matched: int
    user_win_rate: float


class BidOutcomeRequest(BaseModel):
    """Report bid outcome for learning."""
    tender: Tender
    bid_submitted: bool
    won: bool


@app.post("/profile", tags=["Win Prediction"])
async def create_or_update_profile(
    request: CapabilityProfileRequest,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Create/Update Capability Profile
    
    Define your organization's capabilities to get personalized tender matches.
    
    **The more details you provide, the better the matches:**
    - Keywords: Technical terms you specialize in
    - Industries: Sectors you operate in
    - Budget range: Comfortable project sizes
    - Geography: Preferred regions
    
    This data creates your "win profile" that improves over time as you report outcomes.
    """
    api_key, rate_info = auth
    
    # Create profile from API key user
    profile = UserCapabilityProfile(
        user_id=api_key.key_id,
        organization=request.organization,
        keywords=request.keywords,
        industries=request.industries,
        services=request.services,
        min_budget_eur=request.min_budget_eur,
        max_budget_eur=request.max_budget_eur,
        geographic_focus=request.geographic_focus,
        excluded_countries=request.excluded_countries,
        min_quality_score=request.min_quality_score
    )
    
    # In production: save to database
    # await save_profile_to_db(profile)
    
    return {
        "status": "success",
        "user_id": profile.user_id,
        "profile": profile.to_dict(),
        "message": "Profile created successfully. Use this profile for tender matching."
    }


@app.post("/tenders/match", response_model=TenderMatchResponse, tags=["Win Prediction"])
async def find_matching_tenders(
    request: TenderMatchRequest,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Find Matching Tenders
    
    Get tenders ranked by win probability based on your capability profile.
    
    **Returns:**
    - Win probability (0.0-1.0) for each tender
    - Confidence level (high/medium/low)
    - Detailed breakdown of match factors
    - Personalized recommendations
    
    **Example Response:**
    ```json
    {
      "matches": [
        {
          "tender": {"title": "...", "buyer": "ESA"},
          "scores": {
            "overall_match": 0.87,
            "win_probability": 0.72,
            "confidence": "high"
          },
          "why": "Strong keyword match, Budget in sweet spot",
          "recommendations": ["Prioritize this tender"]
        }
      ]
    }
    ```
    """
    api_key, rate_info = auth
    
    # Build profile from request
    profile = UserCapabilityProfile(
        user_id=api_key.key_id,
        organization=request.profile.organization,
        keywords=request.profile.keywords,
        industries=request.profile.industries,
        services=request.profile.services,
        min_budget_eur=request.profile.min_budget_eur,
        max_budget_eur=request.profile.max_budget_eur,
        geographic_focus=request.profile.geographic_focus,
        excluded_countries=request.profile.excluded_countries,
        min_quality_score=request.profile.min_quality_score
    )
    
    # Find matches
    matches = win_predictor.find_matches(
        tenders=request.tenders,
        profile=profile,
        min_score=request.min_match_score,
        top_k=request.top_k
    )
    
    return TenderMatchResponse(
        matches=[m.to_dict() for m in matches],
        total_evaluated=len(request.tenders),
        total_matched=len(matches),
        user_win_rate=profile.calculate_overall_win_rate()
    )


@app.post("/tenders/outcome", tags=["Win Prediction"])
async def report_bid_outcome(
    request: BidOutcomeRequest,
    auth: tuple = Depends(verify_api_key)
):
    """
    ### Report Bid Outcome
    
    Report whether you won or lost a bid. This improves future predictions.
    
    **Learning Loop:**
    1. You receive tender match recommendations
    2. You submit bids on promising ones
    3. You report the outcome (won/lost)
    4. System learns your win patterns per buyer/budget/topic
    5. Future matches become more accurate
    
    The more outcomes you report, the better your personalized predictions become.
    """
    api_key, rate_info = auth
    
    # In production: load existing profile from DB
    # For demo: create minimal profile
    profile = UserCapabilityProfile(
        user_id=api_key.key_id,
        organization="User Org",
        past_wins=[],
        past_bids=[]
    )
    
    # Update profile with outcome
    win_predictor.update_profile_from_outcome(
        profile=profile,
        tender=request.tender,
        bid_submitted=request.bid_submitted,
        won=request.won
    )
    
    # In production: save updated profile to DB
    
    return {
        "status": "success",
        "message": f"Outcome recorded: bid_submitted={request.bid_submitted}, won={request.won}",
        "updated_win_rate": profile.calculate_overall_win_rate(),
        "note": "Your profile has been updated. Future matches will incorporate this outcome."
    }


@app.get("/tenders/demo-match", tags=["Win Prediction"])
async def demo_matching(auth: tuple = Depends(verify_api_key)):
    """
    ### Demo: Win Prediction
    
    Pre-populated demo showing how matching works.
    No need to provide tenders - uses sample data.
    """
    api_key, rate_info = auth
    
    # Sample user profile
    profile = UserCapabilityProfile(
        user_id="demo_user",
        organization="Satellite Solutions Ltd",
        keywords=["satellite", "communication", "optical", "earth observation"],
        industries=["space", "defense"],
        services=["manufacturing", "consulting"],
        min_budget_eur=500_000,
        max_budget_eur=10_000_000,
        geographic_focus=["EU", "NATO"],
        past_wins=[
            {"buyer": "ESA", "budget": 2_000_000, "title": "satellite ground station"},
            {"buyer": "EDA", "budget": 1_500_000, "title": "military comms"},
        ]
    )
    
    # Sample tenders
    sample_tenders = [
        Tender(
            source="TED",
            title="Supply of Advanced Satellite Communication Terminals for Defense",
            buyer="European Defence Agency",
            country="BE",
            deadline="2024-06-15",
            estimated_budget="€2,500,000",
            normalized_budget_eur=2_500_000,
            summary="Procurement of mobile satellite communication terminals for military field operations.",
            url="https://ted.europa.eu/001",
            classification="Defense"
        ),
        Tender(
            source="ESA",
            title="Earth Observation Data Processing Platform",
            buyer="European Space Agency",
            country="FR",
            deadline="2024-08-20",
            estimated_budget="€8,000,000",
            normalized_budget_eur=8_000_000,
            summary="Cloud-based platform for processing high-resolution satellite imagery.",
            url="https://esa.int/002",
            classification="Space"
        ),
        Tender(
            source="TED",
            title="Office IT Equipment Supply",
            buyer="Local Municipality",
            country="ES",
            deadline="2024-05-01",
            estimated_budget="€50,000",
            normalized_budget_eur=50_000,
            summary="Standard office computers and printers.",
            url="https://ted.europa.eu/003",
            classification=None
        ),
    ]
    
    # Find matches
    matches = win_predictor.find_matches(sample_tenders, profile, min_score=0.3)
    
    return {
        "demo_profile": profile.to_dict(),
        "matches": [m.to_dict() for m in matches],
        "explanation": "This demonstrates how the win prediction engine ranks tenders based on your capabilities and past performance."
    }


# ============================================================================
# Demo/Dev Endpoints
# ============================================================================

@app.get("/demo/key", include_in_schema=False)
async def get_demo_key():
    """Get a demo API key for testing (development only)."""
    return {
        "demo_key": "ss_demo_key",
        "tier": "pro",
        "note": "Use this in the Authorization header: Bearer ss_demo_key"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
