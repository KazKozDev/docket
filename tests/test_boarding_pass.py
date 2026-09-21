"""Boarding passes are the document type this pipeline's target deployment
actually runs on — IAG is British Airways, Iberia, Vueling and Aer Lingus.

They validate differently from an invoice: there is no arithmetic, but
almost every field comes from a controlled vocabulary, so a malformed IATA
code or record locator is proof of a bad read rather than a hint.
"""
from datetime import datetime

from docket.classify import classify
from docket.schemas import BoardingPass, DocType
from docket.validate import validate

PASS_TEXT = """BOARDING PASS
Passenger: GARCIA/MARIA MS
Flight IB3241   Booking ref: X4H2QP
BCN Barcelona  ->  LHR London Heathrow
Departure: 2026-05-14 08:35   Boarding: 08:05
Gate B24   Seat 12C   Economy
"""


def _pass(**overrides) -> BoardingPass:
    fields = dict(
        passenger_name="GARCIA/MARIA MS",
        booking_reference="X4H2QP",
        flight_number="IB3241",
        departure_airport="BCN",
        arrival_airport="LHR",
        departure_datetime=datetime(2026, 5, 14, 8, 35),
        seat="12C",
        gate="B24",
    )
    fields.update(overrides)
    return BoardingPass(**fields)


def test_classified_by_rules_in_english():
    result = classify(PASS_TEXT)
    assert result.doc_type == DocType.BOARDING_PASS
    assert result.method == "rules"


def test_classified_by_rules_in_spanish():
    spanish = """TARJETA DE EMBARQUE
    Pasajero: GARCIA/MARIA MS
    Vuelo IB3241   Puerta de embarque B24
    Asiento 12C
    """
    result = classify(spanish)
    assert result.doc_type == DocType.BOARDING_PASS
    assert result.method == "rules"


def test_a_well_formed_pass_validates_clean():
    assert validate(_pass(), PASS_TEXT) == []


def test_malformed_iata_code_is_flagged():
    issues = validate(_pass(departure_airport="Barcelona"), PASS_TEXT)
    assert any(i.field == "departure_airport" for i in issues)


def test_same_origin_and_destination_is_flagged():
    issues = validate(_pass(arrival_airport="BCN"), PASS_TEXT)
    assert any(i.field == "arrival_airport" for i in issues)


def test_malformed_flight_number_is_flagged():
    issues = validate(_pass(flight_number="FLIGHT 3241"), PASS_TEXT)
    assert any(i.field == "flight_number" for i in issues)


def test_malformed_record_locator_is_flagged():
    issues = validate(_pass(booking_reference="X4H2"), PASS_TEXT)
    assert any(i.field == "booking_reference" for i in issues)


def test_record_locator_absent_from_the_document_is_flagged():
    """Same grounding principle as contracts: a six-character code the model
    invented is well-formed but still wrong, and only the page can say so.
    """
    issues = validate(_pass(booking_reference="ZZ9QQ1"), PASS_TEXT)
    assert any(
        i.field == "booking_reference" and "does not appear" in i.message
        for i in issues
    )


def test_departure_far_in_the_future_is_flagged():
    issues = validate(_pass(departure_datetime=datetime(2099, 5, 14, 8, 35)), PASS_TEXT)
    assert any(i.field == "departure_datetime" for i in issues)
