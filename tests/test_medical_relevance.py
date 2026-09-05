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


@pytest.mark.parametrize(
    "name",
    [
        "Κτηνιατρείο Χρήστος Μαρουλίδης",
        "Κτηνίατρος Παπαδόπουλος",
        "ΚΤΗΝΙΑΤΡΕΙΟ ΝΕΑΣ ΜΗΧΑΝΙΩΝΑΣ",
    ],
)
def test_a_vet_is_not_a_doctor(name):
    """
    Regression: "ktiniatreio" contains "iatr", so a veterinary clinic in Nea
    Michaniona was reported as a doctor with no website. Excluded by default;
    test_veterinary_is_opt_in covers deliberately asking for vets.
    """
    assert medical_signal(name, [])[0] == "excluded"


@pytest.mark.parametrize(
    "name,signal",
    [
        ("Ιατρείο Περαίας", "name:ιατρ"),
        ("Πολυϊατρείο", "name:πολυιατρ"),
        ("ΟΔΟΝΤΙΑΤΡΟΣ ΠΕΡΑΙΑ", "name:οδοντ"),
        ("Παιδίατρος Αρβανιτίδης", "name:παιδιατρ"),
    ],
)
def test_the_vet_exclusion_does_not_catch_real_practices(name, signal):
    assert medical_signal(name, []) == ("confirmed", signal)


@pytest.mark.parametrize(
    "name,signal",
    [
        ("Πασχαλίδης Παντελής, Εργοθεραπευτής", "name:εργοθεραπ"),
        ("Κέντρο Λογοθεραπείας Εργοθεραπείας", "name:λογοθεραπ"),
        ("ΠΡΟΤΥΠΟ ΚΕΝΤΡΟ ΦΥΣΙΚΟΘΕΡΑΠΕΙΑΣ ΜΕΡΤΖΗΣ", "name:φυσικοθεραπ"),
        ("ΛΟΓΟΘΕΡΑΠΕΙΑ-Αντάμη Ντέση", "name:λογοθεραπ"),
        ("Ποδολογικό Κέντρο Χριστίνα Βεργουλίδου", "name:ποδολογ"),
    ],
)
def test_allied_health_professions_are_confirmed(name, signal):
    """Speech, occupational, physio and podiatry practices count as practices."""
    assert medical_signal(name, []) == ("confirmed", signal)


@pytest.mark.parametrize(
    "name,types",
    [
        ("Κτηνιατρείο Χρήστος Μαρουλίδης", []),
        ("Κτηνίατρος Παπαδόπουλος", []),
        ("ΚΤΗΝΙΑΤΡΕΙΟ", ["veterinary_care"]),
    ],
)
def test_veterinary_is_opt_in(name, types):
    """
    A vet treats animals, so it stays out of a doctor list by default and
    comes back only when the caller asks for it.
    """
    assert medical_signal(name, types)[0] == "excluded"
    assert medical_signal(name, types, include_veterinary=True)[0] == "confirmed"


@pytest.mark.parametrize(
    "name,types,reason",
    [
        # Greek pharmacies carry veterinary_care because they stock animal
        # products; three of them entered a live vet-inclusive run this way.
        ("Φαρμακείο Περαία - Σαρδέλης", ["pharmacy", "veterinary_care", "store"], "type:pharmacy"),
        ("Φαρμακείο Νικόλαος Βασιλείου", ["pharmacy", "veterinary_care"], "type:pharmacy"),
        ("SMART PETS", ["veterinary_care", "pet_store"], "type:pet_store"),
    ],
)
def test_asking_for_vets_does_not_admit_pharmacies_or_pet_shops(name, types, reason):
    assert medical_signal(name, types, include_veterinary=True) == ("excluded", reason)


def test_a_bare_veterinary_type_is_only_worth_a_review():
    """`veterinary_care` is applied as loosely as `medical_clinic`."""
    assert medical_signal("Άλφα", ["veterinary_care"], include_veterinary=True) == (
        "review",
        "type:veterinary_care",
    )


def test_including_vets_does_not_disturb_anything_else():
    """The opt-in must add animal clinics, not re-grade human practices."""
    for name, types in [
        ("ΟΔΟΝΤΙΑΤΡΟΣ ΠΕΡΑΙΑ", []),
        ("Ιερός Ναός Αγίας Τριάδος", ["church"]),
        ("Υγεία Περαίας", ["medical_clinic"]),
        ("Φαρμακείο Περαία", ["pharmacy"]),
    ]:
        assert medical_signal(name, types) == medical_signal(
            name, types, include_veterinary=True
        )


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
