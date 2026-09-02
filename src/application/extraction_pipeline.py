# Merged extraction pipeline — v2.
# Wires the strategy dispatch chain into the deterministic extraction pipeline.
#
# Strategy dispatch priority:
#   1. page_fields (user-specified OverrideStrategy)     — highest
#   2. Google Maps Place page (GoogleMapsPlaceStrategy)
#   3. Google Maps Search (GoogleMapsStrategy)            — domain-specific
#   4. ExtractionOverlay (declarative field mappings)     — declarative
#   5. JSON-LD structured data                            — generic fallbacks
#   6. Semantic HTML patterns (articles, lists, tables)
#
# A failed optional stage does not erase results from earlier stages.

import hashlib
import json
import logging
import uuid

from scrapling import Selector

from src.application.deduplicator import Deduplicator
from src.application.link_discovery import extract_links, find_next_page_url
from src.domain.models import ExtractedRecord, ExtractionOverlay, ExtractionSchema, ProcessingResult, RawScrapePayload
from src.domain.ports import OverlayRepository
from src.domain.similarity import find_best_relocation
from src.extractors.base_extractor import BaseExtractionStrategy
from src.extractors.strategies import GenericStrategy, GoogleMapsPlaceStrategy, GoogleMapsStrategy, OverrideStrategy

logger = logging.getLogger("Spacescraper.ExtractionPipeline")

# P7.3: the old list rule fired on any <ul>/<ol> with >= 3 <li> — which is
# every navigation menu, footer link block, and breadcrumb on the web.
_LIST_NOISE_ANCESTOR_TAGS = ("nav", "footer", "header", "aside")

# P7.2: content-shape patterns for the last-resort, content-addressed stage.
# Deliberately small — this is a fallback before escalating to an LLM, not a
# general-purpose content classifier.
_CONTENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("price", r"[$£€]\s?\d[\d,]*\.?\d*"),
    (
        "date",
        r"\b\d{4}-\d{2}-\d{2}\b"
        r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b",
    ),
)


def _text(el) -> str:
    """bs4 get_text(strip=True)-equivalent: all descendant text, concatenated
    with no separator, each fragment stripped."""
    return el.get_all_text(separator="", strip=True)


def _build_element_signature(el: Selector) -> dict:
    """A4: a redundant, comparable snapshot of an element's position and
    shape — tag, attrs, own text, ancestor path, parent, siblings, children.
    No single field is trusted; score_similarity weighs all of them so a
    class rename or DOM reshuffle doesn't sink the whole match."""
    parent = el.parent
    ancestor_tags: list[str] = []
    node = parent
    depth = 0
    while node is not None and depth < 30:
        ancestor_tags.append(node.tag)
        node = node.parent
        depth += 1
    return {
        "tag": el.tag,
        "attrs": dict(el.attrib),
        "text": _text(el),
        "ancestor_tags": ancestor_tags,
        "parent_tag": parent.tag if parent is not None else None,
        "parent_attrs": dict(parent.attrib) if parent is not None else {},
        "parent_text": _text(parent) if parent is not None else "",
        "sibling_tags": [c.tag for c in parent.children] if parent is not None else [],
        "child_tags": [c.tag for c in el.children],
    }


def _is_bare_link_item(li) -> bool:
    """An <li> that is just a link wrapping its own text — the shape of a
    nav-menu/breadcrumb entry, not real list content."""
    links = li.css("a")
    if len(links) != 1:
        return False
    return _text(li) == _text(links[0])


class DeterministicExtractionPipeline(BaseExtractionStrategy):
    """
    Merged strategy chain for extraction.

    Strategy dispatch:
      - page_fields on the job submission? → OverrideStrategy (user mappings)
      - URL matches google.com/maps/place/? → GoogleMapsPlaceStrategy
      - URL matches google.com/maps/search? → GoogleMapsStrategy
      - ExtractionOverlay exists for domain? → overlay extraction
      - Fall through → JSON-LD → Semantic HTML
    """

    def __init__(self, overlay_repo: OverlayRepository | None = None):
        self.overlay_repo = overlay_repo
        self._generic = GenericStrategy()
        self._override = OverrideStrategy()
        self._gm = GoogleMapsStrategy()
        self._gm_place = GoogleMapsPlaceStrategy()

    # ------------------------------------------------------------------
    # Main dispatch entry point
    # ------------------------------------------------------------------

    async def extract(
        self,
        html: str,
        json_payloads: list[dict],
        current_url: str = "",
        overlay: dict | None = None,
        schema: ExtractionSchema | None = None,
    ) -> list[ExtractedRecord]:
        """Run the full strategy chain. Returns validated records."""
        soup = Selector(html)
        all_records: list[ExtractedRecord] = []

        # -----------------------------------------------------------------
        # Stage A: page_fields override — user-specified selectors win
        # -----------------------------------------------------------------
        if isinstance(overlay, dict) and overlay.get("mappings"):
            records = await self._override.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): Override produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage B: Google Maps Place detail page
        # -----------------------------------------------------------------
        if self._gm_place.matches_domain(current_url):
            records = await self._gm_place.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): GoogleMapsPlace produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage C: Google Maps Search results
        # -----------------------------------------------------------------
        if self._gm.matches_domain(current_url):
            records = await self._gm.extract(html, json_payloads, current_url, overlay, schema)
            if records:
                validated = self._validate_records(records, schema)
                logger.info("Pipeline(dispatch): GoogleMapsSearch produced %d records", len(validated))
                return validated

        # -----------------------------------------------------------------
        # Stage D: Overlay (declarative, domain-specific)
        # -----------------------------------------------------------------
        overlay_records = await self._try_overlay(soup, current_url, overlay)
        if overlay_records:
            validated = self._validate_records(overlay_records, schema)
            logger.info("Pipeline(dispatch): Overlay produced %d valid records", len(validated))
            return validated  # overlay is authoritative when present

        # -----------------------------------------------------------------
        # Stage E: JSON-LD
        # -----------------------------------------------------------------
        json_ld_records = self._extract_json_ld(soup, current_url)
        if json_ld_records:
            validated = self._validate_records(json_ld_records, schema)
            all_records.extend(validated)

        # -----------------------------------------------------------------
        # Stage F: Semantic HTML (articles, lists, tables)
        # -----------------------------------------------------------------
        if not json_ld_records:
            html_records = self._extract_semantic_html(soup, current_url)
            if html_records:
                validated = self._validate_records(html_records, schema)
                all_records.extend(validated)

        # -----------------------------------------------------------------
        # Stage G: Structured markup (Microdata / RDFa / OpenGraph) — P7.2
        # -----------------------------------------------------------------
        if not all_records:
            structured_records = self._extract_structured_markup(soup, current_url)
            if structured_records:
                validated = self._validate_records(structured_records, schema)
                all_records.extend(validated)

        # -----------------------------------------------------------------
        # Stage H: Content-addressed (regex shape -> similar peers) — P7.2
        # -----------------------------------------------------------------
        if not all_records:
            content_records = await self._extract_content_addressed(soup, current_url)
            if content_records:
                validated = self._validate_records(content_records, schema)
                all_records.extend(validated)

        if not all_records:
            logger.debug("Pipeline(dispatch): No records extracted from %s", current_url)

        return all_records

    # ------------------------------------------------------------------
    # Overlay (declarative extraction)
    # ------------------------------------------------------------------

    async def _try_overlay(
        self, soup: Selector, current_url: str, overlay: dict | None
    ) -> list[ExtractedRecord]:
        """Try running an overlay, either explicit or from the repository."""
        if overlay:
            return self._apply_overlay_dict(soup, current_url, overlay)
        if self.overlay_repo:
            from urllib.parse import urlparse
            domain = urlparse(current_url).netloc
            active = await self.overlay_repo.get_active_overlay(domain)
            if active and active.field_mappings:
                return await self._apply_field_mappings(soup, current_url, active)
        return []

    def _apply_overlay_dict(
        self, soup: Selector, current_url: str, overlay: dict
    ) -> list[ExtractedRecord]:
        """Apply an inline overlay dictionary directly."""
        container_selector = overlay.get("container_selector")
        field_mappings = overlay.get("field_mappings", {})
        if not field_mappings:
            return []
        containers = soup.css(container_selector) if container_selector else [soup]
        records: list[ExtractedRecord] = []
        for el in containers:
            data: dict[str, object] = {}
            for field, selector in field_mappings.items():
                found = el.css(selector).first
                if found:
                    data[field] = _text(found)
            if data:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="generic",
                    data=data,
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)
        return records

    async def _apply_field_mappings(
        self, soup: Selector, current_url: str, overlay: ExtractionOverlay
    ) -> list[ExtractedRecord]:
        """Apply an ExtractionOverlay from the repository.

        A4: a per-field selector miss with a captured signature for that
        field triggers relocation — score every same-tag element in the
        container against the stored signature and, above threshold, use
        its value. Recovered fields are proposed back as a new CANDIDATE
        overlay (never mutating the ACTIVE one directly), same promotion
        gate as any other synthesized overlay.
        """
        cs = overlay.container_selector
        containers = soup.css(cs) if cs else [soup]
        records: list[ExtractedRecord] = []
        relocated: dict[str, Selector] = {}
        for el in containers:
            data: dict[str, object] = {}
            for field, selector in overlay.field_mappings.items():
                found = el.css(selector).first
                if found:
                    data[field] = _text(found)
                    continue
                signature = overlay.field_signatures.get(field)
                if not signature or field in relocated:
                    continue
                candidate_els = el.css(signature.get("tag", "*"))
                candidates = [(str(i), _build_element_signature(c)) for i, c in enumerate(candidate_els)]
                match = find_best_relocation(signature, candidates)
                if match:
                    cand_el = candidate_els[int(match[0])]
                    data[field] = _text(cand_el)
                    relocated[field] = cand_el
            if data:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="generic",
                    data=data,
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)
        if relocated:
            await self._persist_relocated_overlay(overlay, relocated, current_url)
        return records

    async def _persist_relocated_overlay(
        self, source: ExtractionOverlay, relocated: dict[str, Selector], current_url: str
    ) -> None:
        """A4: propose a regenerated selector for each relocated field as a
        new CANDIDATE overlay, keeping every other field mapping/signature
        from `source` unchanged. Never fatal, never touches ACTIVE state —
        same evidence-gated promotion as any other synthesized overlay."""
        if not self.overlay_repo:
            return
        field_mappings = dict(source.field_mappings)
        field_signatures = dict(source.field_signatures)
        for field, el in relocated.items():
            classes = el.attrib.get("class", "").split()
            # ponytail: same crude tag(+first class) seed as _synthesize_overlay —
            # shadow evaluation scores it before it can reach ACTIVE.
            field_mappings[field] = f"{el.tag}.{classes[0]}" if classes else el.tag
            field_signatures[field] = _build_element_signature(el)
        overlay = ExtractionOverlay(
            overlay_id=f"ovl_{uuid.uuid4().hex[:12]}",
            domain=source.domain,
            schema_id=source.schema_id,
            container_selector=source.container_selector,
            field_mappings=field_mappings,
            field_signatures=field_signatures,
            source_evidence=current_url,
            rollback_overlay_id=source.overlay_id,
        )
        try:
            await self.overlay_repo.create_overlay(overlay)
            logger.info(
                "Pipeline(A4): relocated %s on %s, proposed CANDIDATE overlay %s",
                sorted(relocated.keys()), source.domain, overlay.overlay_id,
            )
        except Exception:
            logger.warning("Pipeline(A4): failed to persist relocated overlay for %s", source.domain, exc_info=True)

    # ------------------------------------------------------------------
    # JSON-LD extraction
    # ------------------------------------------------------------------

    def _extract_json_ld(self, soup: Selector, current_url: str) -> list[ExtractedRecord]:
        """Parse JSON-LD script tags into ExtractedRecords."""
        records: list[ExtractedRecord] = []
        for script in soup.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.text)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # Expand @graph into individual records
                    graph = item.get("@graph")
                    if isinstance(graph, list):
                        for graph_item in graph:
                            if isinstance(graph_item, dict):
                                records.append(self._make_json_ld_record(graph_item, current_url))
                    else:
                        records.append(self._make_json_ld_record(item, current_url))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return records

    def _make_json_ld_record(self, item: dict, current_url: str) -> ExtractedRecord:
        """Create an ExtractedRecord from a JSON-LD item."""
        record = ExtractedRecord(
            record_id=f"rec_{uuid.uuid4().hex[:12]}",
            record_type=item.get("@type", "structured_data").lower(),
            data=item,
            source_url=current_url,
        )
        record.compute_identity_hash()
        return record

    # ------------------------------------------------------------------
    # Semantic HTML extraction
    # ------------------------------------------------------------------

    def _extract_semantic_html(self, soup: Selector, current_url: str) -> list[ExtractedRecord]:
        """Extract generic semantic HTML patterns (articles, tables, lists)."""
        records: list[ExtractedRecord] = []

        # Articles
        for article in soup.css("article"):
            title = article.find(["h1", "h2", "h3"])
            text = _text(article)
            if text and len(text) > 50:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="article",
                    data={
                        "title": _text(title) if title else "",
                        "text": text[:5000],
                    },
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)

        # Tables
        for table in soup.css("table"):
            headers = [_text(th) for th in table.css("th")]
            rows: list[list[str]] = []
            for tr in table.css("tr"):
                cells = [_text(cell) for cell in tr.css("td, th")]
                if cells:
                    rows.append(cells)
            if headers or rows:
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="table",
                    data={"headers": headers, "rows": rows},
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)

        # Lists (ul > li patterns) — scoped per P7.3: skip nav/footer/header/
        # aside ancestors, and skip lists whose items are all bare links
        # (the shape of a nav menu even without a semantic <nav> wrapper).
        for lst in soup.css("ul, ol"):
            if lst.find_ancestor(lambda n: n.tag in _LIST_NOISE_ANCESTOR_TAGS):
                continue
            li_tags = lst.css("li")
            if len(li_tags) < 3:
                continue
            if all(_is_bare_link_item(li) for li in li_tags):
                continue
            items = [_text(li) for li in li_tags]
            record = ExtractedRecord(
                record_id=f"rec_{uuid.uuid4().hex[:12]}",
                record_type="list",
                data={"items": items},
                source_url=current_url,
            )
            record.compute_identity_hash()
            records.append(record)

        return records

    # ------------------------------------------------------------------
    # Structured markup: Microdata / RDFa / OpenGraph (P7.2)
    # ------------------------------------------------------------------

    def _extract_structured_markup(self, soup: Selector, current_url: str) -> list[ExtractedRecord]:
        """Deterministic structured-data stages that don't need JSON-LD.
        Tries OpenGraph meta tags, then Microdata, then RDFa; returns
        records from the first one that finds anything."""
        og_record = self._extract_opengraph(soup, current_url)
        if og_record:
            return [og_record]
        microdata = self._extract_scoped_props(soup, "itemscope", "itemprop", "itemtype", current_url)
        if microdata:
            return microdata
        return self._extract_scoped_props(soup, "typeof", "property", "typeof", current_url)

    def _extract_opengraph(self, soup: Selector, current_url: str) -> ExtractedRecord | None:
        """og:* meta tags -> a single record, if any are present."""
        props: dict[str, str] = {}
        for meta in soup.css('meta[property^="og:"]'):
            key = meta.attrib.get("property", "")[len("og:"):]
            value = meta.attrib.get("content")
            if key and value:
                props[key] = value
        if not props:
            return None
        record = ExtractedRecord(
            record_id=f"rec_{uuid.uuid4().hex[:12]}",
            record_type="opengraph",
            data=props,
            source_url=current_url,
        )
        record.compute_identity_hash()
        return record

    def _extract_scoped_props(
        self, soup: Selector, scope_attr: str, name_attr: str, type_attr: str, current_url: str
    ) -> list[ExtractedRecord]:
        """Shared walk for Microdata ([itemscope]/[itemprop]/[itemtype]) and
        RDFa ([typeof]/[property]/[typeof]). Only top-level scopes are
        recorded — nested scopes (e.g. a Product's nested Offer) fold their
        props into the parent record rather than emitting a second one."""
        records: list[ExtractedRecord] = []
        for scope in soup.css(f"[{scope_attr}]"):
            if scope.find_ancestor(lambda n: scope_attr in n.attrib):
                continue
            props: dict[str, str] = {}
            for prop in scope.css(f"[{name_attr}]"):
                name = prop.attrib.get(name_attr)
                if not name:
                    continue
                value = prop.attrib.get("content") or _text(prop)
                if value:
                    props[name] = value
            if not props:
                continue
            type_val = scope.attrib.get(type_attr, "")
            record_type = type_val.rstrip("/").rsplit("/", 1)[-1].lower() or "structured"
            record = ExtractedRecord(
                record_id=f"rec_{uuid.uuid4().hex[:12]}",
                record_type=record_type,
                data=props,
                source_url=current_url,
            )
            record.compute_identity_hash()
            records.append(record)
        return records

    # ------------------------------------------------------------------
    # Content-addressed extraction (P7.2)
    # ------------------------------------------------------------------

    async def _extract_content_addressed(self, soup: Selector, current_url: str) -> list[ExtractedRecord]:
        """Last resort before an LLM call: find an element by what its text
        looks like (price, date, ...), walk to its container to recombine
        split-span values, then use find_similar to locate its peers. Three
        or more similar containers is treated as a real repeating list; a
        successful hit is also synthesized into a CANDIDATE overlay so the
        next visit to this domain doesn't need this stage at all."""
        for field_name, pattern in _CONTENT_PATTERNS:
            match = soup.find_by_regex(pattern, first_match=True)
            if not match:
                continue
            container = match.parent
            if container is None:
                continue
            peers = container.find_similar()
            items = [container, *peers]
            if len(items) < 3:
                continue  # a lone match isn't a list worth an overlay
            records = []
            for item in items:
                text = _text(item)
                if not text:
                    continue
                record = ExtractedRecord(
                    record_id=f"rec_{uuid.uuid4().hex[:12]}",
                    record_type="content_addressed",
                    data={field_name: text},
                    source_url=current_url,
                )
                record.compute_identity_hash()
                records.append(record)
            if records:
                await self._synthesize_overlay(field_name, match, container, peers, current_url)
                return records
        return []

    async def _synthesize_overlay(
        self, field_name: str, match: Selector, container: Selector, peers, current_url: str
    ) -> None:
        """Turn a content-addressed hit into a CANDIDATE overlay. Never
        touches ACTIVE state directly — ShadowOverlayEvaluator gates
        promotion on real evidence, same as an LLM-authored overlay (R13)."""
        if not self.overlay_repo:
            return
        from urllib.parse import urlparse
        domain = urlparse(current_url).netloc
        if not domain:
            return
        common_classes = set(container.attrib.get("class", "").split())
        for peer in peers:
            common_classes &= set(peer.attrib.get("class", "").split())
        container_selector = (
            f"{container.tag}.{'.'.join(sorted(common_classes))}"
            if common_classes
            else container.generate_css_selector
        )
        # ponytail: the field selector is just tag(+first class) of the
        # matched element — a crude but cheap seed. Shadow evaluation scores
        # it against real samples before it can ever reach ACTIVE, so a bad
        # guess here self-corrects instead of shipping silently.
        match_classes = match.attrib.get("class", "").split()
        field_selector = f"{match.tag}.{match_classes[0]}" if match_classes else match.tag
        overlay = ExtractionOverlay(
            overlay_id=f"ovl_{uuid.uuid4().hex[:12]}",
            domain=domain,
            schema_id=f"content_addressed_{field_name}",
            container_selector=container_selector,
            field_mappings={field_name: field_selector},
            field_signatures={field_name: _build_element_signature(match)},
            source_evidence=current_url,
        )
        try:
            await self.overlay_repo.create_overlay(overlay)
            logger.info(
                "Pipeline(P7.2): synthesized CANDIDATE overlay %s for %s (%s)",
                overlay.overlay_id, domain, field_name,
            )
        except Exception:
            logger.warning("Pipeline(P7.2): failed to persist synthesized overlay for %s", domain, exc_info=True)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_records(
        self, records: list[ExtractedRecord], schema: ExtractionSchema | None
    ) -> list[ExtractedRecord]:
        """Filter records through schema validation if a schema is provided."""
        if not schema:
            return records
        valid: list[ExtractedRecord] = []
        for r in records:
            errors = schema.validate_record(r.data)
            if errors:
                logger.debug(
                    "Pipeline: record failed schema validation: %s", errors
                )
            else:
                valid.append(r)
        return valid


class ExtractionPipeline:
    """
    Spacescraper Intelligence Orchestrator (W2.2).

    Replaces pipeline.py::DataPipeline. Same `.process()` contract (RawScrapePayload,
    strategy) -> ProcessingResult, so callers need no interface changes — only the
    strategy and orchestrator implementations swap. Delegates field extraction to a
    BaseExtractionStrategy (DeterministicExtractionPipeline on the live path) and
    near-duplicate removal to Deduplicator, keeping this class itself thin.

    AI enrichment (C7) is not reintroduced: DataPipeline's ai_enrichment_enabled
    flag gated a stub that returned immediately on every call, so removing it
    changes no live behavior. Reintroducing enrichment for real is unrelated
    feature work, tracked separately if it's ever wanted.
    """

    def __init__(self, deduplicator: Deduplicator | None = None):
        self.deduplicator = deduplicator or Deduplicator()

    async def process(self, payload: RawScrapePayload, strategy: BaseExtractionStrategy) -> ProcessingResult:
        result = ProcessingResult(job_id=payload.job_id, success=False)

        if payload.status_code >= 400 or payload.error_message:
            result.error = payload.error_message
            return result

        try:
            logger.info("Spacescraper: Dispatching to %s", strategy.__class__.__name__)
            records = await strategy.extract(
                payload.html_content,
                payload.json_payloads,
                current_url=payload.url,
                overlay=payload.overlay,
            )

            for record in records:
                self._ensure_hashes(record)

            unique_records = self.deduplicator.dedupe(records)

            result.success = True
            result.entities = unique_records
            result.follow_urls = self._discover_follow_urls(payload)

        except Exception as e:
            logger.exception("Spacescraper Pipeline Critical Error: %s", e)
            result.error = f"Pipeline Internal Error: {e}"

        return result

    def _discover_follow_urls(self, payload: RawScrapePayload) -> list[dict]:
        """P2: FollowLink pointers for the processor's existing fan-out
        (worker_processor.py:117+) — opt-in via payload.follow_links, and
        gated on depth budget so recursion actually terminates."""
        if not payload.html_content:
            return []

        if not payload.follow_links:
            return []

        follow: list[dict] = []
        next_page = find_next_page_url(payload.html_content, payload.url)
        if next_page:
            # Same depth: pagination through a result set isn't discovery.
            follow.append({
                "url": next_page, "target_site": "universal",
                "depth": payload.depth, "max_depth": payload.max_depth,
            })

        if payload.depth < payload.max_depth:
            for url in extract_links(
                payload.html_content, payload.url,
                include_globs=payload.link_include_globs or None,
                exclude_globs=payload.link_exclude_globs or None,
            ):
                follow.append({
                    "url": url, "target_site": "universal",
                    "depth": payload.depth + 1, "max_depth": payload.max_depth,
                })

        return follow

    @staticmethod
    def _ensure_hashes(record: ExtractedRecord) -> None:
        """Guarantee both hashes are set regardless of which strategy branch built the record."""
        if not record.identity_hash:
            record.compute_identity_hash()
        if not record.content_hash:
            record.content_hash = hashlib.sha256(
                json.dumps(record.data, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
