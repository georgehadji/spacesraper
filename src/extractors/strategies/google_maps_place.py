# GoogleMapsPlaceStrategy — extract rich details from individual Google Maps
# place pages. Triggered when URL matches google.com/maps/place/.
# Higher priority than GoogleMapsStrategy because it accesses richer data.

import json
import logging
import uuid
from typing import Any

from src.domain.models import ExtractedRecord, ExtractionSchema

logger = logging.getLogger("Spacescraper.Strategies.GoogleMapsPlace")


class GoogleMapsPlaceStrategy:
    """
    Extract data from a single Google Maps place detail page.

    Place pages contain richer data than search results:
      - Detailed description (editorial summary)
      - Popular times (live busyness)
      - All reviews (pagination supported)
      - Full photo gallery
      - "People also search for" carousel
      - Accessibility features
      - Service options (dine-in, takeaway, delivery)
    """

    name: str = "google_maps_place"

    async def extract(
        self,
        html: str,
        json_payloads: list[dict],
        current_url: str = "",
        overlay: dict | None = None,
        schema: ExtractionSchema | None = None,
    ) -> list[ExtractedRecord]:
        """Parse a single Google Maps place page."""
        data = self._parse_place_payload(json_payloads, html)

        if not data or not data.get("name"):
            logger.debug("GoogleMapsPlace: no place data in %s", current_url)
            return []

        record = ExtractedRecord(
            record_id=f"rec_{uuid.uuid4().hex[:12]}",
            record_type="business_listing",
            data=data,
            source_url=current_url,
        )
        record.compute_identity_hash()

        logger.info("GoogleMapsPlace: extracted place %s", data.get("name"))
        return [record]

    # ------------------------------------------------------------------
    # Place page parsing
    # ------------------------------------------------------------------

    def _parse_place_payload(
        self, json_payloads: list[dict], html: str
    ) -> dict[str, Any] | None:
        """Extract place data from intercepted JSON payloads."""
        data: dict[str, Any] = {}

        # 1. Find the place object in JSON payloads
        place_obj = self._find_place_object(json_payloads)
        if not place_obj:
            # Fallback: parse from HTML metadata
            data.update(self._parse_html_meta(html))
            return data if data.get("name") else None

        # 2. Map known fields
        data["place_id"] = place_obj.get("place_id", "")
        data["name"] = place_obj.get("name", "")
        data["address"] = place_obj.get("formatted_address", "")
        data["website"] = place_obj.get("website", "")

        if "geometry" in place_obj:
            geo = place_obj["geometry"]
            if "location" in geo:
                data["latitude"] = geo["location"].get("lat")
                data["longitude"] = geo["location"].get("lng")

        data["rating"] = place_obj.get("rating")
        data["reviews_count"] = place_obj.get("user_ratings_total")

        # Phone
        if "formatted_phone_number" in place_obj:
            data["phone"] = place_obj["formatted_phone_number"]
        elif "international_phone_number" in place_obj:
            data["phone"] = place_obj["international_phone_number"]

        # Categories
        if "types" in place_obj:
            types = place_obj["types"]
            if isinstance(types, list):
                data["category"] = types[0] if types else ""

        # Opening hours
        if "opening_hours" in place_obj:
            hours = place_obj["opening_hours"]
            if isinstance(hours, dict):
                data["opening_hours"] = ", ".join(hours.get("weekday_text", []))

        # Price level
        if "price_level" in place_obj:
            price_levels = {0: "Free", 1: "Inexpensive", 2: "Moderate", 3: "Expensive", 4: "Very Expensive"}
            level = place_obj["price_level"]
            data["price_level"] = price_levels.get(int(level), str(level))

        # Description
        if "editorial_summary" in place_obj:
            summ = place_obj["editorial_summary"]
            if isinstance(summ, dict):
                data["description"] = summ.get("overview", "")
            elif isinstance(summ, str):
                data["description"] = summ
        elif "description" in place_obj:
            data["description"] = place_obj["description"]

        # Photos
        if "photos" in place_obj:
            photos = place_obj["photos"]
            if isinstance(photos, list):
                data["photos_count"] = len(photos)

        # Reviews (deep extraction — expandable)
        if "reviews" in place_obj:
            reviews = place_obj["reviews"]
            if isinstance(reviews, list):
                data["reviews"] = [
                    {
                        "author": r.get("author_name", ""),
                        "rating": r.get("rating"),
                        "text": r.get("text", "")[:2000],
                        "time": r.get("time"),
                    }
                    for r in reviews[:10]
                ]

        # Popular times
        if "popular_times" in place_obj:
            data["popular_times"] = place_obj["popular_times"]

        return data

    def _find_place_object(self, json_payloads: list[dict]) -> dict | None:
        """Heuristic: find the payload containing a Google Place result."""
        for payload in json_payloads:
            found = self._search_dict(payload, max_depth=5)
            if found:
                return found
        return None

    def _search_dict(self, obj: Any, max_depth: int) -> dict | None:
        """Recursively search for an object with place_id and name."""
        if max_depth <= 0:
            return None
        if isinstance(obj, dict):
            if "place_id" in obj and "name" in obj:
                return obj
            for v in obj.values():
                result = self._search_dict(v, max_depth - 1)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._search_dict(item, max_depth - 1)
                if result:
                    return result
        return None

    def _parse_html_meta(self, html: str) -> dict[str, Any]:
        """Fallback: extract place data from HTML meta tags."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        data: dict[str, Any] = {}

        # Title may contain business name
        if soup.title:
            title = soup.title.get_text(strip=True)
            if "·" in title:
                data["name"] = title.split("·")[0].strip()[:200]
            elif "-" in title:
                data["name"] = title.rsplit("-", 1)[0].strip()[:200]

        # JSON-LD may contain place schema
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string)
                if isinstance(ld, dict):
                    if ld.get("@type") in ("Place", "LocalBusiness", "Restaurant"):
                        data.update(self._parse_jsonld_place(ld))
                        break
            except (json.JSONDecodeError, TypeError):
                continue

        return data

    def _parse_jsonld_place(self, ld: dict) -> dict[str, Any]:
        """Extract fields from schema.org Place/LocalBusiness JSON-LD."""
        result: dict[str, Any] = {}
        result["name"] = ld.get("name", "")
        result["website"] = ld.get("url", ld.get("sameAs", ""))

        if "address" in ld:
            addr = ld["address"]
            if isinstance(addr, dict):
                parts = [
                    addr.get("streetAddress", ""),
                    addr.get("addressLocality", ""),
                    addr.get("addressRegion", ""),
                    addr.get("postalCode", ""),
                    addr.get("addressCountry", ""),
                ]
                result["address"] = ", ".join(p for p in parts if p)

        if "telephone" in ld:
            result["phone"] = ld["telephone"]

        if "aggregateRating" in ld:
            ar = ld["aggregateRating"]
            result["rating"] = ar.get("ratingValue")
            result["reviews_count"] = ar.get("reviewCount")

        if "geo" in ld:
            geo = ld["geo"]
            result["latitude"] = geo.get("latitude")
            result["longitude"] = geo.get("longitude")

        if "openingHoursSpecification" in ld:
            result["opening_hours"] = str(ld["openingHoursSpecification"])[:200]

        if "priceRange" in ld:
            result["price_level"] = str(ld["priceRange"])[:10]

        return result

    @staticmethod
    def matches_domain(url: str) -> bool:
        """Check if URL is a Google Maps place page."""
        return "google.com/maps/place" in url
