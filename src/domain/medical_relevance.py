# Project: Spacescraper (Domain)
# Role: Decide whether a Maps listing is a medical practice.
#
# Text queries are recall tools, not filters: searching "iatreio <locality>"
# returns the locality itself, the church named after it, and whatever else
# scored well on the string. Two independent signals decide membership here --
# Google's own place types, and the practice-naming vocabulary Greek listings
# actually use -- because either one alone has a known blind spot: types are
# unevenly applied to small practices, and names are free text.

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "fold_greek",
    "MEDICAL_PLACE_TYPES",
    "AMBIGUOUS_PLACE_TYPES",
    "NON_MEDICAL_PLACE_TYPES",
    "VETERINARY_PLACE_TYPES",
    "looks_medical",
    "medical_signal",
]

# Precise practitioner types. Google applies these narrowly, so one of them
# is enough on its own to accept a listing.
MEDICAL_PLACE_TYPES: frozenset[str] = frozenset({
    "doctor",
    "dentist",
    "dental_clinic",
    "hospital",
    "medical_lab",
    "physiotherapist",
    "chiropractor",
})

# Applied loosely: real practices carry `medical_clinic`, but so does a bar in
# Agia Triada and every psychologist in the area. Enough to keep a listing for
# review, never enough to confirm it unaided.
AMBIGUOUS_PLACE_TYPES: frozenset[str] = frozenset({
    "medical_clinic",
    "health",
    "skin_care_clinic",
    "wellness_center",
})

# Types that settle the question the other way. Checked only when no name
# signal fired, so a pharmacy that also calls itself an iatreio still surfaces.
NON_MEDICAL_PLACE_TYPES: frozenset[str] = frozenset({
    "pharmacy",
    "drugstore",
    "real_estate_agency",
    "church",
    "place_of_worship",
    "locality",
    "political",
    "community_center",
    "event_venue",
    "manufacturer",
    "clothing_store",
    "cosmetics_store",
    "pet_store",
    "store",
    "restaurant",
    "bar",
    "cafe",
    "gym",
    "beauty_salon",
    "hair_salon",
    "lodging",
    "school",
    "insurance_agency",
    "lawyer",
})

# Greek practice vocabulary, accent-stripped and lowercased before matching so
# a listing typed in capitals ("ΜΙΚΡΟΒΙΟΛΟΓΙΚΗ ΔΙΑΓΝΩΣΗ") matches the same
# pattern as a mixed-case one.
_NAME_PATTERNS: tuple[str, ...] = (
    # "ktiniatreio" (veterinary clinic) contains "iatr"; the lookbehind keeps
    # a vet out of a list of doctors.
    r"(?<!κτην)ιατρ",   # iatros / iatreio / iatriko / polyiatreio
    r"γιατρ",           # giatros
    r"οδοντ",           # odontiatros / odontiatreio
    r"ορθοπ",           # orthopaidikos
    r"ορθοδοντ",        # orthodontikos
    r"οφθαλμ",          # ofthalmiatros
    r"παιδιατρ",        # paidiatros
    r"χειρουργ",        # cheirourgos
    r"μαιευτ",          # maieutiras
    r"καρδιολογ",
    r"παθολογ",
    r"γυναικολογ",
    r"δερματολογ",
    r"νευρολογ",
    r"ουρολογ",
    r"πνευμονολογ",
    r"ενδοκρινολογ",
    r"γαστρεντερολογ",
    r"ρευματολογ",
    r"ογκολογ",
    r"αιματολογ",
    r"μικροβιολογ",
    r"βιοπαθολογ",
    r"ακτινολογ",
    r"αναισθησιολογ",
    r"ωτορινολαρυγγολογ",
    r"αλλεργιολογ",
    r"νεφρολογ",
    r"ψυχιατρ",         # psychiatrist is a physician; psychologist is not
    r"διαγνω",          # diagnosi / diagnostiko kentro / viodiagnosi
    r"πολυιατρ",
    r"κλινικ",
    r"νοσοκομ",
    r"φυσικοθεραπ",
    r"λογοθεραπ",
    r"εργοθεραπ",       # ergotherapeutis (occupational therapy)
    r"ποδολογ",         # podologos (podiatry/chiropody)
    r"κεντρο υγειας",
    r"\bωρλ\b",
    r"\bdr\b",
    r"\bδρ\b",
    r"\bmd\b",
    r"clinic",
    r"medical",
    r"dental",
)

_NAME_RE = re.compile("|".join(_NAME_PATTERNS))


def fold_greek(text: str) -> str:
    """Lowercase and strip Greek accents so one pattern matches every casing."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Internal alias kept for the name patterns below.
_fold = fold_greek

# Veterinary practice, kept out of the default answer: a vet treats animals,
# so a list of doctors that includes one is wrong. Opt in when the caller
# actually wants animal clinics -- the base "iatr" stem deliberately refuses
# to match inside "ktiniatreio", so this is the only route in.
_VET_RE = re.compile(r"κτηνιατρ")
VETERINARY_PLACE_TYPES: frozenset[str] = frozenset({"veterinary_care"})



def medical_signal(
    name: str,
    types: list[str] | None,
    *,
    include_veterinary: bool = False,
) -> tuple[str, str | None]:
    """
    Grade a listing as ("confirmed" | "review" | "excluded", signal).

    Three tiers rather than a yes/no because the underlying data supports
    only three. A name that says "orthopaidikos" is certain; a bare
    `medical_clinic` type is a coin-toss Google has already lost once in this
    dataset; a church is certain the other way. Collapsing the middle tier
    into either neighbour would mean either inventing doctors or dropping
    real ones, so it is handed back to the caller instead.
    """
    place_types = list(types or [])
    folded = _fold(name)

    # A name that says "ktiniatreio" is definitive, so it outranks the type
    # list the way the human-practice vocabulary does.
    if include_veterinary:
        vet = _VET_RE.search(folded)
        if vet:
            return "confirmed", f"name:{vet.group(0)}"

    # A practice-vocabulary name outranks every type signal: it is written by
    # the business about itself, and Greek listings are typed inconsistently.
    match = _NAME_RE.search(folded)
    if match:
        return "confirmed", f"name:{match.group(0)}"

    for place_type in place_types:
        if place_type in MEDICAL_PLACE_TYPES:
            return "confirmed", f"type:{place_type}"

    for place_type in place_types:
        if place_type in NON_MEDICAL_PLACE_TYPES:
            return "excluded", f"type:{place_type}"

    # `veterinary_care` is applied as loosely as `medical_clinic`: Greek
    # pharmacies carry it because they stock animal products, and so do pet
    # shops. Checked after the non-medical types so a pharmacy is still a
    # pharmacy, and worth only a review on its own.
    if include_veterinary:
        for place_type in place_types:
            if place_type in VETERINARY_PLACE_TYPES:
                return "review", f"type:{place_type}"

    for place_type in place_types:
        if place_type in AMBIGUOUS_PLACE_TYPES:
            return "review", f"type:{place_type}"

    return "excluded", None


def looks_medical(
    name: str, types: list[str] | None, *, include_veterinary: bool = False
) -> bool:
    """True when the listing is confirmed medical or worth a human look."""
    tier, _ = medical_signal(name, types, include_veterinary=include_veterinary)
    return tier in ("confirmed", "review")
