"""Places API adapter: request shape, error mapping, and paging."""

import json

import pytest

from src.infrastructure.places.google_places import (
    DEFAULT_FIELD_MASK,
    GooglePlacesClient,
    PlaceResult,
    PlacesApiError,
    PlacesQuotaError,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeHttp:
    """Captures posts and replays queued responses."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json, "headers": headers})
        return self._responses.pop(0) if self._responses else FakeResponse(200, {})


def client(*responses, **kw):
    return GooglePlacesClient("test-key", http_client=FakeHttp(*responses), **kw)


def test_an_empty_api_key_is_rejected_up_front():
    with pytest.raises(PlacesApiError) as exc:
        GooglePlacesClient("")
    assert exc.value.code == "PLACES_NO_API_KEY"


async def test_request_carries_key_and_field_mask():
    c = client(FakeResponse(200, {"places": []}))
    await c.search_nearby(40.5, 22.9, 2000, ["doctor"])
    headers = c._http.calls[0]["headers"]
    assert headers["X-Goog-Api-Key"] == "test-key"
    assert headers["X-Goog-FieldMask"] == DEFAULT_FIELD_MASK
    # websiteUri is the field the whole question depends on.
    assert "places.websiteUri" in headers["X-Goog-FieldMask"]


async def test_nearby_body_restricts_to_the_requested_circle():
    c = client(FakeResponse(200, {"places": []}))
    await c.search_nearby(40.5, 22.9, 1500, ["doctor", "dentist"])
    body = c._http.calls[0]["body"]
    circle = body["locationRestriction"]["circle"]
    assert circle["center"] == {"latitude": 40.5, "longitude": 22.9}
    assert circle["radius"] == 1500.0
    assert body["includedTypes"] == ["doctor", "dentist"]


async def test_nearby_reports_saturation_when_the_page_comes_back_full():
    full = {"places": [{"id": str(i), "displayName": {"text": f"P{i}"}} for i in range(20)]}
    results, truncated = await client(FakeResponse(200, full)).search_nearby(
        40.5, 22.9, 2000, ["doctor"]
    )
    assert len(results) == 20 and truncated is True

    partial = {"places": [{"id": "1", "displayName": {"text": "P"}}]}
    results, truncated = await client(FakeResponse(200, partial)).search_nearby(
        40.5, 22.9, 2000, ["doctor"]
    )
    assert len(results) == 1 and truncated is False


async def test_text_search_follows_the_page_token():
    page1 = {
        "places": [{"id": "a", "displayName": {"text": "A"}}],
        "nextPageToken": "tok",
    }
    page2 = {"places": [{"id": "b", "displayName": {"text": "B"}}]}
    c = client(FakeResponse(200, page1), FakeResponse(200, page2))
    results = await c.search_text("ιατρός Περαία")
    assert [r.place_id for r in results] == ["a", "b"]
    assert c._http.calls[1]["body"]["pageToken"] == "tok"


async def test_text_search_stops_at_the_page_cap():
    pages = [
        FakeResponse(200, {"places": [{"id": str(i), "displayName": {"text": "X"}}],
                           "nextPageToken": "t"})
        for i in range(5)
    ]
    c = client(*pages)
    await c.search_text("q", max_pages=2)
    assert len(c._http.calls) == 2


async def test_text_search_biases_rather_than_restricts():
    """Bias keeps a well-named practice just outside the radius reachable."""
    c = client(FakeResponse(200, {"places": []}))
    await c.search_text("q", latitude=40.5, longitude=22.9, radius_m=2000)
    body = c._http.calls[0]["body"]
    assert "locationBias" in body
    assert "locationRestriction" not in body


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (403, "Method doesn't allow unregistered callers", PlacesApiError),
        (400, "Invalid field mask", PlacesApiError),
        (429, "Rate limit exceeded", PlacesQuotaError),
        (403, "You must enable billing", PlacesQuotaError),
    ],
)
async def test_error_mapping(status, message, expected):
    c = client(FakeResponse(status, {"error": {"message": message}}))
    with pytest.raises(expected) as exc:
        await c.search_nearby(40.5, 22.9, 2000, ["doctor"])
    assert message in str(exc.value)


async def test_the_api_key_is_never_echoed_into_an_error():
    c = client(FakeResponse(403, {"error": {"message": "denied"}}))
    with pytest.raises(PlacesApiError) as exc:
        await c.search_nearby(40.5, 22.9, 2000, ["doctor"])
    assert "test-key" not in str(exc.value)


async def test_resolve_area_center_reads_coordinates_back_from_the_api():
    payload = {"places": [{"id": "x", "location": {"latitude": 40.5, "longitude": 22.92}}]}
    centre = await client(FakeResponse(200, payload)).resolve_area_center("Περαία")
    assert centre == (40.5, 22.92)


async def test_resolve_area_center_returns_none_when_nothing_matches():
    assert await client(FakeResponse(200, {"places": []})).resolve_area_center("x") is None


def test_place_mapping():
    raw = {
        "id": "abc",
        "displayName": {"text": "Ιατρείο Α"},
        "formattedAddress": "Κύπρου 2, Περαία 570 19",
        "websiteUri": "https://www.xo.gr/profile/1",
        "nationalPhoneNumber": "2392 025164",
        "primaryTypeDisplayName": {"text": "Ιατρός"},
        "types": ["doctor", "health"],
        "location": {"latitude": 40.5, "longitude": 22.92},
        "rating": 4.5,
        "userRatingCount": 12,
        "businessStatus": "OPERATIONAL",
    }
    p = PlaceResult.from_api(raw)
    assert (p.place_id, p.name, p.phone) == ("abc", "Ιατρείο Α", "2392 025164")
    assert p.website == "https://www.xo.gr/profile/1"
    assert p.primary_type == "Ιατρός"
    assert (p.latitude, p.longitude) == (40.5, 22.92)


def test_a_missing_website_stays_none_rather_than_empty_string():
    """None and "" must not be conflated: absence is the signal being measured."""
    p = PlaceResult.from_api({"id": "a", "displayName": {"text": "A"}})
    assert p.website is None


@pytest.mark.parametrize(
    "raw",
    [
        {"displayName": {"text": "No id"}},
        {"id": "x"},
        {"id": "x", "displayName": {"text": "  "}},
    ],
)
def test_entries_without_identity_are_dropped(raw):
    assert PlaceResult.from_api(raw) is None


async def test_request_count_tracks_billable_calls():
    c = client(FakeResponse(200, {"places": []}), FakeResponse(200, {"places": []}))
    await c.search_nearby(40.5, 22.9, 2000, ["doctor"])
    await c.search_text("q")
    assert c.request_count == 2
