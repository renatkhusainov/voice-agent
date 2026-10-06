"""experiments/hl7v2/hl7.py: parse the two samples, generate an SIU^S12 from
the captured FHIR booking, and prove it by re-parsing. Offline: the fixture,
not HAPI."""

import copy
import json
from datetime import UTC, date, datetime

import pytest
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.exceptions import HL7apyException
from hl7apy.parser import parse_message

from experiments.hl7v2.hl7 import (
    SAMPLES,
    build_siu_s12,
    load,
    parse_adt,
    parse_oru,
    reparse_siu,
    wire,
)

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)


@pytest.fixture
def booking():
    return json.loads((SAMPLES / "fhir_booking_1350.json").read_text())


def test_adt_a04_gives_name_and_date_of_birth():
    adt = parse_adt(load("adt_a04.hl7"))

    assert adt["message_type"] == "ADT^A04^ADT_A01"  # A04 reuses the A01 structure
    assert adt["patient"] == {"id": "MRN100234", "family": "Garcia", "given": "Maria",
                              "dob": date(1982, 3, 14), "sex": "F"}
    assert adt["patient_class"] == "O"


def test_oru_r01_gives_patient_and_observations():
    oru = parse_oru(load("oru_r01.hl7"))

    assert oru["patient"]["family"] == "Garcia" and oru["patient"]["dob"] == date(1982, 3, 14)
    inr, pt = oru["observations"]
    assert inr == {"code": "6301-6", "name": "INR in Platelet poor plasma by Coagulation assay",
                   "value": "2.4", "units": "{INR}", "reference_range": "2.0-3.0",
                   "abnormal_flag": "N", "status": "F"}
    assert (pt["value"], pt["abnormal_flag"]) == ("27.8", "H")


def test_validation_rejects_a_message_missing_a_required_field():
    broken = load("adt_a04.hl7").replace("Garcia^Maria^Elena^^Ms.", "")  # PID-5 emptied

    with pytest.raises(HL7apyException):
        parse_adt(broken)


def test_siu_s12_round_trips_the_booking(booking):
    siu = wire(build_siu_s12(booking, control_id="SIU0001", now=NOW))

    assert reparse_siu(siu) == {
        "message_type": "SIU^S12^SIU_S12",
        "placer_id": "1",                        # our call
        "filler_id": "1350",                     # the FHIR Appointment
        "start": "20261006100000-0400",          # 14:00Z, in the practice's own time
        "status": "Booked",
        "patient": {"id": "1271", "family": "Lee", "given": "Dana", "dob": None, "sex": None},
        "location": "1026^^^SUNSHINE_DENTAL^^^^^Sunshine Dental",
        "practitioner": "1110^Rivera^Sam^^^^RDH",
    }


def test_siu_s12_has_the_segments_a_scheduler_expects_in_order(booking):
    siu = wire(build_siu_s12(booking, control_id="SIU0001", now=NOW))

    assert siu.count("\r") == 7 and "\n" not in siu
    assert [s[:3] for s in siu.split("\r")] == ["MSH", "SCH", "TQ1", "PID", "RGS", "AIS", "AIL", "AIP"]
    pid = next(s for s in siu.split("\r") if s.startswith("PID"))
    assert pid.split("|")[13] == "^PRN^CP^^1^813^5550142"


def test_siu_s12_is_a_wire_message_another_parser_accepts(booking):
    # Not just hl7apy agreeing with itself: parse with the strict reference
    # and no group hints, the way a receiver that just got bytes would.
    siu = wire(build_siu_s12(booking, control_id="SIU0001", now=NOW))

    msg = parse_message(siu, validation_level=VALIDATION_LEVEL.STRICT)

    assert msg.msh.msh_10.to_er7() == "SIU0001"
    assert msg.validate() is True


def test_siu_s12_carries_a_birth_date_when_fhir_has_one(booking):
    booking = copy.deepcopy(booking)
    booking["Patient"]["birthDate"] = "1990-05-17"

    siu = wire(build_siu_s12(booking, control_id="SIU0002", now=NOW))

    assert reparse_siu(siu)["patient"]["dob"] == date(1990, 5, 17)
