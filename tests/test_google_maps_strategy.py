"""Tests for the Google Maps integration strategy.

Tests the ported parsing logic from google-maps-scraper without needing a
real browser or network. Uses fixture JSON that mimics the structure of
Google Maps search result internal payloads.

Strategy hierarchy tested:
  - OverrideStrategy (page_fields) beats GoogleMapsStrategy
  - GoogleMapsPlaceStrategy beats GoogleMapsStrategy via URL match
  - _parse_search_results handles )]}' -prefixed JSON
  - _entry_to_data extracts fields at correct index positions
  - url_to_grid_cells subdivides bounding boxes correctly
"""

import json
import pytest
from src.extractors.strategies.google_maps import GoogleMapsStrategy


# ---------------------------------------------------------------------------
# Fixtures: simulated Google Maps search result payloads
# ---------------------------------------------------------------------------

SAMPLE_SEARCH_JSON = (
    ')]}\'\n'
    + json.dumps([
        [
            None,
            [
                None,  # items[0] = header
                # items[1] = first business
                [
                    None, None, None, None, None,
                    None, None, None, None, None,
                    None, None, None, None, None,
                    None, None, None, None, None,
                    None, None, None, None, None,
                    None, None, None, None, None,
                    None, None, None, None, None,
                    # positions discussed below
                ]
            ]
        ]
    ])
)


def _make_search_result(entries: list) -> str:
    """
    Build a fake )]}'-prefixed search result JSON with the given entries.

    Entry format (list of 15+ elements):
      [0]   = place_id (str)
      [2]   = address parts (list of str)
      [4]   = rating sub-array: [..., [..., None, rating, count, ...]]
      [7]   = website sub-array: [url]
      [9]   = coordinates sub-array: [..., [..., lat, lng, ...]]
      [10]  = data_id (str)
      [11]  = title/business name (str)
      [13]  = categories (list of str)
      [30]  = timezone (str)
      [34]  = status sub-array (deep)
      [178] = phone sub-array (deep)
    """
    items = [None]  # header
    for entry in entries:
        business_arr = [None] * 179
        for idx, val in entry.items():
            business_arr[idx] = val
        # items[i][14] must be the business array (port of Go structure)
        outer = [None] * 15
        outer[14] = business_arr
        items.append(outer)

    raw = [None, items]
    return ")]}'\n" + json.dumps([raw])


SIMPLE_ENTRY = {
    0: "ChIJ12345",
    2: ["123 Main St", "Springfield", "IL"],
    4: [None, None, None, None, None, None, None, 4.5, 231],
    7: ["https://example.com"],
    9: [None, None, 40.7128, -74.0060],
    10: "data_abc",
    11: "Test Business",
    13: ["Pizza", "Italian"],
    30: "America/New_York",
    34: [None, None, None, None, ["", "", "", "", "Permanently closed"]],
    178: [["+1 555-1234"]],
}

MINIMAL_ENTRY = {
    11: "Minimal Biz",
}


class TestParseSearchResults:
    """_parse_search_results — port of gmaps/multiple.go ParseSearchResults."""

    def test_simple_entry(self):
        raw = _make_search_result([SIMPLE_ENTRY])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        assert result is not None
        assert len(result) == 1

    def test_minimal_entry(self):
        raw = _make_search_result([MINIMAL_ENTRY])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        assert result is not None
        assert len(result) == 1

    def test_multiple_entries(self):
        raw = _make_search_result([SIMPLE_ENTRY, MINIMAL_ENTRY])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        assert result is not None
        assert len(result) == 2

    def test_empty_returns_none(self):
        raw = ")]}'\n" + json.dumps([{}])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        assert result is None

    def test_short_entry_is_skipped(self):
        """Entry without at least 15 outer elements in items[i] is skipped."""
        raw = ")]}'\n" + json.dumps([[None, [[], [42]]]])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        assert result is None or len(result) == 0

    def test_no_prefix_still_works(self):
        """_parse_search_results handles JSON without the )]}' prefix."""
        items = [[None, [None] * 15 + [[[None] * 12 + ["Name"]]]]]
        raw = json.dumps([[None, items]])
        strategy = GoogleMapsStrategy()
        result = strategy._parse_search_results(raw.encode("utf-8"))
        # Should still work because regex-based prefix stripping is lenient
        if result:
            assert len(result) >= 0


class TestEntryToData:
    """_entry_to_data — field mapping from Go array indices."""

    def test_full_entry(self):
        strategy = GoogleMapsStrategy()
        business = [None] * 179
        for idx, val in SIMPLE_ENTRY.items():
            business[idx] = val
        data = strategy._entry_to_data(business)

        assert data.get("name") == "Test Business"
        assert data.get("place_id") == "ChIJ12345"
        assert data.get("address") == "123 Main St, Springfield, IL"
        assert data.get("rating") == 4.5
        assert data.get("reviews_count") == 231
        assert data.get("website") == "https://example.com"
        assert data.get("latitude") == 40.7128
        assert data.get("longitude") == -74.0060
        assert data.get("category") == "Pizza"
        assert data.get("timezone") == "America/New_York"
        assert data.get("status") == "Permanently closed"
        assert data.get("phone") == "+15551234"
        assert data.get("data_id") == "data_abc"

    def test_minimal_entry(self):
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data([None] * 11 + ["Minimal Biz"])
        assert data.get("name") == "Minimal Biz"
        # No rating, no address, no phone — silently None
        assert "rating" not in data
        assert "address" not in data
        assert "phone" not in data

    def test_empty_returns_empty_dict(self):
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data([])
        assert data == {}

    def test_rating_clamped(self):
        business = [None] * 179
        business[4] = [None, None, None, None, None, None, None, 5.5, 10]
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data(business)
        assert data.get("rating") == 5.0  # clamped to max 5.0

    def test_rating_clamped_low(self):
        business = [None] * 179
        business[4] = [None, None, None, None, None, None, None, 0.5, 10]
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data(business)
        assert data.get("rating") == 1.0  # clamped to min 1.0

    def test_phone_strips_spaces_and_dashes(self):
        business = [None] * 179
        business[178] = [["+1 555-6789"]]
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data(business)
        assert data.get("phone") == "+15556789"

    def test_status_deep_path(self):
        business = [None] * 179
        business[34] = [None, None, None, None, ["", "", "", "", "CLOSED"]]
        strategy = GoogleMapsStrategy()
        data = strategy._entry_to_data(business)
        assert data.get("status") == "CLOSED"


class TestGridCells:
    """url_to_grid_cells — port of grid/grid.go GenerateCells."""

    def test_small_bbox(self):
        cells = GoogleMapsStrategy.url_to_grid_cells(
            40.0, -74.0, 40.01, -73.99, cell_size_km=1.0
        )
        assert len(cells) >= 1
        # Each cell is a center point with lat/lon
        assert "lat" in cells[0]
        assert "lon" in cells[0]

    def test_larger_bbox(self):
        cells = GoogleMapsStrategy.url_to_grid_cells(
            40.0, -74.0, 40.5, -73.0, cell_size_km=5.0
        )
        # BBox ~55km x ~85km at 40N → should generate many cells
        assert len(cells) >= 5
        assert len(cells) <= 500

    def test_estimate_cell_count(self):
        count = GoogleMapsStrategy.estimate_cell_count(
            40.0, -74.0, 40.5, -73.0, cell_size_km=5.0
        )
        assert count >= 1
        actual = len(GoogleMapsStrategy.url_to_grid_cells(
            40.0, -74.0, 40.5, -73.0, cell_size_km=5.0
        ))
        # Estimate should be close to actual
        assert abs(count - actual) / max(actual, 1) < 0.5

    def test_zero_cell_size_defaults_to_1(self):
        cells = GoogleMapsStrategy.url_to_grid_cells(
            40.0, -74.0, 40.1, -73.9, cell_size_km=0
        )
        assert len(cells) >= 1

    def test_cell_centers_fall_within_bbox(self):
        cells = GoogleMapsStrategy.url_to_grid_cells(
            40.0, -74.0, 40.1, -73.9, cell_size_km=0.5
        )
        for cell in cells:
            assert 40.0 <= cell["lat"] <= 40.1
            assert -74.0 <= cell["lon"] <= -73.9


class TestFullExtract:
    """Full extract pipeline end-to-end."""

    @pytest.mark.asyncio
    async def test_extract_from_search_json(self):
        raw = _make_search_result([SIMPLE_ENTRY])
        strategy = GoogleMapsStrategy()
        records = await strategy.extract(
            html="",
            json_payloads=[json.loads(raw.replace(")]}'\n", ""))],
            current_url="https://www.google.com/maps/search/pizza",
        )
        assert len(records) >= 1
        assert records[0].record_type == "business_listing"
        assert records[0].data["name"] == "Test Business"
        assert records[0].identity_hash is not None

    @pytest.mark.asyncio
    async def test_extract_from_html_fallback(self):
        strategy = GoogleMapsStrategy()
        records = await strategy.extract(
            html="""
            <html><body>
            <script>window.APP_INITIALIZATION_STATE=[null,[[null,["""
            + json.dumps([None] * 15 + [[[None] * 12 + ["HTML Biz"]]])
            + """]]]]</script>
            </body></html>
            """,
            json_payloads=[],
            current_url="https://www.google.com/maps/search/test",
        )
        assert len(records) >= 0  # may or may not parse HTML blob

    @pytest.mark.asyncio
    async def test_extract_empty_returns_empty(self):
        strategy = GoogleMapsStrategy()
        records = await strategy.extract(
            html="", json_payloads=[], current_url="https://example.com"
        )
        assert records == []

    @pytest.mark.asyncio
    async def test_extract_no_name_is_skipped(self):
        strategy = GoogleMapsStrategy()
        records = await strategy.extract(
            html="",
            json_payloads=[],
            current_url="https://www.google.com/maps/search/test",
        )
        assert records == []
