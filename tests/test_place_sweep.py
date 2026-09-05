"""
Sweep behaviour, offline against a stub Places client.

The cases below are regressions for defects the live Thermaikos run exposed:
Text Search leaking results from 13 km away, the locality centroid mislabelling
practices, and a full Nearby page being reported as a complete answer.
"""

import pytest

from src.application.place_sweep import (
    AreaSpec,
    SweepConfig,
    _subdivide,
    run_places_sweep,
)
from src.infrastructure.places.google_places import PlaceResult

PERAIA = (40.5009985, 22.9257853)


def place(
    pid, name, *, lat, lng, website=None, types=("doctor",), address="Περαία 570 19"
):
    return PlaceResult(
        place_id=pid,
        name=name,
        address=address,
        website=website,
        types=list(types),
        latitude=lat,
        longitude=lng,
    )


class StubClient:
    """Duck-types GooglePlacesClient; records what the sweep asked for."""

    def __init__(self, nearby=None, text=None, centre=PERAIA, nearby_full=False):
        self._nearby = nearby or []
        self._text = text or []
        self._centre = centre
        self._nearby_full = nearby_full
        self.request_count = 0
        self.nearby_calls = []

    async def resolve_area_center(self, query):
        self.request_count += 1
        return self._centre

    async def search_nearby(self, lat, lng, radius_m, included_types, **kw):
        self.request_count += 1
        self.nearby_calls.append((lat, lng, radius_m, tuple(included_types)))
        # Only the top-level call is saturated, so subdivision terminates.
        full = self._nearby_full and len(self.nearby_calls) == 1
        return list(self._nearby), full

    async def search_text(self, text_query, **kw):
        self.request_count += 1
        return list(self._text)


def one_area(**kw):
    return SweepConfig(
        areas=[AreaSpec(name="Peraia", query="Περαία", radius_m=2000.0, **kw)],
        included_types=["doctor"],
        text_queries=["ιατρός"],
    )


async def test_buckets_split_by_web_presence():
    client = StubClient(
        nearby=[
            place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258),
            place("b", "Ιατρείο Β", lat=40.5011, lng=22.9259,
                  website="https://www.xo.gr/profile/1"),
            place("c", "Ιατρείο Γ", lat=40.5012, lng=22.9260,
                  website="https://iatreio-c.gr"),
            place("d", "Ιατρείο Δ", lat=40.5013, lng=22.9261,
                  website="https://facebook.com/d"),
        ]
    )
    report = await run_places_sweep(client, one_area())

    assert {x.place.place_id for x in report.no_website} == {"a", "b"}
    assert {x.place.place_id for x in report.has_website} == {"c"}
    assert {x.place.place_id for x in report.borderline} == {"d"}


async def test_a_directory_url_is_reported_as_missing_not_present():
    """The whole point: an xo.gr profile must not read as a website."""
    client = StubClient(
        nearby=[place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258,
                      website="https://www.xo.gr/profile/911524")]
    )
    report = await run_places_sweep(client, one_area())
    assert [x.place.place_id for x in report.no_website] == ["a"]
    assert report.no_website[0].website_kind.value == "directory"


async def test_results_outside_the_radius_are_dropped():
    """
    Regression: Text Search biases, it does not bound. A live run for
    "Agia Triada" returned a practice in the same-named Thessaloniki district
    13 km away.
    """
    client = StubClient(
        text=[
            place("near", "Ιατρείο Κοντά", lat=40.5010, lng=22.9258),
            place("far", "ΙΑΤΡΕΙΑ ΑΓ.ΤΡΙΑΔΟΣ", lat=40.6200, lng=22.9500),
        ]
    )
    report = await run_places_sweep(client, one_area())
    ids = {x.place.place_id for x in report.no_website}
    assert ids == {"near"}
    assert any("Geographic filter removed 1" in w for w in report.warnings)


async def test_area_label_comes_from_the_address_not_the_nearest_centroid():
    """
    Regression: Google's "Peraia" centroid sits nearer the Neoi Epivates
    centroid than Peraia's own medical district, so proximity mislabelled
    Peraia practices as Neoi Epivates.
    """
    config = SweepConfig(
        areas=[
            AreaSpec(name="Peraia", query="Περαία", radius_m=3000.0,
                     latitude=40.5010, longitude=22.9258,
                     address_tokens=["περαια"]),
            AreaSpec(name="Neoi Epivates", query="Νέοι Επιβάτες", radius_m=3000.0,
                     latitude=40.49877, longitude=22.9122,
                     address_tokens=["νεοι επιβατες"]),
        ],
        included_types=["doctor"],
        text_queries=[],
    )
    # Physically nearer the Neoi Epivates centre, but addressed in Peraia.
    client = StubClient(
        nearby=[place("x", "Ιατρείο Χ", lat=40.4990, lng=22.9130,
                      address="Κύπρου 2, Περαία 570 19")]
    )
    report = await run_places_sweep(client, config)
    listing = report.no_website[0]
    assert listing.areas[0] == "Peraia"
    assert listing.area_source == "address"


async def test_non_medical_results_are_set_aside_not_counted():
    client = StubClient(
        text=[
            place("church", "Ιερός Ναός Αγίας Τριάδος", lat=40.5010, lng=22.9258,
                  types=("church", "place_of_worship")),
            place("doc", "Ιατρείο Α", lat=40.5011, lng=22.9259, types=()),
        ]
    )
    report = await run_places_sweep(client, one_area())
    assert [x.place.place_id for x in report.no_website] == ["doc"]
    assert [x["place_id"] for x in report.excluded_non_medical] == ["church"]


async def test_ambiguous_listings_are_kept_but_flagged_for_review():
    client = StubClient(
        text=[place("amb", "Υγεία Περαίας", lat=40.5010, lng=22.9258,
                    types=("medical_clinic", "health"))]
    )
    report = await run_places_sweep(client, one_area())
    assert report.no_website[0].relevance == "review"
    summary = report.to_dict()["summary"]
    assert summary["no_website_confirmed"] == 0
    assert summary["no_website_needs_review"] == 1


async def test_a_full_nearby_page_triggers_subdivision():
    """A full page means "capped", so the area must be re-searched smaller."""
    client = StubClient(
        nearby=[place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258)],
        nearby_full=True,
    )
    config = one_area()
    config.subdivision_factor = 3
    report = await run_places_sweep(client, config)
    # 1 saturated top-level call + 9 sub-cells.
    assert len(client.nearby_calls) == 10
    assert all(c[2] < 2000.0 for c in client.nearby_calls[1:])
    assert not any("still returned a full page" in w for w in report.warnings)


async def test_subdivision_is_reported_when_it_cannot_resolve_the_cap():
    class AlwaysFull(StubClient):
        async def search_nearby(self, lat, lng, radius_m, included_types, **kw):
            self.request_count += 1
            self.nearby_calls.append((lat, lng, radius_m, tuple(included_types)))
            return list(self._nearby), True

    client = AlwaysFull(nearby=[place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258)])
    report = await run_places_sweep(client, one_area())
    assert any("still returned a full page" in w for w in report.warnings)


async def test_closed_businesses_are_excluded_by_default():
    closed = place("z", "Ιατρείο Ζ", lat=40.5010, lng=22.9258)
    closed.business_status = "CLOSED_PERMANENTLY"
    client = StubClient(nearby=[closed])
    assert (await run_places_sweep(client, one_area())).total == 0

    config = one_area()
    config.include_closed = True
    assert (await run_places_sweep(StubClient(nearby=[closed]), config)).total == 1


async def test_the_same_practice_found_twice_is_one_row():
    dup = place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258)
    client = StubClient(nearby=[dup], text=[dup])
    report = await run_places_sweep(client, one_area())
    assert len(report.no_website) == 1
    assert sorted(report.no_website[0].sources) == ["nearby:doctor", "text:ιατρός"]


async def test_a_website_found_in_a_later_pass_is_not_lost():
    """Passes return uneven detail; the richer value must win."""
    bare = place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258)
    rich = place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258,
                 website="https://iatreio-a.gr")
    report = await run_places_sweep(
        StubClient(nearby=[bare], text=[rich]), one_area()
    )
    assert [x.place.place_id for x in report.has_website] == ["a"]


async def test_an_unresolvable_area_is_reported_not_silently_skipped():
    class NoCentre(StubClient):
        async def resolve_area_center(self, query):
            self.request_count += 1
            return None

    report = await run_places_sweep(NoCentre(), one_area())
    assert report.total == 0
    assert any("could not resolve a centre" in w for w in report.warnings)


async def test_a_failing_query_does_not_abort_the_sweep():
    class FlakyText(StubClient):
        async def search_text(self, text_query, **kw):
            raise RuntimeError("429 quota")

    client = FlakyText(nearby=[place("a", "Ιατρείο Α", lat=40.5010, lng=22.9258)])
    report = await run_places_sweep(client, one_area())
    assert [x.place.place_id for x in report.no_website] == ["a"]
    assert any("429 quota" in w for w in report.warnings)


@pytest.mark.parametrize("factor,expected", [(2, 4), (3, 9), (4, 16)])
def test_subdivide_cell_count(factor, expected):
    assert len(_subdivide(40.5, 22.9, 2000.0, factor)) == expected


def test_subdivided_cells_overlap_enough_to_cover_the_corners():
    """Sub-circles must cover their square corner-to-corner, or gaps appear."""
    cells = _subdivide(40.5, 22.9, 3000.0, 3)
    half_width = 3000.0 / 3
    for _, _, radius in cells:
        assert radius >= half_width * 1.414
