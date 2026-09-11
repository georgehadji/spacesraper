# Project: Spacescraper (Application)
# Role: Turn a sweep report into a contact list someone can actually work from.
#
# The sweep answers "who has no website". Calling those businesses needs two
# more things the raw export does not do, and both were being done by hand:
#
#   1. One row per business. Google lists a practice twice when it has an old
#      and a new profile -- "costas.markou_obgyn" and "Markou Konstantinos
#      Gynaikologos" are one doctor with one phone. Two rows means two calls
#      to the same person.
#   2. Judgement the data cannot supply. Google types a bar as medical_clinic
#      and a barbershop as health. Which listings are really practices is the
#      operator's call, so it arrives as an exclusion file rather than a
#      hardcoded list that would silently rot.

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.application.place_sweep import Listing, SweepReport

__all__ = [
    "ExclusionRule",
    "LeadExportResult",
    "dedupe_by_phone",
    "load_exclusions",
    "write_leads_csv",
]

# Column headers, in Greek: the people working these lists read Greek, and the
# file is opened in Excel rather than parsed.
GREEK_COLUMNS = [
    "Περιοχή",
    "Επωνυμία",
    "Τηλέφωνο",
    "Διεύθυνση",
    "Ιστοσελίδα",
    "Σύνδεσμος",
    "Ειδικότητα",
    "Βεβαιότητα",
    "Σημείωση",
    "Google Maps",
]

WEBSITE_KIND_EL = {
    "none": "Καμία ιστοσελίδα",
    "directory": "Μόνο χρυσός οδηγός",
    "social": "Μόνο social media",
    "booking": "Μόνο πλατφόρμα ραντεβού",
}

RELEVANCE_EL = {"confirmed": "Επιβεβαιωμένο", "review": "Προς έλεγχο"}


@dataclass(slots=True)
class ExclusionRule:
    """A name prefix to drop, with the reason recorded for the audit trail."""

    prefix: str
    reason: str = ""

    def matches(self, name: str) -> bool:
        return name.startswith(self.prefix)


@dataclass(slots=True)
class LeadExportResult:
    """What the export kept, and what it dropped and why."""

    kept: list[Listing] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    merged: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        confirmed = sum(1 for x in self.kept if x.relevance == "confirmed")
        return {
            "rows": len(self.kept),
            "confirmed": confirmed,
            "needs_review": len(self.kept) - confirmed,
            "excluded": len(self.excluded),
            "merged_duplicates": len(self.merged),
        }


def load_exclusions(path: str | Path) -> list[ExclusionRule]:
    """
    Read an exclusion file: one name prefix per line, `#` starts a comment.

    An optional reason follows a `|`, so the export can say why a business was
    dropped instead of it just vanishing:

        Aigli hotel | ξενοδοχείο
        ΚΟΥΡΕΙΟ | κουρείο
    """
    rules: list[ExclusionRule] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        prefix, _, reason = line.partition("|")
        prefix = prefix.strip()
        if prefix:
            rules.append(ExclusionRule(prefix=prefix, reason=reason.strip()))
    return rules


# Greek subscriber numbers are ten digits. The client asks for regionCode GR,
# but Google falls back to internationalPhoneNumber when it has no national
# one, so the same line arrives as both "2392 021745" and "+30 2392 021745".
_GR_COUNTRY_CODE = "30"
_GR_NUMBER_LENGTH = 10


def _digits(phone: str | None) -> str:
    """
    Reduce a phone number to comparable digits.

    Strips the international prefix so a national and an international
    rendering of one line compare equal; without this a practice listed twice
    under both forms would survive deduplication as two contacts.
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if (
        len(digits) > _GR_NUMBER_LENGTH
        and digits.startswith(_GR_COUNTRY_CODE)
        and len(digits) - len(_GR_COUNTRY_CODE) == _GR_NUMBER_LENGTH
    ):
        digits = digits[len(_GR_COUNTRY_CODE):]
    return digits


def _richness(listing: Listing) -> tuple:
    """
    Rank two records of the same business.

    Prefer a confirmed grading, then the record carrying more fields, then the
    longer name -- "Markou Konstantinos Gynaikologos" is worth more on a call
    sheet than the username-style "costas.markou_obgyn".
    """
    place = listing.place
    filled = sum(
        1
        for value in (place.phone, place.address, place.rating, place.reviews_count)
        if value
    )
    return (listing.relevance == "confirmed", filled, len(place.name))


def dedupe_by_phone(
    listings: Iterable[Listing],
) -> tuple[list[Listing], list[tuple[str, str]]]:
    """
    Collapse listings that share a phone number, keeping the richest record.

    Returns (kept, merges) where each merge is (kept_name, dropped_name).
    Listings with no phone are always kept: absence of a number is not
    evidence that two businesses are the same one.
    """
    by_phone: dict[str, list[Listing]] = {}
    no_phone: list[Listing] = []
    for listing in listings:
        digits = _digits(listing.place.phone)
        if digits:
            by_phone.setdefault(digits, []).append(listing)
        else:
            no_phone.append(listing)

    kept: list[Listing] = []
    merges: list[tuple[str, str]] = []
    for group in by_phone.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=_richness, reverse=True)
        winner, losers = group[0], group[1:]
        kept.append(winner)
        merges.extend((winner.place.name, loser.place.name) for loser in losers)

    kept.extend(no_phone)
    return kept, merges


def build_leads(
    report: SweepReport,
    *,
    exclusions: Iterable[ExclusionRule] = (),
    area_names: dict[str, str] | None = None,
    include_borderline: bool = False,
) -> LeadExportResult:
    """Apply exclusions and phone deduplication to a sweep's no-website rows."""
    rules = list(exclusions)
    source = list(report.no_website)
    if include_borderline:
        source += list(report.borderline)

    result = LeadExportResult()
    survivors: list[Listing] = []
    for listing in source:
        rule = next((r for r in rules if r.matches(listing.place.name)), None)
        if rule is not None:
            result.excluded.append(
                (listing.place.name, rule.reason or "εξαιρέθηκε από τον χρήστη")
            )
        else:
            survivors.append(listing)

    kept, merges = dedupe_by_phone(survivors)
    for _, dropped in merges:
        result.excluded.append((dropped, "διπλότυπο (ίδιο τηλέφωνο)"))
    result.merged = merges

    labels = area_names or {}
    kept.sort(
        key=lambda x: (
            labels.get(x.areas[0] if x.areas else "", x.areas[0] if x.areas else ""),
            x.place.name,
        )
    )
    result.kept = kept
    return result


def write_leads_csv(
    path: str | Path,
    report: SweepReport,
    *,
    exclusions: Iterable[ExclusionRule] = (),
    area_names: dict[str, str] | None = None,
    include_borderline: bool = False,
) -> LeadExportResult:
    """
    Write the deduplicated Greek contact list.

    UTF-8 with a BOM: without it Excel opens Greek text as mojibake, which is
    the first thing anyone does with this file.
    """
    result = build_leads(
        report,
        exclusions=exclusions,
        area_names=area_names,
        include_borderline=include_borderline,
    )
    merged_by_name = {winner: dropped for winner, dropped in result.merged}
    labels = area_names or {}

    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GREEK_COLUMNS)
        writer.writeheader()
        for listing in result.kept:
            place = listing.place
            area = listing.areas[0] if listing.areas else ""
            merged = merged_by_name.get(place.name)
            writer.writerow(
                {
                    "Περιοχή": labels.get(area, area),
                    "Επωνυμία": place.name,
                    "Τηλέφωνο": place.phone or "",
                    "Διεύθυνση": place.address or "",
                    "Ιστοσελίδα": WEBSITE_KIND_EL.get(
                        listing.website_kind.value, listing.website_kind.value
                    ),
                    "Σύνδεσμος": place.website or "",
                    # The derived specialty beats Google's type, which
                    # collapses every physician to a single "Ιατρός".
                    "Ειδικότητα": listing.specialty or place.primary_type or "",
                    "Βεβαιότητα": RELEVANCE_EL.get(listing.relevance, listing.relevance),
                    "Σημείωση": f"Συγχωνεύτηκε με: {merged}" if merged else "",
                    "Google Maps": place.maps_uri or "",
                }
            )
    return result
