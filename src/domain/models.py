# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Domain & Data Models)
# Role: Defines the core data structures used throughout the system.

from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, HttpUrl
from datetime import datetime
from enum import Enum

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
