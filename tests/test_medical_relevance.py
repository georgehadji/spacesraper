"""
Relevance grading, pinned to real rows from the Thermaikos sweep.

Every listing below was returned by a live Places query for the three
localities, so these cases fix the two failure modes that actually occurred:
a church and a locality arriving as "doctors", and real practices being
discarded because Google typed them `medical_clinic` rather than `doctor`.
"""

import pytest

from src.domain.medical_relevance import (
    fold_greek,
    looks_medical,
    medical_signal,
)


@pytest.mark.parametrize(
    "name,types",
    [
        # Name vocabulary, which outranks types.
        ("Αποστολούδης Κυριάκος, Ορθοπαιδικός", []),
        ("ΜΙΚΡΟΒΙΟΛΟΓΙΚΗ ΔΙΑΓΝΩΣΗ", []),
        ("Ορθοδοντικό Ιατρείο - Ταράουνε Φ.", []),
        ("ΤΡΟΥΛΙΑΛΗΣ ΜΙΧΑΗΛ ΟΔΟΝΤΙΑΤΡΟΣ", []),
        ("Καντά Ουρανία Νευρολόγος", []),
        # "Diagnosis" and "diagnostic" share a stem; both must match.
        ("Λεντούδη Αικατερίνη - Διάγνωσις", ["medical_clinic", "health"]),
        ("Βιοδιάγνωση", ["medical_clinic", "health"]),
        # Precise Google types stand alone.
        ("costas.markou_obgyn", ["doctor"]),
        ("Some Lab", ["medical_lab"]),
    ],
)
def test_confirmed(name, types):
    assert medical_signal(name, types)[0] == "confirmed"


@pytest.mark.parametrize(
    "name,types",
    [
        # `medical_clinic` and `health` are applied loosely by Google, so they
        # buy a human look rather than a place on the answer.
        ("Υγεία Περαίας", ["medical_clinic", "health"]),
        ("Ανάλυση", ["health"]),
        ("Χαλκιδαίου Στυλιανή-Μαρίνα", ["medical_clinic", "health"]),
        # A bar Google really did type as medical_clinic in this dataset.
        ("Hools outpost", ["medical_clinic", "health"]),
        # Psychologists are not physicians; the caller decides.
        ("Ζωή Καραμπασιάδου | Ψυχολόγος Α.Π.Θ.", ["medical_clinic", "health"]),
    ],
)
def test_review(name, types):
    assert medical_signal(name, types)[0] == "review"


@pytest.mark.parametrize(
    "name,types",
    [
        ("Ιερός Ναός Αγίας Τριάδος", ["church", "place_of_worship"]),
        ("Αγία Τριάδα", ["locality", "political"]),
        ("Φαρμακείο Περαία - Σαρδέλης", ["pharmacy", "health"]),
        ("Αναγνωστίδης Κτηματομεσιτικές", ["real_estate_agency"]),
        ("Κοινοτικό Κατάστημα Αγίας Τριάδας", ["community_center"]),
        ("Evdermia", ["manufacturer"]),
        ("Some Bar", []),
    ],
)
def test_excluded(name, types):
    assert medical_signal(name, types)[0] == "excluded"


def test_name_signal_beats_a_non_medical_type():
    """A pharmacy that calls itself an iatreio still surfaces for review."""
    tier, signal = medical_signal("Ιατρείο κaι Φαρμακείο", ["pharmacy"])
    assert tier == "confirmed"
    assert signal == "name:ιατρ"


def test_signal_names_the_evidence():
    assert medical_signal("Καντά Ουρανία Νευρολόγος", [])[1] == "name:νευρολογ"
    assert medical_signal("X", ["dentist"])[1] == "type:dentist"


def test_accent_and_case_folding():
    assert fold_greek("Περαία") == "περαια"
    assert fold_greek("ΝΕΟΙ ΕΠΙΒΆΤΕΣ") == "νεοι επιβατες"
    # All-caps, accented and mixed spellings must reach the same verdict.
    assert looks_medical("ΟΔΟΝΤΊΑΤΡΟΣ ΠΕΡΑΊΑ", []) is True
    assert looks_medical("οδοντιατρος περαια", []) is True


def test_looks_medical_spans_confirmed_and_review():
    assert looks_medical("X", ["doctor"]) is True
    assert looks_medical("X", ["health"]) is True
    assert looks_medical("X", ["church"]) is False
