"""P6 — the Places sweep's failure paths (D33, D34, D37, D38).

Every call in this sweep is billed. These four findings share a consequence:
money is spent and the result is either thrown away or misreported.

  D33  resolve_area_center was the one unguarded call in the area loop, so a
       raise in area 2 discarded area 1's already-billed results entirely.
  D34  PlacesQuotaError had no consumer outside tests. Both passes catch
       Exception broadly, so quota exhaustion became a warning, the sweep
       fired every remaining request against an exhausted quota, and the run
       ended at EXIT_OK reading like a sparse area.
  D37  "page came back full → subdivide" counted *parsed* results. One entry
       dropped by PlaceResult.from_api turned 20 into 19, so the area was
       never subdivided and the businesses behind it were never searched for.
  D38  the cross-pass merge backfilled website but not phone. A practice
       whose number only appears in the second pass was exported with no
       phone — and a phone number is the product of this sweep.

Scope: offline against stub clients. No live Places call, no API key. That
is sufficient for all four — each is a decision this code makes about a
response it has already received, not a question about what Google returns.
"""

import json
import types

import pytest

import cli
from src.application.place_sweep import (
    AreaSpec,
    Listing,
    SweepConfig,
    SweepReport,
    WebsiteKind,
    _Accumulator,
    run_places_sweep,
)
from src.infrastructure.places.google_places import (
    GooglePlacesClient,
    PlaceResult,
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
    def __init__(self, *responses):
        self._responses = list(responses)

    async def post(self, url, json=None, headers=None, timeout=None):
        return self._responses.pop(0) if self._responses else FakeResponse(200, {})


def places_client(*responses):
    return GooglePlacesClient("test-key", http_client=FakeHttp(*responses))

PERAIA = (40.5009985, 22.9257853)
EPANOMI = (40.4300000, 22.9300000)


def place(pid, name, *, lat, lng, website=None, phone=None, types=("doctor",)):
    return PlaceResult(
        place_id=pid,
        name=name,
        address="Περαία 570 19",
        website=website,
        phone=phone,
        types=list(types),
        latitude=lat,
        longitude=lng,
    )


class ScriptedClient:
    """Duck-types GooglePlacesClient with per-area scripted behaviour."""

    def __init__(self, *, centres=None, centre_errors=None, nearby=None, nearby_error_after=None):
        self._centres = centres or {}
        self._centre_errors = centre_errors or {}
        self._nearby = nearby or {}
        self._nearby_error_after = nearby_error_after
        self.request_count = 0
        self.nearby_calls: list[tuple[float, float]] = []
        self.centre_queries: list[str] = []

    async def resolve_area_center(self, query):
        self.centre_queries.append(query)
        self.request_count += 1
        if query in self._centre_errors:
            raise self._centre_errors[query]
        return self._centres.get(query)

    async def search_nearby(self, lat, lng, radius_m, included_types, **kw):
        self.request_count += 1
        self.nearby_calls.append((lat, lng))
        if (
            self._nearby_error_after is not None
            and len(self.nearby_calls) > self._nearby_error_after
        ):
            raise PlacesQuotaError("quota exceeded", code="PLACES_QUOTA")
        return list(self._nearby.get((round(lat, 4), round(lng, 4)), [])), False

    async def search_text(self, text_query, **kw):
        self.request_count += 1
        return []


def two_areas():
    return SweepConfig(
        areas=[
            AreaSpec(name="Peraia", query="Περαία", radius_m=2000.0),
            AreaSpec(name="Epanomi", query="Επανομή", radius_m=2000.0),
        ],
        included_types=["doctor"],
        text_queries=["ιατρός"],
    )


# --- D33 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_a_failing_centre_lookup_does_not_discard_earlier_areas():
    """The guard for D33.

    The area loop already handled resolve_area_center *returning* None. It
    did not handle it *raising* — and a raise there aborted the whole sweep,
    taking every area already paid for with it.
    """
    client = ScriptedClient(
        centres={"Περαία": PERAIA},
        centre_errors={"Επανομή": RuntimeError("upstream geocode 500")},
        nearby={
            (round(PERAIA[0], 4), round(PERAIA[1], 4)): [
                place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1])
            ]
        },
    )

    report = await run_places_sweep(client, two_areas())

    assert report.total == 1, "area 1's billed results were discarded by area 2's failure"
    assert {x.place.place_id for x in report.no_website} == {"a"}
    assert any("Επανομή" in w or "Epanomi" in w for w in report.warnings), (
        "the failed area must be named in the report, not silently absent"
    )


@pytest.mark.asyncio
async def test_b_a_failing_centre_lookup_still_sweeps_the_areas_after_it():
    """Order must not matter: area 1 failing cannot cost area 2."""
    client = ScriptedClient(
        centres={"Επανομή": EPANOMI},
        centre_errors={"Περαία": RuntimeError("upstream geocode 500")},
        nearby={
            (round(EPANOMI[0], 4), round(EPANOMI[1], 4)): [
                place("b", "Ιατρείο Β", lat=EPANOMI[0], lng=EPANOMI[1])
            ]
        },
    )

    report = await run_places_sweep(client, two_areas())

    assert {x.place.place_id for x in report.no_website} == {"b"}


# --- D34 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_quota_exhaustion_stops_the_sweep_instead_of_becoming_a_warning():
    """The guard for D34.

    Once the quota is gone every remaining call is guaranteed to fail, so
    continuing spends the rest of the run on rejections. The broad
    `except Exception` in both passes demoted the quota error to a warning
    and did exactly that.
    """
    client = ScriptedClient(
        centres={"Περαία": PERAIA, "Επανομή": EPANOMI},
        nearby={
            (round(PERAIA[0], 4), round(PERAIA[1], 4)): [
                place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1])
            ]
        },
        nearby_error_after=1,
    )

    report = await run_places_sweep(client, two_areas())

    assert report.quota_exhausted is True, (
        "quota exhaustion is indistinguishable from a sparse area"
    )
    assert len(client.nearby_calls) == 2, (
        "the sweep kept calling after the quota was exhausted"
    )
    assert report.total == 1, "results billed before the quota ran out were discarded"
    assert any("quota" in w.lower() for w in report.warnings)


@pytest.mark.asyncio
async def test_d_a_quota_exhausted_run_exits_non_zero(monkeypatch, capsys):
    """The consequence D34 actually names: the operator must be able to tell.

    A sweep that found two practices because the quota died after one area
    exits the same way as one that found two practices because there are two.
    """
    captured = {}

    async def fake_sweep(client, config):
        report = SweepReport()
        report.quota_exhausted = True
        report.no_website.append(
            Listing(
                place=place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1]),
                website_kind=WebsiteKind.NONE,
            )
        )
        captured["called"] = True
        return report

    monkeypatch.setattr("src.application.place_sweep.run_places_sweep", fake_sweep)

    args = types.SimpleNamespace(
        api_key="test-key", area=None, radius=None, preset="doctors", query=None,
        social_counts_as_none=False, booking_counts_as_none=False, max_pages=1,
        include_closed=False, include_veterinary=False, max_depth=1,
        no_relevance_filter=True, no_area_filter=True, timeout=10.0,
        csv=None, leads_csv=None, exclude_file=None,
        leads_include_borderline=False, pretty=False,
    )

    code = await cli.cmd_places(args)

    assert captured.get("called")
    assert code == cli.EXIT_FAILURE, "an exhausted quota exited as a successful run"
    assert "quota" in capsys.readouterr().err.lower()


# --- D37 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_a_full_page_is_detected_from_what_the_api_returned():
    """The guard for D37.

    from_api returns None for an entry with no id or no display name. The
    saturation flag counted survivors, so a single such entry in a full page
    read as "not full" — and the area was never subdivided, which is the only
    mechanism that reaches the businesses beyond the page limit.
    """
    entries = [{"id": str(i), "displayName": {"text": f"P{i}"}} for i in range(19)]
    entries.append({"id": "20", "displayName": {"text": "   "}})  # dropped by from_api

    results, truncated = await places_client(
        FakeResponse(200, {"places": entries})
    ).search_nearby(40.5, 22.9, 2000, ["doctor"])

    assert len(results) == 19, "precondition: one entry must be unparseable"
    assert truncated is True, "a full page read as partial, so the area was never subdivided"


@pytest.mark.asyncio
async def test_f_a_genuinely_partial_page_is_still_partial():
    """Control for the D37 guard: it must not flag every page as saturated."""
    entries = [{"id": str(i), "displayName": {"text": f"P{i}"}} for i in range(5)]
    results, truncated = await places_client(
        FakeResponse(200, {"places": entries})
    ).search_nearby(40.5, 22.9, 2000, ["doctor"])

    assert len(results) == 5 and truncated is False


# --- D38 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g_a_phone_from_a_later_pass_is_kept():
    """The guard for D38.

    The merge already backfilled a website a sparser earlier pass lacked.
    Phone was left out of that — and a phone number is what this sweep is
    for, so dropping it discards the deliverable while keeping the row.
    """
    acc = _Accumulator()
    acc.add(place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1]), "Peraia", "nearby:doctor")
    acc.add(
        place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1], phone="2392 022222"),
        "Peraia",
        "text:ιατρός",
    )

    listings = acc.listings()
    assert len(listings) == 1
    assert listings[0].place.phone == "2392 022222", "the later pass's phone was discarded"


@pytest.mark.asyncio
async def test_h_an_existing_phone_is_not_overwritten_by_a_later_blank():
    """Control: backfill fills gaps, it does not let a sparser pass win."""
    acc = _Accumulator()
    acc.add(
        place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1], phone="2392 011111"),
        "Peraia",
        "nearby:doctor",
    )
    acc.add(place("a", "Ιατρείο Α", lat=PERAIA[0], lng=PERAIA[1]), "Peraia", "text:ιατρός")

    assert acc.listings()[0].place.phone == "2392 011111"
