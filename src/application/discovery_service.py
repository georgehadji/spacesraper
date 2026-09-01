# Author: Spacescraper (Application — Discovery)
# Role: Query-to-URL discovery. A stage in FRONT of the pipeline — the
# scraper, processor, and reporter are untouched and never learn a job
# originated from a search result.
#
# Pipeline (Pipeline pattern — a filter chain, each step independently
# testable, no mocking required per filter):
#   hits -> dedup_by_canonical_url -> UrlPolicy.is_allowed -> validate_outbound_url
#        -> SmartCrawler cache check -> fan-out budget -> ScrapeJob[]
#
# Every rejection is counted by reason and logged structurally — a silently
# dropped URL is a bug (matches the B6 fail-open lesson).

import logging
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from src.domain.models import ResearchPlan, ScrapeJob, SearchHit
from src.domain.exceptions import DiscoveryRefusedError, SSRFGuardError
from src.domain.ports import SearchProvider
from src.security.url_policy import UrlPolicy
from src.security.ssrf_guard import validate_outbound_url

logger = logging.getLogger("Spacescraper.Discovery")

# Discovery cap is well below the crawl-recursion cap (200) — start at 25.
DEFAULT_DISCOVERY_MAX_FANOUT = 25


class DiscoveryService:
    """
    Orchestrates a discovery run: query -> search hits -> filtered, budgeted
    ScrapeJobs. Depends only on ports (SearchProvider, UrlPolicy) and the
    queue's fan-out budget check, so it is constructible with mocks and no
    network for unit tests.
    """

    def __init__(
        self,
        search_provider: SearchProvider,
        url_policy: UrlPolicy,
        queue,  # RedisQueueWorker-shaped: get_allowed_fanout(root_id, requested, max_fanout)
        smart_crawler=None,  # Optional[SmartCrawler]; cache check skipped if not provided
        discovery_max_fanout: int = DEFAULT_DISCOVERY_MAX_FANOUT,
    ):
        self.search_provider = search_provider
        self.url_policy = url_policy
        self.queue = queue
        self.smart_crawler = smart_crawler
        self.discovery_max_fanout = discovery_max_fanout

    async def discover(self, plan: ResearchPlan) -> Tuple[List[ScrapeJob], Dict[str, int]]:
        """
        Run the full discovery pipeline for a plan.

        Returns (scrape_jobs, rejection_counts). Never silently drops a hit —
        every filtered-out URL is attributed to a reason in rejection_counts
        and logged at INFO.

        Raises DiscoveryRefusedError if the plan has no allowlist configured —
        search must never be allowed to target arbitrary hosts by default.
        """
        if not plan.allowed_domains:
            raise DiscoveryRefusedError(
                "Discovery requires a non-empty allowlist; refusing to run unscoped search.",
                code="DISCOVERY_EMPTY_ALLOWLIST",
            )

        rejections: Dict[str, int] = defaultdict(int)

        hits = await self.search_provider.search(plan.query, max_results=plan.max_results)
        logger.info("Discovery plan %s: %d raw hits for query", plan.plan_id, len(hits))

        deduped = self._dedup_by_canonical_url(hits)
        rejections["duplicate"] = len(hits) - len(deduped)

        survivors: List[SearchHit] = []
        for hit in deduped:
            allowed, reason = await self.url_policy.is_allowed(hit.url, trust_level="untrusted")
            if not allowed:
                rejections["policy_denied"] += 1
                logger.info("Discovery plan %s: rejected %s (%s)", plan.plan_id, hit.url, reason)
                continue

            try:
                validate_outbound_url(hit.url)
            except SSRFGuardError as e:
                rejections["ssrf_blocked"] += 1
                logger.warning("Discovery plan %s: SSRF-blocked %s (%s)", plan.plan_id, hit.url, e)
                continue

            if self.smart_crawler is not None:
                try:
                    cache_result = await self.smart_crawler.check_cache(hit.url)
                    if not cache_result.should_scrape:
                        rejections["cache_fresh"] += 1
                        continue
                except Exception as e:
                    # Cache check is an optimization, never a gate — fail open here only.
                    logger.debug("Discovery plan %s: cache check failed for %s (%s)", plan.plan_id, hit.url, e)

            survivors.append(hit)

        allowed_count = await self.queue.get_allowed_fanout(
            plan.plan_id, len(survivors), self.discovery_max_fanout
        )
        if allowed_count < len(survivors):
            rejections["fanout_budget_exceeded"] = len(survivors) - allowed_count
            logger.warning(
                "Discovery plan %s: fan-out budget capped %d of %d hits",
                plan.plan_id, allowed_count, len(survivors),
            )
        budgeted = survivors[:allowed_count]

        scrape_jobs = [self._to_scrape_job(hit, plan) for hit in budgeted]

        for reason, count in rejections.items():
            if count:
                logger.info("Discovery plan %s: rejected %d hits (%s)", plan.plan_id, count, reason)

        return scrape_jobs, dict(rejections)

    def _dedup_by_canonical_url(self, hits: List[SearchHit]) -> List[SearchHit]:
        """Drop hits sharing a canonical URL, keeping the highest-ranked (lowest rank number)."""
        seen: Dict[str, SearchHit] = {}
        for hit in hits:
            canonical = self._canonicalize(hit.url)
            existing = seen.get(canonical)
            if existing is None or hit.rank < existing.rank:
                seen[canonical] = hit
        # Preserve original rank order
        return sorted(seen.values(), key=lambda h: h.rank)

    @staticmethod
    def _canonicalize(url: str) -> str:
        """
        Normalize a URL for dedup: http/https treated as the same resource,
        lowercase host, strip fragment and trailing slash. Scheme collapsing
        is dedup-key-only — the original hit.url (with its real scheme) is
        what's actually enqueued and fetched, this never touches that.
        """
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme in ("http", "https"):
            scheme = "http"
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((scheme, netloc, path, "", parsed.query, ""))

    @staticmethod
    def _to_scrape_job(hit: SearchHit, plan: ResearchPlan) -> ScrapeJob:
        return ScrapeJob(
            job_id=f"disc_{uuid.uuid4().hex[:8]}",
            url=hit.url,
            target_site="universal",
            depth=0,
            max_depth=3,
        )
