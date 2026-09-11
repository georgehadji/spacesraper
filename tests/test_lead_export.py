"""
Lead export: the cleanup that turns a sweep into a call sheet.

Pinned to the Thermaikos run, where both problems this module solves showed
up: one doctor listed twice under one phone number, and businesses Google
typed as medical that plainly are not.
"""

import csv

import pytest

from src.application.lead_export import (
    ExclusionRule,
    build_leads,
    dedupe_by_phone,
    load_exclusions,
    write_leads_csv,
)
from src.application.place_sweep import Listing, SweepReport
from src.domain.website_classifier import WebsiteKind, classify_website
from src.infrastructure.places.google_places import PlaceResult


def listing(name, phone=None, website=None, *, relevance="confirmed",
            area="Peraia", address="Περαία 570 19", rating=None, reviews=None):
    place = PlaceResult(
        place_id=f"id-{name}", name=name, address=address, website=website,
        phone=phone, rating=rating, reviews_count=reviews,
        maps_uri="https://maps.google.com/?cid=1",
    )
    return Listing(
        place=place, website_kind=classify_website(website),
        areas=[area], relevance=relevance,
    )


def report(*listings):
    r = SweepReport()
    r.no_website = list(listings)
    return r


# --- deduplication -------------------------------------------------------

def test_same_phone_collapses_to_one_row():
    """Regression: one gynaecologist held two Google profiles on one line."""
    a = listing("costas.markou_obgyn", "2392 021745")
    b = listing("Μάρκου Κωνσταντίνος Γυναικολόγος", "2392 021745")
    kept, merges = dedupe_by_phone([a, b])
    assert [x.place.name for x in kept] == ["Μάρκου Κωνσταντίνος Γυναικολόγος"]
    assert merges == [("Μάρκου Κωνσταντίνος Γυναικολόγος", "costas.markou_obgyn")]


def test_phone_formatting_does_not_hide_a_duplicate():
    """Google prints the same line as 2392021745, 2392 021745, +30 2392 021745."""
    kept, _ = dedupe_by_phone([
        listing("Α", "2392021745"),
        listing("Β", "2392 021745"),
        listing("Γ", "+30 2392 021745"),
    ])
    assert len(kept) == 1


def test_a_confirmed_record_wins_over_one_needing_review():
    kept, _ = dedupe_by_phone([
        listing("Τζαναβάρη Κατερίνα", "694 537 5427", relevance="review"),
        listing("Τζαναβάρη Αικατερίνη Ρευματολόγος", "694 537 5427"),
    ])
    assert [x.place.name for x in kept] == ["Τζαναβάρη Αικατερίνη Ρευματολόγος"]


def test_the_fuller_record_wins_when_both_are_confirmed():
    kept, _ = dedupe_by_phone([
        listing("Σ", "2392 000001"),
        listing("Σ ιατρείο", "2392 000001", rating=4.5, reviews=12),
    ])
    assert [x.place.name for x in kept] == ["Σ ιατρείο"]


def test_listings_without_a_phone_are_never_merged():
    """No number is not evidence that two businesses are the same one."""
    kept, merges = dedupe_by_phone([listing("Α"), listing("Β"), listing("Γ")])
    assert len(kept) == 3 and merges == []


def test_distinct_phones_are_left_alone():
    kept, _ = dedupe_by_phone([
        listing("Α", "2392 000001"), listing("Β", "2392 000002"),
    ])
    assert len(kept) == 2


# --- exclusions ----------------------------------------------------------

def test_exclusion_file_parsing(tmp_path):
    path = tmp_path / "exclude.txt"
    path.write_text(
        "# businesses Google mistyped as medical\n"
        "Aigli hotel | ξενοδοχείο\n"
        "ΚΟΥΡΕΙΟ ΣΑΜΑΡΙΑΣ|κουρείο\n"
        "\n"
        "SMART PETS\n"
        "   # indented comment\n",
        encoding="utf-8",
    )
    rules = load_exclusions(path)
    assert [(r.prefix, r.reason) for r in rules] == [
        ("Aigli hotel", "ξενοδοχείο"),
        ("ΚΟΥΡΕΙΟ ΣΑΜΑΡΙΑΣ", "κουρείο"),
        ("SMART PETS", ""),
    ]


def test_exclusions_drop_by_prefix_and_record_the_reason():
    result = build_leads(
        report(
            listing("Aigli hotel"),
            listing("Ιατρείο Α", "2392 000001"),
            listing("ΚΟΥΡΕΙΟ ΣΑΜΑΡΙΑΣ / SAMARIAS BARBERSHOP", "2392 000002"),
        ),
        exclusions=[
            ExclusionRule("Aigli hotel", "ξενοδοχείο"),
            ExclusionRule("ΚΟΥΡΕΙΟ", "κουρείο"),
        ],
    )
    assert [x.place.name for x in result.kept] == ["Ιατρείο Α"]
    assert result.excluded == [
        ("Aigli hotel", "ξενοδοχείο"),
        ("ΚΟΥΡΕΙΟ ΣΑΜΑΡΙΑΣ / SAMARIAS BARBERSHOP", "κουρείο"),
    ]


def test_an_excluded_name_is_reported_not_silently_dropped():
    result = build_leads(report(listing("Χ")), exclusions=[ExclusionRule("Χ")])
    assert result.excluded == [("Χ", "εξαιρέθηκε από τον χρήστη")]
    assert result.summary["excluded"] == 1


def test_a_prefix_does_not_match_mid_name():
    """`ΚΟΥΡΕΙΟ` must not drop `ΜΙΚΡΟΒΙΟΛΟΓΙΚΟ ΚΟΥΡΕΙΟΥ`-style names."""
    result = build_leads(
        report(listing("Ιατρείο ΚΟΥΡΕΙΟ γειτονικό", "2392 000001")),
        exclusions=[ExclusionRule("ΚΟΥΡΕΙΟ")],
    )
    assert len(result.kept) == 1


# --- CSV -----------------------------------------------------------------

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_csv_columns_and_greek_labels(tmp_path):
    path = tmp_path / "leads.csv"
    write_leads_csv(
        path,
        report(
            listing("Ιατρείο Α", "2392 000001"),
            listing("Ιατρείο Β", "2392 000002", "https://www.xo.gr/profile/1"),
            listing("Ιατρείο Γ", "2392 000003", "https://facebook.com/c"),
            listing("Ιατρείο Δ", "2392 000004", relevance="review"),
        ),
    )
    rows = read_csv(path)
    assert list(rows[0]) == [
        "Περιοχή", "Επωνυμία", "Τηλέφωνο", "Διεύθυνση", "Ιστοσελίδα",
        "Σύνδεσμος", "Ειδικότητα", "Βεβαιότητα", "Σημείωση", "Google Maps",
    ]
    by_name = {r["Επωνυμία"]: r for r in rows}
    assert by_name["Ιατρείο Α"]["Ιστοσελίδα"] == "Καμία ιστοσελίδα"
    assert by_name["Ιατρείο Β"]["Ιστοσελίδα"] == "Μόνο χρυσός οδηγός"
    assert by_name["Ιατρείο Γ"]["Ιστοσελίδα"] == "Μόνο social media"
    assert by_name["Ιατρείο Α"]["Βεβαιότητα"] == "Επιβεβαιωμένο"
    assert by_name["Ιατρείο Δ"]["Βεβαιότητα"] == "Προς έλεγχο"


def test_csv_is_written_with_a_bom_for_excel(tmp_path):
    """Without the BOM Excel renders Greek as mojibake."""
    path = tmp_path / "leads.csv"
    write_leads_csv(path, report(listing("Ιατρείο Α", "2392 000001")))
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_a_merged_duplicate_is_recorded_on_the_surviving_row(tmp_path):
    path = tmp_path / "leads.csv"
    write_leads_csv(path, report(
        listing("costas.markou_obgyn", "2392 021745"),
        listing("Μάρκου Κωνσταντίνος Γυναικολόγος", "2392 021745"),
    ))
    rows = read_csv(path)
    assert len(rows) == 1
    assert rows[0]["Σημείωση"] == "Συγχωνεύτηκε με: costas.markou_obgyn"


def test_area_labels_can_be_shortened(tmp_path):
    path = tmp_path / "leads.csv"
    write_leads_csv(
        path,
        report(listing("Ιατρείο Α", "2392 000001", area="Νέα Μηχανιώνα Θεσσαλονίκης")),
        area_names={"Νέα Μηχανιώνα Θεσσαλονίκης": "Νέα Μηχανιώνα"},
    )
    assert read_csv(path)[0]["Περιοχή"] == "Νέα Μηχανιώνα"


def test_borderline_rows_are_opt_in(tmp_path):
    r = report(listing("Ιατρείο Α", "2392 000001"))
    r.borderline = [listing("Ιατρείο Β", "2392 000002", "https://facebook.com/b")]

    write_leads_csv(tmp_path / "a.csv", r)
    assert len(read_csv(tmp_path / "a.csv")) == 1

    write_leads_csv(tmp_path / "b.csv", r, include_borderline=True)
    assert len(read_csv(tmp_path / "b.csv")) == 2


def test_summary_counts_every_outcome():
    result = build_leads(
        report(
            listing("Ιατρείο Α", "2392 000001"),
            listing("Ιατρείο Α παλιό", "2392 000001"),
            listing("Ιατρείο Β", "2392 000002", relevance="review"),
            listing("Aigli hotel"),
        ),
        exclusions=[ExclusionRule("Aigli hotel", "ξενοδοχείο")],
    )
    assert result.summary == {
        "rows": 2, "confirmed": 1, "needs_review": 1,
        "excluded": 2, "merged_duplicates": 1,
    }


@pytest.mark.parametrize("kind,label", [
    (WebsiteKind.NONE, "Καμία ιστοσελίδα"),
    (WebsiteKind.DIRECTORY, "Μόνο χρυσός οδηγός"),
    (WebsiteKind.SOCIAL, "Μόνο social media"),
    (WebsiteKind.BOOKING, "Μόνο πλατφόρμα ραντεβού"),
])
def test_every_website_kind_has_a_greek_label(kind, label):
    from src.application.lead_export import WEBSITE_KIND_EL
    assert WEBSITE_KIND_EL[kind.value] == label
