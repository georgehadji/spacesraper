# GoogleMapsStrategy — extract business listings from Google Maps search results.
# Depends on Google Maps internal JSON payloads intercepted by ScraperEngine.
# Strategy hierarchy: override > google_maps_place > google_maps > generic.

import hashlib
import json
import logging
import uuid
from typing import List, Optional, Dict, Any
from src.domain.models import ExtractedRecord, ExtractionSchema

logger = logging.getLogger("Spacescraper.Strategies.GoogleMaps")


class GoogleMapsStrategy:
    """
    Extracts business listings from Google Maps search results.

    Google Maps renders results dynamically (JavaScript). This strategy
    relies on the internal JSON payloads intercepted by ScraperEngine's
    network interception layer (json_payloads parameter).

    The JSON structure is fragile — Google frequently changes array
    positions. The parser returns partial results gracefully: missing
    fields are omitted rather than causing the entire extraction to fail.

    This is a domain-level strategy, active when:
      - The URL matches google.com/maps/search
      - No page_fields override is present
    """

    name: str = "google_maps"

    async def extract(
        self,
        html: str,
        json_payloads: List[dict],
        current_url: str = "",
        overlay: Optional[dict] = None,
        schema: Optional[ExtractionSchema] = None,
    ) -> List[ExtractedRecord]:
        """Parse Google Maps internal JSON into ExtractedRecord list."""
        records: List[ExtractedRecord] = []

        # Try intercepted JSON payloads first, then embedded HTML
        business_arrays = self._find_business_arrays(json_payloads)
        if not business_arrays:
            raw = self._extract_embedded_json(html)
            if raw:
                business_arrays = self._parse_search_results(raw)

        if not business_arrays:
            logger.debug("GoogleMaps: no business data found in %s", current_url)
            return []

        for arr in business_arrays:
            data = self._entry_to_data(arr)
            if not data.get("name"):
                continue

            record = ExtractedRecord(
                record_id=f"rec_{uuid.uuid4().hex[:12]}",
                record_type="business_listing",
                data=data,
                source_url=current_url,
            )
            record.compute_identity_hash()
            records.append(record)

        logger.info(
            "GoogleMapsStrategy: extracted %d business listings from %s",
            len(records), current_url,
        )
        return records

    # ------------------------------------------------------------------
    # Internal JSON parsing — port of google-maps-scraper gmaps/multiple.go
    # ------------------------------------------------------------------

    def _find_business_arrays(self, json_payloads: List[dict]) -> Optional[List[list]]:
        """
        Find Google Maps business arrays in intercepted XHR payloads.

        Google Maps search results embed business data as a deeply nested
        array-of-arrays. The known structure (from multiple.go):
          raw[0] = outer container
          container[1] = items (list of business entries)
          items[i][14] = business array

        Returns a list of business arrays (each an inner list at [14]),
        or None if not found.
        """
        best: Optional[List[list]] = None
        best_len = 0

        for payload in json_payloads:
            if not isinstance(payload, list):
                continue
            if len(payload) < 1:
                continue
            # Try to find the container at payload[0]
            container = payload[0]
            if not isinstance(container, list) or len(container) < 2:
                # If payload wraps the data, payload[0][0] might be the
                # container (if len(payload) == 1)
                if isinstance(container, list) and len(container) == 1:
                    container = container[0]
                else:
                    continue
            items = container[1] if len(container) > 1 else None
            if not isinstance(items, list) or len(items) < 2:
                continue
            # items[0] is header; items[1..n] are businesses
            business_arrays: List[list] = []
            for i in range(1, len(items)):
                arr = items[i]
                if not isinstance(arr, list) or len(arr) < 15:
                    continue
                business = arr[14]
                if isinstance(business, list) and len(business) > 11:
                    business_arrays.append(business)
            if len(business_arrays) > best_len:
                best = business_arrays
                best_len = len(business_arrays)

        return best

    def _extract_embedded_json(self, html: str) -> Optional[bytes]:
        """
        Extract raw )]}' -prefixed JSON from window.APP_INITIALIZATION_STATE.

        Google Maps embeds search result data as a JavaScript variable with
        a )]}' prefix (to prevent JSON injection). This extracts and returns
        the raw bytes after stripping the prefix.
        """
        import re
        # Look for the APP_INITIALIZATION_STATE[3] pattern
        for pattern in [
            r'window\.APP_INITIALIZATION_STATE\[?\d*\]?\s*=\s*(\[.*?\])\s*;',
            r'\(\)\]\}"(\[.*?\])\s*;',
        ]:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    raw = match.group(1).encode("utf-8")
                    return raw
                except Exception:
                    continue
        return None

    def _parse_search_results(self, raw: bytes) -> Optional[List[list]]:
        """
        Parse Google Maps search result JSON — port of ParseSearchResults.

        Structure (from multiple.go:
          raw JSON is a [)]}' blocks (strip first line if needed)
          data[0] = outer container
          container[1] = items (list of business array data)
          items[0] = header/metadata (skipped)
          items[1..n] = individual business outer arrays
          each items[i][14] = business array with fields
        """
        import re
        try:
            text = raw.decode("utf-8")
            # Strip the )]}' prefix if present (Google Maps anti-scraping)
            text = re.sub(r"^\)\]\}'\s*", "", text).strip()
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        if not isinstance(data, list) or len(data) == 0:
            return None

        container = data[0]
        if not isinstance(container, list) or len(container) < 2:
            return None

        items = container[1]
        if not isinstance(items, list) or len(items) < 2:
            return None

        business_arrays: List[list] = []
        for i in range(1, len(items)):
            arr = items[i]
            if not isinstance(arr, list) or len(arr) < 15:
                continue
            business = arr[14]
            if isinstance(business, list) and len(business) > 11:
                business_arrays.append(business)

        return business_arrays if business_arrays else None

    # ------------------------------------------------------------------
    # Field mapping — port of gmaps/entry.go array indices
    # ------------------------------------------------------------------

    def _entry_to_data(self, entry: list) -> Dict[str, Any]:
        """
        Map a Google Maps business entry array to a flat data dict.

        Index positions from gmaps/multiple.go ParseSearchResults:
          [0]   ID (place input ID)
          [2]   Address parts (list, joined with ", ")
          [4][7] Review rating (float64)
          [4][8] Review count (int)
          [7][0] Website URL
          [9][2] Latitude (float64)
          [9][3] Longitude (float64)
          [10]  DataID
          [11]  Title (business name)
          [13]  Categories (list of strings)
          [30]  Timezone string
          [34][4][4] Status ("Permanently closed", etc.)
          [178][0][0] Phone (string, spaces stripped)
        """
        data: Dict[str, Any] = {}

        try:
            # --- Title (business name) — entry[11] ---
            val = self._nth(entry, 11)
            if isinstance(val, str) and val.strip():
                data["name"] = val.strip()[:200]

            # --- Place input ID — entry[0] ---
            val = self._nth(entry, 0)
            if isinstance(val, str) and val.strip():
                data["place_id"] = val.strip()

            # --- Address — entry[2] (list of parts) ---
            parts = self._nth(entry, 2)
            if isinstance(parts, list):
                addr_parts = [str(p) for p in parts if isinstance(p, str) and p.strip()]
                if addr_parts:
                    data["address"] = ", ".join(addr_parts)[:500]

            # --- Rating — entry[4][7] ---
            sub4 = self._nth(entry, 4)
            if isinstance(sub4, list) and len(sub4) > 8:
                rating = sub4[7]
                if isinstance(rating, (int, float)):
                    data["rating"] = float(max(1.0, min(5.0, rating)))

                # --- Review count — entry[4][8] ---
                cnt = sub4[8]
                if isinstance(cnt, (int, float)):
                    data["reviews_count"] = int(cnt)

            # --- Website — entry[7][0] ---
            sub7 = self._nth(entry, 7)
            if isinstance(sub7, list) and len(sub7) > 0:
                url = sub7[0]
                if isinstance(url, str) and url.startswith("http"):
                    data["website"] = url[:500]

            # --- Coordinates — entry[9][2] (lat), [9][3] (lng) ---
            sub9 = self._nth(entry, 9)
            if isinstance(sub9, list) and len(sub9) >= 3:
                if isinstance(sub9[2], (int, float)):
                    data["latitude"] = float(sub9[2])
                if len(sub9) >= 4 and isinstance(sub9[3], (int, float)):
                    data["longitude"] = float(sub9[3])

            # --- DataID — entry[10] ---
            val = self._nth(entry, 10)
            if isinstance(val, str) and val.strip():
                data["data_id"] = val.strip()

            # --- Categories — entry[13] (list) ---
            val = self._nth(entry, 13)
            if isinstance(val, list) and val:
                cats = [str(c) for c in val if isinstance(c, str) and c.strip()]
                if cats:
                    data["category"] = cats[0][:100]

            # --- Timezone — entry[30] ---
            val = self._nth(entry, 30)
            if isinstance(val, str) and val.strip():
                data["timezone"] = val.strip()[:50]

            # --- Status — entry[34][4][4] ---
            sub34 = self._nth(entry, 34)
            if isinstance(sub34, list) and len(sub34) >= 5:
                sub_sub = sub34[4]
                if isinstance(sub_sub, list) and len(sub_sub) >= 5:
                    status = sub_sub[4]
                    if isinstance(status, str) and status.strip():
                        data["status"] = status.strip()[:50]

            # --- Phone — entry[178][0][0] ---
            sub178 = self._nth(entry, 178)
            if isinstance(sub178, list) and len(sub178) > 0:
                sub_z = sub178[0]
                if isinstance(sub_z, list) and len(sub_z) > 0:
                    phone = sub_z[0]
                    if isinstance(phone, str):
                        cleaned = phone.replace(" ", "").replace("-", "").strip()
                        if cleaned:
                            data["phone"] = cleaned[:20]

        except Exception:
            logger.debug("GoogleMaps: partial entry parse failure", exc_info=True)

        return data

    @staticmethod
    def _nth(arr: list, idx: int) -> Any:
        """Safe index accessor — returns None if out of bounds or not a list."""
        if not isinstance(arr, list) or idx >= len(arr):
            return None
        return arr[idx]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def matches_domain(url: str) -> bool:
        """Check if URL is a Google Maps search."""
        return "google.com/maps" in url and "/search" in url

    @staticmethod
    def url_to_grid_cells(
        lat1: float, lng1: float,
        lat2: float, lng2: float,
        cell_size_km: float = 1.0,
    ) -> List[Dict[str, float]]:
        """
        Subdivide a bounding box into grid cells.
        Each cell is approximately cell_size_km x cell_size_km.
        Returns center point of each cell.

        Ported from google-maps-scraper grid/grid.go GenerateCells().

        Google Maps returns ~120 results max per search. Grid mode
        subdivides large geographic areas into smaller cells to
        surface all businesses in a region.
        """
        import math as m

        km_per_deg_lat = 111.32
        min_cos_lat = 1e-6

        if cell_size_km <= 0:
            cell_size_km = 1.0

        lat_step = cell_size_km / km_per_deg_lat

        mid_lat = (lat1 + lat2) / 2
        cos_mid = m.cos(mid_lat * m.pi / 180)
        if abs(cos_mid) < min_cos_lat:
            cos_mid = min_cos_lat if cos_mid >= 0 else -min_cos_lat
        lon_step = cell_size_km / (km_per_deg_lat * cos_mid)

        cells: List[Dict[str, float]] = []
        lat = lat1 + lat_step / 2
        while lat < lat2:
            lon = lng1 + lon_step / 2
            while lon < lng2:
                cells.append({"lat": lat, "lon": lon})
                lon += lon_step
            lat += lat_step

        return cells

    @staticmethod
    def estimate_cell_count(
        lat1: float, lng1: float,
        lat2: float, lng2: float,
        cell_size_km: float = 1.0,
    ) -> int:
        """Estimate number of grid cells without generating them."""
        import math as m

        km_per_deg_lat = 111.32
        min_cos_lat = 1e-6

        if cell_size_km <= 0:
            cell_size_km = 1.0

        lat_step = cell_size_km / km_per_deg_lat

        mid_lat = (lat1 + lat2) / 2
        cos_mid = m.cos(mid_lat * m.pi / 180)
        if abs(cos_mid) < min_cos_lat:
            cos_mid = min_cos_lat if cos_mid >= 0 else -min_cos_lat
        lon_step = cell_size_km / (km_per_deg_lat * cos_mid)

        lat_cells = max(0, int(m.ceil((lat2 - lat1) / lat_step)))
        lon_cells = max(0, int(m.ceil((lng2 - lng1) / lon_step)))
        return lat_cells * lon_cells
