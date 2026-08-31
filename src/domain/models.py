# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Domain & Data Models)
# Role: Defines the core data structures used throughout the system.

from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, HttpUrl, ConfigDict
from datetime import datetime
from enum import Enum

# -----------------------------------------------------------------------------
# Job Lifecycle Models
# -----------------------------------------------------------------------------

class JobState(str, Enum):
    """Guarded state machine for job lifecycle."""
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD_LETTERED = "DEAD_LETTERED"

    def can_transition_to(self, target: "JobState") -> bool:
        """Validate state transitions according to the job lifecycle."""
        allowed: dict[JobState, set[JobState]] = {
            JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED, JobState.DEAD_LETTERED},
            JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
            JobState.SUCCEEDED: set(),
            JobState.FAILED: {JobState.QUEUED, JobState.DEAD_LETTERED},
            JobState.CANCELLED: set(),
            JobState.DEAD_LETTERED: set(),
        }
        return target in allowed.get(self, set())

class Job(BaseModel):
    """
    Durable job record with state machine enforcement.
    Tracks a single scraping job from submission through completion.
    """
    job_id: str = Field(..., description="Unique identifier for the job.")
    url: str = Field(..., description="Target URL to scrape.")
    target_site: str = Field("universal", description="Strategy identifier.")
    state: JobState = Field(default=JobState.QUEUED, description="Current job state.")
    priority: int = Field(default=0, description="Queue priority.")
    max_depth: int = Field(default=3, description="Maximum recursion depth.")
    overlay: Optional[Dict[str, Any]] = Field(None, description="Extraction overlay.")
    webhook_url: Optional[str] = Field(None, description="Result notification URL.")
    correlation_id: Optional[str] = Field(None, description="End-to-end correlation ID.")
    record_count: int = Field(default=0, description="Number of extracted records produced.")
    error_message: Optional[str] = Field(None, description="Last error detail, sanitized.")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def transition_to(self, new_state: JobState) -> "Job":
        """Return a new Job instance with the updated state, or raise on invalid transition."""
        if not self.state.can_transition_to(new_state):
            raise ValueError(
                f"Invalid state transition: {self.state.value} -> {new_state.value}"
            )
        return self.model_copy(update={
            "state": new_state,
            "updated_at": datetime.utcnow(),
        })

class JobAttempt(BaseModel):
    """Records a single execution attempt for a job."""
    attempt_id: str = Field(..., description="Unique attempt identifier.")
    job_id: str = Field(..., description="Parent job ID.")
    state: JobState = Field(default=JobState.RUNNING, description="Attempt state.")
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    worker_id: Optional[str] = Field(None, description="Worker node that ran this attempt.")
    error_message: Optional[str] = Field(None, description="Error detail if failed.")

# -----------------------------------------------------------------------------
# Discovery Models (Increment 7 — query to URLs)
# -----------------------------------------------------------------------------

class SearchHit(BaseModel):
    """
    A single search-engine result. Value Object: no identity, compared by value,
    frozen. snippet is content-hash side only — never an identity_hash input,
    so an LLM/search phrasing change can never trigger a false-discovery storm.
    """
    model_config = ConfigDict(frozen=True)

    url: str = Field(..., description="Result URL as returned by the search provider.")
    title: str = Field(..., description="Result title.")
    snippet: str = Field(default="", description="Result snippet/description. Data only, never a prompt directive.")
    rank: int = Field(..., description="Position in the search results (0-indexed).")
    provider: str = Field(..., description="Search provider that produced this hit (e.g. 'duckduckgo', 'serper').")


class ResearchPlan(BaseModel):
    """
    Durable record of a discovery request: a query plus the domain scope and
    fan-out budget it is allowed to run under. Replayable via serp_artifact_sha.
    """
    plan_id: str = Field(..., description="Unique identifier for the research plan.")
    query: str = Field(..., description="Sanitized search query.")
    max_results: int = Field(default=10, description="Maximum search hits to request.")
    allowed_domains: List[str] = Field(default_factory=list, description="Non-empty allowlist required to run.")
    serp_artifact_sha: Optional[str] = Field(None, description="Content-addressed SHA256 of the raw SERP, for replay.")
    state: JobState = Field(default=JobState.QUEUED, description="Plan lifecycle state, reusing JobState.")
    child_job_ids: List[str] = Field(default_factory=list, description="ScrapeJob IDs enqueued from this plan.")
    error_message: Optional[str] = Field(None, description="Refusal or failure reason, sanitized.")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# -----------------------------------------------------------------------------
# Queue Message Envelope (typed messages for Redis Streams)
# -----------------------------------------------------------------------------

class MessageType(str, Enum):
    """Types of messages flowing through the queue system."""
    SCRAPE_JOB = "scrape_job"
    RAW_PAYLOAD = "raw_payload"
    DISCOVERY_EVENT = "discovery_event"
    JOB_CANCEL = "job_cancel"
    DISCOVERY_QUERY = "discovery_query"

class QueueMessage(BaseModel):
    """
    Typed message envelope for Redis Streams.
    Every message flowing through the queue carries this envelope,
    ensuring traceability and idempotent processing.
    """
    message_id: str = Field(..., description="Unique UUID for deduplication.")
    message_type: MessageType = Field(..., description="Type discriminator for deserialization.")
    correlation_id: Optional[str] = Field(None, description="End-to-end trace ID.")
    root_job_id: Optional[str] = Field(None, description="Original root job for fan-out tracking.")
    schema_version: str = Field("1.0", description="Envelope schema version for migration.")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Serialized message payload.")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    retry_count: int = Field(default=0, description="Number of delivery attempts so far.")
    max_retries: int = Field(default=3, description="Max attempts before dead-letter.")

# -----------------------------------------------------------------------------
# Outbox Event Models (reliable event delivery)
# -----------------------------------------------------------------------------

class OutboxStatus(str, Enum):
    """Delivery status for outbox events."""
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"

class OutboxEvent(BaseModel):
    """
    An event in the outbox for reliable delivery.
    Created atomically with the originating transaction,
    then relayed to Valkey Streams by the OutboxRelay.
    """
    event_id: str = Field(..., description="Unique idempotency key.")
    aggregate_type: str = Field(..., description="Aggregate root type (e.g. 'job', 'record').")
    aggregate_id: str = Field(..., description="Aggregate root ID.")
    event_type: str = Field(..., description="Event type (e.g. 'job.submitted', 'job.completed').")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event payload data.")
    status: OutboxStatus = Field(default=OutboxStatus.PENDING)
    retry_count: int = Field(default=0)
    max_retries: int = Field(default=10)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

# -----------------------------------------------------------------------------
# Extraction Schema & Overlay Models
# -----------------------------------------------------------------------------

class FieldDefinition(BaseModel):
    """Definition of a single field in an extraction schema."""
    name: str = Field(..., description="Field name in the extracted data.")
    field_type: str = Field("string", description="Expected type: string, number, boolean, url.")
    required: bool = Field(default=False, description="Whether this field must be present.")
    description: Optional[str] = Field(None, description="Semantic description of the field.")
    selector: Optional[str] = Field(None, description="CSS/XPath selector hint.")
    identity: bool = Field(default=False, description="Whether this field contributes to identity_hash.")

class ExtractionSchema(BaseModel):
    """
    Schema definition for extracted records.
    Validates that extracted data conforms to expected fields and types.
    """
    schema_id: str = Field(..., description="Unique schema identifier.")
    schema_version: str = Field("1.0", description="Schema version for migration.")
    record_type: str = Field("generic", description="Type tag for records using this schema.")
    fields: List[FieldDefinition] = Field(default_factory=list, description="Allowed field definitions.")
    quality_rules: Dict[str, Any] = Field(default_factory=dict, description="Quality constraints (min_length, ranges, patterns).")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def validate_record(self, data: Dict[str, Any]) -> List[str]:
        """Validate data against the schema. Returns list of validation errors."""
        errors = []
        for field in self.fields:
            value = data.get(field.name)
            if field.required and value is None:
                errors.append(f"Missing required field: {field.name}")
                continue
            if value is not None:
                if field.field_type == "number":
                    try:
                        float(str(value).replace(",", "").replace("€", "").replace("$", ""))
                    except (ValueError, TypeError):
                        errors.append(f"Field '{field.name}' should be numeric, got '{value}'")
                elif field.field_type == "url" and not str(value).startswith(("http://", "https://")):
                    errors.append(f"Field '{field.name}' should be a URL, got '{value}'")
        return errors

class OverlayState(str, Enum):
    """Lifecycle states for extraction overlays."""
    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    RETIRED = "RETIRED"

class ExtractionOverlay(BaseModel):
    """
    Declarative extraction mapping for a specific domain/page type.
    Versioned and transitioned through lifecycle states.
    """
    overlay_id: str = Field(..., description="Unique overlay identifier.")
    domain: str = Field(..., description="Target domain pattern (e.g. 'books.toscrape.com').")
    schema_id: str = Field(..., description="Linked ExtractionSchema ID.")
    state: OverlayState = Field(default=OverlayState.CANDIDATE)
    version: int = Field(default=1, description="Monotonic version number.")
    container_selector: Optional[str] = Field(None, description="CSS selector for item containers.")
    field_mappings: Dict[str, str] = Field(default_factory=dict, description="Field name -> CSS selector.")
    author: Optional[str] = Field(None, description="Who created this overlay.")
    source_evidence: Optional[str] = Field(None, description="URL or reference justifying this overlay.")
    rollback_overlay_id: Optional[str] = Field(None, description="Previous version for rollback.")
    validation_result: Optional[str] = Field(None, description="Summary of validation suite results.")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# -----------------------------------------------------------------------------
# Learning & Evaluation Models (Increment 5)
# -----------------------------------------------------------------------------

class StrategyObservation(BaseModel):
    """
    Immutable observation recorded for each extraction attempt.
    Used for offline evaluation and strategy selection.
    """
    observation_id: str = Field(..., description="Unique observation ID.")
    job_id: str = Field(..., description="Job that produced this observation.")
    domain: str = Field(..., description="Target domain observed.")
    strategy: str = Field(..., description="Strategy used: 'http', 'browser', 'overlay', 'json_ld', 'semantic_html'.")
    overlay_id: Optional[str] = Field(None, description="Overlay version if overlay strategy used.")
    input_fingerprint: Optional[str] = Field(None, description="Hash of input page structure.")
    valid_record_count: int = Field(default=0, description="Records passing schema validation.")
    required_field_completeness: float = Field(default=0.0, description="Fraction of required fields populated (0-1).")
    duplicate_rate: float = Field(default=0.0, description="Fraction of records that were duplicates (0-1).")
    http_status: Optional[int] = Field(None, description="HTTP status code from fetch.")
    blocked: bool = Field(default=False, description="Whether the request was blocked/challenged.")
    latency_ms: float = Field(default=0.0, description="End-to-end latency in milliseconds.")
    cost: float = Field(default=0.0, description="Estimated monetary cost (AI tokens, browser seconds).")
    success: bool = Field(default=False, description="Whether extraction succeeded.")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FeedbackItem(BaseModel):
    """
    User feedback on an extracted record.
    Stored as labeled training data, not immediate instructions.
    """
    feedback_id: str = Field(..., description="Unique feedback ID.")
    record_id: str = Field(..., description="The record this feedback applies to.")
    job_id: str = Field(..., description="Job that produced the record.")
    decision: str = Field(..., description="'accepted', 'rejected', or 'corrected'.")
    corrected_data: Optional[Dict[str, Any]] = Field(None, description="User-provided corrected data.")
    reason: Optional[str] = Field(None, description="Reason for rejection or correction.")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class EvaluationResult(BaseModel):
    """
    Result of comparing a candidate strategy/overlay against a baseline.
    Produced by the offline evaluator.
    """
    evaluation_id: str = Field(..., description="Unique evaluation ID.")
    candidate_strategy: str = Field(..., description="Strategy being evaluated.")
    baseline_strategy: str = Field("active", description="Baseline strategy to compare against.")
    domain: str = Field(..., description="Domain evaluated.")
    sample_size: int = Field(default=0, description="Number of observations used.")
    precision: float = Field(default=0.0, description="Fraction of valid records (0-1).")
    completeness: float = Field(default=0.0, description="Required field completeness (0-1).")
    latency_p50: float = Field(default=0.0)
    latency_p95: float = Field(default=0.0)
    cost_per_record: float = Field(default=0.0)
    block_rate: float = Field(default=0.0)
    score: float = Field(default=0.0, description="Composite utility score.")
    recommendation: Optional[str] = Field(None, description="'promote', 'demote', 'no_change'.")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class DomainProfile(BaseModel):
    """Per-domain profile tracking preferred strategies and observed behavior."""
    domain: str = Field(..., description="The domain this profile describes.")
    preferred_strategy: str = Field("http", description="Best-performing strategy for this domain.")
    overlay_id: Optional[str] = Field(None, description="Currently ACTIVE overlay ID.")
    success_rate: float = Field(default=0.0, description="Historical extraction success rate (0-1).")
    total_observations: int = Field(default=0, description="Total observation count.")
    avg_latency_ms: float = Field(default=0.0, description="Average latency.")
    block_rate: float = Field(default=0.0, description="Block/challenge rate (0-1).")
    last_observed: Optional[datetime] = None
    profile_version: int = Field(default=1, description="Increment on significant changes.")

# -----------------------------------------------------------------------------
# Core Orchestration Models
# -----------------------------------------------------------------------------

class ScrapeJob(BaseModel):
    """
    Spacescraper Task Definition.
    Represents a single scraping intent published by the controller.
    """
    job_id: str = Field(..., description="Unique UUID for lifecycle tracking.")
    url: str = Field(..., description="Destination URL for the browser session.")
    target_site: str = Field(..., description="Strategy identifier (e.g. 'amazon', 'generic', 'esa_emits').")
    priority: int = Field(default=0, description="Heuristic for queue prioritization.")
    use_stealth: bool = Field(default=True, description="Enable anti-bot bypass mechanisms.")
    use_proxy: bool = Field(default=True, description="Enable rotation through proxy gateway.")
    depth: int = Field(default=0, description="Current recursion depth.")
    max_depth: int = Field(default=3, description="Maximum allowed discovery depth.")
    persona_id: Optional[str] = Field(None, description="Persistent Shadow Persona ID.")
    overlay: Optional[Dict[str, Any]] = Field(None, description="Declarative extraction mapping.")
    webhook_url: Optional[str] = Field(None, description="Optional outbound webhook notification endpoint.")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Creation UTC timestamp.")

class RawScrapePayload(BaseModel):
    """
    Spacescraper Raw Shipment.
    Produced by Scraper Workers; contains the raw HTML and intercepted JSON data.
    """
    job_id: str
    target_site: str
    url: str
    status_code: int
    html_content: Optional[str] = None
    json_payloads: List[Dict[str, Any]] = Field(default_factory=list, description="Intercepted XHR/Fetch network traffic.")
    depth: int = Field(default=0, description="Linage depth of the source job.")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None
    overlay: Optional[Dict[str, Any]] = Field(None, description="Extraction overlay mapping.")
    webhook_url: Optional[str] = Field(None, description="Result notification endpoint.")
    persona_id: Optional[str] = Field(None, description="Persistent browser persona ID.")

# -----------------------------------------------------------------------------
# Generic Extracted Record (replaces domain-specific entities)
# -----------------------------------------------------------------------------

class ChangeType(str, Enum):
    """State transitions for extracted records."""
    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"

class ExtractedRecord(BaseModel):
    """
    Generic extracted data record.
    Replaces Opportunity, Product, Lead, and Article domain-specific entities.
    """
    record_id: str = Field(..., description="Stable unique identifier for this record.")
    record_type: str = Field("generic", description="Type tag for the record (e.g. 'product', 'listing', 'article').")
    schema_version: str = Field("1.0", description="Version of the extraction schema used.")
    canonical_url: Optional[str] = Field(None, description="Canonical URL for deduplication.")
    source_url: str = Field(..., description="Original URL the data was parsed from.")
    data: Dict[str, Any] = Field(default_factory=dict, description="Extracted field data, validated against schema.")
    identity_hash: Optional[str] = Field(None, description="Stable hash from raw pre-AI fields for change detection.")
    content_hash: Optional[str] = Field(None, description="Hash for full-content state tracking.")
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    change_type: ChangeType = Field(default=ChangeType.NEW, description="State: NEW, UPDATED, UNCHANGED.")
    extracted_at: datetime = Field(default_factory=datetime.utcnow)

# -----------------------------------------------------------------------------
# Legacy Domain Entities (deprecated — use ExtractedRecord for new code)
# -----------------------------------------------------------------------------

class BaseEntity(BaseModel):
    """Functional root for all extracted entities in the Spacescraper ecosystem."""
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    source_url: str = Field(..., description="Original URL the data was parsed from.")

class Product(BaseEntity):
    """ Retail/E-Commerce Data Model. (Deprecated — use ExtractedRecord) """
    id: str = Field(..., description="Primary identifier (SKU, ASIN, or Heuristic ID).")
    name: str = Field(..., description="Product Headline/Title.")
    price: Optional[float] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    image_url: Optional[str] = None
    is_out_of_stock: bool = Field(default=False)
    description: Optional[str] = None
    material: Optional[str] = None
    category: Optional[str] = None
    url: str = Field(..., description="Canonical product link.")

class Lead(BaseEntity):
    """ B2B Intelligence Model. (Deprecated — use ExtractedRecord) """
    name: str
    job_title: Optional[str] = None
    company: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    lead_score: Optional[int] = None

class Article(BaseEntity):
    """ Content & Media Model. (Deprecated — use ExtractedRecord) """
    title: str
    author: Optional[str] = None
    publish_date: Optional[str] = None
    category: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    url: str

class Opportunity(BaseEntity):
    """
    Spacescraper Procurement Intelligence. (Deprecated — use ExtractedRecord)
    High-fidelity model for Space & Defense opportunities.
    """
    source: str = Field(..., description="Origin portal (e.g., ESA, NATO, SamGov).")
    external_id: Optional[str] = Field(None, description="Official reference/opportunity ID.")
    title: str = Field(..., description="Procurement headline.")
    buyer: Optional[str] = Field(None, description="Issuing organization.")
    country: Optional[str] = Field(None, description="Target country/region.")
    publication_date: Optional[str] = None
    deadline: Optional[str] = None
    estimated_budget: Optional[str] = None
    currency: Optional[str] = Field(default="EUR")
    status: Optional[str] = Field(default="OPEN")
    url: str = Field(..., description="Direct link to opportunity.")

    # Enrichment fields (Translation & ML)
    summary: Optional[str] = Field(None, description="LLM generated summary.")
    normalized_budget_eur: Optional[float] = Field(None, description="Budget converted to EUR.")

    # Metadata & Tracking
    content_hash: Optional[str] = Field(None, description="Hash for state tracking.")
    identity_hash: Optional[str] = Field(None, description="Stable hash from raw pre-AI fields for change detection.")
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    change_type: str = Field(default="NEW", description="State: NEW, UPDATED, UNCHANGED.")
    duplicate_group_id: Optional[str] = Field(None, description="Clustering ID for fuzzy matches.")

    # Classification (Bonus)
    classification: Optional[str] = Field(None, description="Space, Defense, or Dual-use.")

class FollowLink(BaseEntity):
    """ Discovery Metadata for recursive crawling. """
    url: str
    target_site: str
    priority: int = 0
    depth: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)

# -----------------------------------------------------------------------------
# Pipeline Outputs
# -----------------------------------------------------------------------------

class ProcessingResult(BaseModel):
    """ Consolidated package after extraction and enrichment. """
    job_id: str
    success: bool
    entities: List[Union[Product, Lead, Article, Opportunity, FollowLink, dict]] = []
    follow_urls: List[Dict[str, Any]] = Field(default_factory=list, description="Discovery pointers with depth metadata.")
    error: Optional[str] = None

class DiscoveryEvent(BaseModel):
    """
    Spacescraper Signal.
    Emitted when new high-value intelligence is discovered.
    """
    event_id: str = Field(default_factory=lambda: f"ev_{datetime.utcnow().timestamp()}")
    job_id: str
    target_site: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    new_count: int
    updated_count: int
    entities: List[Opportunity] = [] # Focused on procurement for current iteration
