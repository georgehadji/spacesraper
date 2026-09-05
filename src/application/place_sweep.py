# Project: Spacescraper (Application)
# Role: Sweep named areas for businesses and split them by web presence.
#
# The question this answers is "which practices here have no site of their
# own", so the sweep is built around two failure modes that would each give a
# wrong answer quietly:
#
#   1. Missing businesses. Places API returns at most 20 per Nearby call and
#      60 per Text Search, and type labels on Greek listings are uneven. The
#      sweep therefore runs both a typed nearby pass and Greek text queries,
#      unions the results, and reports truncation instead of hiding it.
#   2. Miscounting a directory profile as a website. Delegated wholesale to
#      domain.website_classifier so the rule lives in one place.

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from src.domain.medical_relevance import fold_greek, medical_signal
from src.domain.website_classifier import WebsiteKind, classify_website
from src.infrastructure.places.google_places import (
    MAX_TEXT_SEARCH_PAGES,
    GooglePlacesClient,
    PlaceResult,
)

logger = logging.getLogger("Spacescraper.PlaceSweep")

# Places API (New) type identifiers. `doctor` is the physician type; the
# wider preset exists because Greek listings are labelled inconsistently and
# a practice is often filed under a neighbouring health type.
TYPE_PRESETS: dict[str, list[str]] = {
    "doctors": ["doctor"],
    "medical": [
        "doctor",
        "dentist",
        "dental_clinic",
        "physiotherapist",
        "chiropractor",
        "medical_lab",
        "hospital",
        "skin_care_clinic",
    ],
}

# Greek search terms. Type filters alone miss practices Google never typed as
# `doctor`, so these run as text queries and the results are unioned in.
DEFAULT_DOCTOR_QUERIES: list[str] = [
    # General
    "ιατρός", "ιατρείο", "πολυϊατρείο", "ιατρικό κέντρο", "διαγνωστικό κέντρο",
    # Specialties, in rough order of how many distinct practices each surfaced
    "παθολόγος", "γυναικολόγος", "ορθοπεδικός", "μικροβιολόγος", "οφθαλμίατρος",
    "δερματολόγος", "καρδιολόγος", "παιδίατρος", "νευρολόγος", "ουρολόγος",
    "πνευμονολόγος", "ενδοκρινολόγος", "γαστρεντερολόγος", "ρευματολόγος",
    "ωτορινολαρυγγολόγος", "ψυχίατρος", "χειρουργός", "αλλεργιολόγος",
    "νεφρολόγος", "ακτινολόγος", "ογκολόγος", "αιματολόγος", "οδοντίατρος",
    "φυσικοθεραπευτής", "εργαστήριο",
]


# Region qualifiers that disambiguate a search query ("Peraia Thessalonikis")
# but never appear in the locality field of a formatted address ("Peraia 570 19").
_QUERY_QUALIFIERS: tuple[str, ...] = (
    "θεσσαλονικης",
    "θεσσαλονικη",
    "θερμαικου",
    "θερμαικος",
    "χαλκιδικης",
    "ελλαδα",
    "greece",
    "thessaloniki",
)


def derive_address_tokens(query: str) -> list[str]:
    """
    Guess the locality substring an address would use, from a search query.

    Without this an `--area` passed on the command line falls back to
    nearest-centroid labelling, which is exactly the mislabelling the address
    lookup exists to avoid.
    """
    folded = fold_greek(query).strip()
    if not folded:
        return []
    words = [w for w in folded.split() if w not in _QUERY_QUALIFIERS]
    if not words:
        # The query is nothing but a qualifier, which means the qualifier is
        # itself the locality being asked for -- "Thermaikos" is both the
        # municipality that qualifies "Agia Triada" and an address locality
        # in its own right ("Thermaikos 570 19").
        words = folded.split()
    if not words:
        return []
    token = " ".join(words)
    tokens = [token]
    # "Nea Michaniona" is also written "N. Michaniona" in addresses.
    if len(words) > 1 and len(words[0]) > 1:
        tokens.append(f"{words[0][0]}. {' '.join(words[1:])}")
    return tokens


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


@dataclass(slots=True)
class AreaSpec:
    """One named area to sweep."""

    name: str
    query: str
    radius_m: float = 2000.0
    latitude: float | None = None
    longitude: float | None = None
    # Locality names as they appear in a formatted address. Google's centroid
    # for a locality is not its town core -- the "Peraia" centroid sits east
    # of Peraia's medical district, closer to the "Neoi Epivates" centroid --
    # so proximity mislabels. The address Google returns says the locality
    # outright, and is used first.
    address_tokens: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.address_tokens:
            self.address_tokens = derive_address_tokens(self.query)

    @property
    def has_center(self) -> bool:
        return self.latitude is not None and self.longitude is not None


# The three localities in Thermaikos, Thessaloniki. Centres are deliberately
# left unset: the sweep resolves each one through the API so no unverifiable
# coordinate sits on the correctness path.
THERMAIKOS_AREAS: list[AreaSpec] = [
    AreaSpec(
        name="Peraia",
        query="Περαία Θεσσαλονίκης",
        radius_m=2500.0,
        address_tokens=["περαια", "peraia", "perea"],
    ),
    AreaSpec(
        name="Neoi Epivates",
        query="Νέοι Επιβάτες Θεσσαλονίκης",
        radius_m=2000.0,
        address_tokens=["νεοι επιβατες", "ν. επιβατες", "neoi epivates", "nei epivates"],
    ),
    AreaSpec(
        name="Agia Triada",
        query="Αγία Τριάδα Θερμαϊκού",
        radius_m=2000.0,
        address_tokens=["αγια τριαδα", "αγ. τριαδα", "agia triada"],
    ),
]


@dataclass(slots=True)
class SweepConfig:
    """Everything that decides what the sweep looks for and how it judges it."""

    areas: list[AreaSpec] = field(default_factory=lambda: list(THERMAIKOS_AREAS))
    included_types: list[str] = field(default_factory=lambda: list(TYPE_PRESETS["doctors"]))
    text_queries: list[str] = field(default_factory=lambda: list(DEFAULT_DOCTOR_QUERIES))
    # Social and booking profiles are judgement calls the caller owns; only
    # NONE and DIRECTORY are treated as "no website" unconditionally.
    social_counts_as_none: bool = False
    booking_counts_as_none: bool = False
    max_text_pages: int = MAX_TEXT_SEARCH_PAGES
    include_closed: bool = False
    # Text Search treats a circle as a bias, not a bound: a query for
    # "iatreio Agia Triada" happily returns the Agia Triada district of
    # Thessaloniki 13 km away. Membership is therefore decided here, on
    # measured coordinates, not on which query happened to surface a result.
    strict_area_filter: bool = True
    # Text queries surface the locality itself, the church named after it and
    # any business whose name scored well. Keep only listings with a medical
    # signal; excluded rows are reported, never silently dropped.
    relevance_filter: bool = True
    # A full Nearby page means "capped", not "complete". Re-search the area as
    # a grid of smaller circles so the cap stops hiding practices.
    subdivide_on_truncation: bool = True
    subdivision_factor: int = 3
    max_subdivision_depth: int = 1


@dataclass(slots=True)
class Listing:
    """A deduplicated business plus the verdict on its web presence."""

    place: PlaceResult
    website_kind: WebsiteKind
    areas: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    distance_m: float | None = None
    medical_signal: str | None = None
    relevance: str = "confirmed"
    area_source: str = "distance"

    def to_dict(self) -> dict[str, Any]:
        p = self.place
        return {
            "place_id": p.place_id,
            "name": p.name,
            "address": p.address,
            "phone": p.phone,
            "website": p.website,
            "website_kind": self.website_kind.value,
            "primary_type": p.primary_type,
            "types": p.types,
            "rating": p.rating,
            "reviews_count": p.reviews_count,
            "business_status": p.business_status,
            "maps_uri": p.maps_uri,
            "latitude": p.latitude,
            "longitude": p.longitude,
            # Order is meaningful: the first entry is the locality the
            # address names, so it must not be alphabetised away.
            "area": self.areas[0] if self.areas else "",
            "areas": list(self.areas),
            "area_source": self.area_source,
            "distance_m": round(self.distance_m) if self.distance_m is not None else None,
            "relevance": self.relevance,
            "medical_signal": self.medical_signal,
            "found_by": sorted(self.sources),
        }


@dataclass(slots=True)
class SweepReport:
    """Sweep output, buckets kept separate so nothing is silently merged."""

    no_website: list[Listing] = field(default_factory=list)
    borderline: list[Listing] = field(default_factory=list)
    has_website: list[Listing] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded_non_medical: list[dict[str, Any]] = field(default_factory=list)
    request_count: int = 0
    areas_resolved: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.no_website) + len(self.borderline) + len(self.has_website)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total_found": self.total,
                "no_website": len(self.no_website),
                "no_website_confirmed": sum(
                    1 for x in self.no_website if x.relevance == "confirmed"
                ),
                "no_website_needs_review": sum(
                    1 for x in self.no_website if x.relevance == "review"
                ),
                "borderline": len(self.borderline),
                "has_website": len(self.has_website),
                "excluded_non_medical": len(self.excluded_non_medical),
                "api_requests": self.request_count,
            },
            "no_website": [x.to_dict() for x in self.no_website],
            "borderline": [x.to_dict() for x in self.borderline],
            "has_website": [x.to_dict() for x in self.has_website],
            "areas_resolved": self.areas_resolved,
            "excluded_non_medical": self.excluded_non_medical,
            "warnings": self.warnings,
        }


class _Accumulator:
    """Union of every pass, keyed by place id."""

    def __init__(self) -> None:
        self._by_id: dict[str, Listing] = {}

    def add(self, place: PlaceResult, area: str, source: str) -> None:
        existing = self._by_id.get(place.place_id)
        if existing is None:
            self._by_id[place.place_id] = Listing(
                place=place,
                website_kind=classify_website(place.website),
                areas=[area],
                sources=[source],
            )
            return
        # The same practice legitimately turns up in several passes and in
        # overlapping areas; record where without duplicating the business.
        if area not in existing.areas:
            existing.areas.append(area)
        if source not in existing.sources:
            existing.sources.append(source)
        # A later pass may carry a website an earlier, sparser one lacked.
        if not existing.place.website and place.website:
            existing.place.website = place.website
            existing.website_kind = classify_website(place.website)

    def listings(self) -> list[Listing]:
        return list(self._by_id.values())


def _subdivide(
    lat: float, lng: float, radius_m: float, factor: int
) -> list[tuple[float, float, float]]:
    """
    Cover one circle with a factor x factor grid of smaller circles.

    Each sub-circle is sized to cover its grid square corner-to-corner
    (half-width * sqrt(2)), so the union covers the original with overlap
    rather than leaving diagonal gaps between cells.
    """
    factor = max(2, factor)
    half_width = radius_m / factor
    sub_radius = half_width * math.sqrt(2)
    metres_per_deg_lat = 111320.0
    metres_per_deg_lng = max(
        1.0, metres_per_deg_lat * math.cos(math.radians(lat))
    )

    cells: list[tuple[float, float, float]] = []
    for i in range(factor):
        for j in range(factor):
            offset_y = (2 * i + 1 - factor) * half_width
            offset_x = (2 * j + 1 - factor) * half_width
            cells.append(
                (
                    lat + offset_y / metres_per_deg_lat,
                    lng + offset_x / metres_per_deg_lng,
                    sub_radius,
                )
            )
    return cells


async def _nearby_pass(
    client: GooglePlacesClient,
    acc: _Accumulator,
    report: SweepReport,
    config: SweepConfig,
    area_name: str,
    lat: float,
    lng: float,
    radius_m: float,
    place_type: str,
    depth: int = 0,
) -> None:
    """One typed Nearby call, subdividing when the response comes back full."""
    try:
        results, truncated = await client.search_nearby(
            lat, lng, radius_m, [place_type]
        )
    except Exception as exc:
        report.warnings.append(
            f"{area_name}: nearby search for type '{place_type}' "
            f"at r={int(radius_m)}m failed: {exc}"
        )
        return

    for place in results:
        acc.add(place, area_name, f"nearby:{place_type}")

    if not truncated:
        return

    if not config.subdivide_on_truncation or depth >= config.max_subdivision_depth:
        report.warnings.append(
            f"{area_name}: nearby search for '{place_type}' still returned a full "
            f"page at r={int(radius_m)}m after {depth} subdivision(s); results for "
            f"this type may remain incomplete."
        )
        return

    for sub_lat, sub_lng, sub_radius in _subdivide(
        lat, lng, radius_m, config.subdivision_factor
    ):
        await _nearby_pass(
            client, acc, report, config, area_name,
            sub_lat, sub_lng, sub_radius, place_type, depth + 1,
        )


def _bucket(listing: Listing, config: SweepConfig) -> str:
    kind = listing.website_kind
    if kind in (WebsiteKind.NONE, WebsiteKind.DIRECTORY):
        return "no_website"
    if kind is WebsiteKind.SOCIAL:
        return "no_website" if config.social_counts_as_none else "borderline"
    if kind is WebsiteKind.BOOKING:
        return "no_website" if config.booking_counts_as_none else "borderline"
    return "has_website"


async def run_places_sweep(
    client: GooglePlacesClient, config: SweepConfig | None = None
) -> SweepReport:
    """
    Sweep every configured area and classify each business's web presence.

    Two passes per area -- typed Nearby Search, then Greek text queries --
    unioned by place id. Both are needed: the type filter is precise but only
    as good as Google's labelling, and the text queries catch what it missed.
    """
    config = config or SweepConfig()
    report = SweepReport()
    acc = _Accumulator()

    for area in config.areas:
        lat, lng = area.latitude, area.longitude
        if lat is None or lng is None:
            resolved = await client.resolve_area_center(area.query)
            if resolved is None:
                report.warnings.append(
                    f"{area.name}: could not resolve a centre for "
                    f"'{area.query}' -- area skipped, no results counted for it."
                )
                continue
            lat, lng = resolved
        report.areas_resolved[area.name] = {
            "query": area.query,
            "latitude": lat,
            "longitude": lng,
            "radius_m": area.radius_m,
        }

        # Pass 1 -- typed nearby search, one call per type so each type gets
        # its own 20-result budget rather than competing for a shared one.
        for place_type in config.included_types:
            await _nearby_pass(
                client, acc, report, config,
                area.name, lat, lng, area.radius_m, place_type,
            )

        # Pass 2 -- Greek text queries, biased to the same circle.
        for query in config.text_queries:
            full_query = f"{query} {area.query}"
            try:
                results = await client.search_text(
                    full_query,
                    latitude=lat,
                    longitude=lng,
                    radius_m=area.radius_m,
                    max_pages=config.max_text_pages,
                )
            except Exception as exc:
                report.warnings.append(
                    f"{area.name}: text search '{full_query}' failed: {exc}"
                )
                continue
            for place in results:
                acc.add(place, area.name, f"text:{query}")

    dropped_far = 0
    dropped_no_coords = 0
    for listing in acc.listings():
        status = (listing.place.business_status or "").upper()
        if not config.include_closed and status in (
            "CLOSED_PERMANENTLY",
            "CLOSED_TEMPORARILY",
        ):
            continue

        if config.strict_area_filter:
            lat, lng = listing.place.latitude, listing.place.longitude
            if lat is None or lng is None:
                # Without coordinates there is no way to tell this listing is
                # in the target area rather than a same-named one elsewhere.
                dropped_no_coords += 1
                continue
            inside: list[tuple[str, float]] = []
            for area_name, centre in report.areas_resolved.items():
                distance = _haversine_m(
                    centre["latitude"], centre["longitude"], lat, lng
                )
                if distance <= centre["radius_m"]:
                    inside.append((area_name, distance))
            if not inside:
                dropped_far += 1
                continue
            # Membership and distance come from the coordinates, not from
            # whichever area's query happened to surface the listing.
            inside.sort(key=lambda item: item[1])
            listing.distance_m = inside[0][1]

            # Prefer the locality Google printed in the address over the
            # nearest centroid; fall back to proximity only when the address
            # names none of the areas.
            folded_address = fold_greek(listing.place.address)
            named = [
                area.name
                for area in config.areas
                if area.address_tokens
                and any(token in folded_address for token in area.address_tokens)
            ]
            ordered = [name for name, _ in inside]
            if named:
                listing.areas = named + [n for n in ordered if n not in named]
                listing.area_source = "address"
            else:
                listing.areas = ordered
                listing.area_source = "distance"

        tier, signal = medical_signal(listing.place.name, listing.place.types)
        listing.medical_signal = signal
        listing.relevance = tier
        if config.relevance_filter and tier == "excluded":
            report.excluded_non_medical.append(
                {
                    "name": listing.place.name,
                    "address": listing.place.address,
                    "types": listing.place.types,
                    "reason": signal or "no medical signal",
                    "place_id": listing.place.place_id,
                }
            )
            continue

        target = _bucket(listing, config)
        getattr(report, target).append(listing)

    if dropped_far:
        report.warnings.append(
            f"Geographic filter removed {dropped_far} listing(s) outside every "
            f"area radius (Text Search biases, it does not bound). "
            f"Pass strict_area_filter=False to keep them."
        )
    if report.excluded_non_medical:
        report.warnings.append(
            f"Relevance filter set aside {len(report.excluded_non_medical)} listing(s) "
            f"with no medical signal (see excluded_non_medical to audit them)."
        )
    if dropped_no_coords:
        report.warnings.append(
            f"Dropped {dropped_no_coords} listing(s) that returned no coordinates "
            f"and so could not be placed inside an area."
        )

    for bucket in (report.no_website, report.borderline, report.has_website):
        bucket.sort(
            key=lambda x: (x.areas[0] if x.areas else "", x.distance_m or 0.0, x.place.name)
        )

    report.request_count = client.request_count
    logger.info(
        "Sweep complete: %d businesses, %d without a website, %d API requests",
        report.total, len(report.no_website), report.request_count,
    )
    return report
