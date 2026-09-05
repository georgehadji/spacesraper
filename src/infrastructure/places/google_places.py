# Project: Spacescraper (Infrastructure / Places)
# Role: Adapter for the Google Places API (New) on places.googleapis.com.
#
# This is the supported, billed route to Maps business data. It exists
# alongside the google_maps extraction strategies rather than replacing them:
# the strategies parse whatever a browser managed to load, this asks Google
# directly and gets a documented schema back, including the websiteUri field
# the "no website" question turns on.
#
# Docs: https://developers.google.com/maps/documentation/places/web-service/search-nearby
#       https://developers.google.com/maps/documentation/places/web-service/text-search

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.domain.exceptions import SpacescraperError

logger = logging.getLogger("Spacescraper.Places")

PLACES_BASE_URL = "https://places.googleapis.com/v1"

# Places API (New) caps a single response at 20 results. Text Search will
# page beyond that with a token; Nearby Search will not page at all, so a
# nearby call returning exactly this many is a truncation signal, never a
# complete answer.
MAX_PAGE_SIZE = 20
MAX_TEXT_SEARCH_PAGES = 3  # 60 results, the documented Text Search ceiling

# Only the fields the sweep actually reads. The field mask is not cosmetic:
# it selects the SKU Google bills, so widening it costs money per call.
DEFAULT_FIELD_MASK = ",".join(
    f"places.{f}"
    for f in (
        "id",
        "displayName",
        "formattedAddress",
        "websiteUri",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "primaryType",
        "primaryTypeDisplayName",
        "types",
        "location",
        "rating",
        "userRatingCount",
        "googleMapsUri",
        "businessStatus",
    )
)


class PlacesApiError(SpacescraperError):
    """The Places API rejected a request or returned an unusable response."""


class PlacesQuotaError(PlacesApiError):
    """Quota exhausted or billing not enabled -- retrying will not help."""


@dataclass(slots=True)
class PlaceResult:
    """One business as the Places API reports it."""

    place_id: str
    name: str
    address: str = ""
    website: str | None = None
    phone: str | None = None
    primary_type: str = ""
    types: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None
    reviews_count: int | None = None
    maps_uri: str = ""
    business_status: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> PlaceResult | None:
        """Map one API `places[]` entry. Returns None when it has no identity."""
        place_id = (raw.get("id") or "").strip()
        display = raw.get("displayName") or {}
        name = (display.get("text") if isinstance(display, dict) else "") or ""
        name = name.strip()
        if not place_id or not name:
            return None

        loc = raw.get("location") or {}
        primary_display = raw.get("primaryTypeDisplayName") or {}

        return cls(
            place_id=place_id,
            name=name,
            address=(raw.get("formattedAddress") or "").strip(),
            # An absent websiteUri is the signal we are looking for, so keep
            # None distinct from the empty string a blank field would give.
            website=(raw.get("websiteUri") or None),
            phone=(
                raw.get("nationalPhoneNumber")
                or raw.get("internationalPhoneNumber")
                or None
            ),
            primary_type=(
                primary_display.get("text")
                if isinstance(primary_display, dict) and primary_display.get("text")
                else (raw.get("primaryType") or "")
            ),
            types=[t for t in (raw.get("types") or []) if isinstance(t, str)],
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            rating=raw.get("rating"),
            reviews_count=raw.get("userRatingCount"),
            maps_uri=(raw.get("googleMapsUri") or ""),
            business_status=(raw.get("businessStatus") or ""),
        )


class GooglePlacesClient:
    """
    Thin async client over Places API (New).

    Requests go through the shared SSRF-guarded HTTP client, so the same
    egress rules that apply to scrape targets apply here.
    """

    def __init__(
        self,
        api_key: str,
        *,
        field_mask: str = DEFAULT_FIELD_MASK,
        language_code: str = "el",
        region_code: str = "GR",
        timeout: float = 30.0,
        http_client: Any | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise PlacesApiError(
                "A Google Maps Platform API key is required "
                "(set GOOGLE_MAPS_API_KEY or pass --api-key).",
                code="PLACES_NO_API_KEY",
            )
        self._api_key = api_key.strip()
        self._field_mask = field_mask
        self._language_code = language_code
        self._region_code = region_code
        self._timeout = timeout
        self._http = http_client
        self.request_count = 0

    async def _client(self) -> Any:
        if self._http is not None:
            return self._http
        from src.infrastructure.http_client import internal_http

        return internal_http

    def _headers(self, field_mask: str | None = None) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask or self._field_mask,
        }

    async def _post(
        self, endpoint: str, body: dict[str, Any], field_mask: str | None = None
    ) -> dict[str, Any]:
        client = await self._client()
        url = f"{PLACES_BASE_URL}/{endpoint}"
        self.request_count += 1

        response = await client.post(
            url,
            json=body,
            headers=self._headers(field_mask),
            timeout=self._timeout,
        )

        if response.status_code == 200:
            return response.json()

        # Surface Google's own message; it distinguishes "key not authorised
        # for this API" from "billing disabled", which the caller must fix
        # differently. The key itself is never echoed back.
        detail = ""
        try:
            payload = response.json()
            detail = (payload.get("error") or {}).get("message", "")
        except Exception:
            detail = (response.text or "")[:300]

        message = f"Places API {endpoint} failed ({response.status_code}): {detail}"
        if response.status_code == 429 or "quota" in detail.lower() or "billing" in detail.lower():
            raise PlacesQuotaError(message, code="PLACES_QUOTA")
        raise PlacesApiError(message, code="PLACES_HTTP_ERROR")

    @staticmethod
    def _parse_places(payload: dict[str, Any]) -> list[PlaceResult]:
        results: list[PlaceResult] = []
        for raw in payload.get("places") or []:
            if not isinstance(raw, dict):
                continue
            parsed = PlaceResult.from_api(raw)
            if parsed is not None:
                results.append(parsed)
        return results

    async def search_nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: float,
        included_types: list[str],
        *,
        max_result_count: int = MAX_PAGE_SIZE,
    ) -> tuple[list[PlaceResult], bool]:
        """
        Nearby Search within a circle.

        Returns (results, truncated). `truncated` is True when the response
        came back full: Nearby Search cannot page, so a full page means there
        are probably more businesses than the caller was shown and the area
        needs subdividing.
        """
        capped = max(1, min(int(max_result_count), MAX_PAGE_SIZE))
        body: dict[str, Any] = {
            "maxResultCount": capped,
            "languageCode": self._language_code,
            "regionCode": self._region_code,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": float(radius_m),
                }
            },
        }
        if included_types:
            body["includedTypes"] = list(included_types)

        payload = await self._post("places:searchNearby", body)
        results = self._parse_places(payload)
        return results, len(results) >= capped

    async def search_text(
        self,
        text_query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: float | None = None,
        max_pages: int = MAX_TEXT_SEARCH_PAGES,
    ) -> list[PlaceResult]:
        """
        Text Search, following nextPageToken up to `max_pages`.

        A circle is applied as locationBias rather than locationRestriction:
        bias keeps a well-named practice just outside the radius reachable,
        which matters because these localities have no crisp boundary.
        """
        body: dict[str, Any] = {
            "textQuery": text_query,
            "pageSize": MAX_PAGE_SIZE,
            "languageCode": self._language_code,
            "regionCode": self._region_code,
        }
        if latitude is not None and longitude is not None and radius_m:
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": float(radius_m),
                }
            }

        collected: list[PlaceResult] = []
        seen: set[str] = set()
        token: str | None = None
        field_mask = f"{self._field_mask},nextPageToken"

        for _ in range(max(1, max_pages)):
            if token:
                body["pageToken"] = token
            payload = await self._post("places:searchText", body, field_mask)
            for place in self._parse_places(payload):
                if place.place_id not in seen:
                    seen.add(place.place_id)
                    collected.append(place)
            token = payload.get("nextPageToken")
            if not token:
                break

        return collected

    async def resolve_area_center(
        self, area_query: str
    ) -> tuple[float, float] | None:
        """
        Look up a locality's coordinates through the API itself.

        The sweep needs a centre for each named area. Reading it back from
        Google keeps hardcoded coordinates -- which drift and cannot be
        verified offline -- off the correctness path.
        """
        payload = await self._post(
            "places:searchText",
            {
                "textQuery": area_query,
                "pageSize": 1,
                "languageCode": self._language_code,
                "regionCode": self._region_code,
            },
            field_mask="places.id,places.location,places.formattedAddress",
        )
        for raw in payload.get("places") or []:
            loc = (raw or {}).get("location") or {}
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                return float(lat), float(lng)
        return None
