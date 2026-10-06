"""HL7 v2 by hand: parse what a hospital sends, generate what it expects.
An experiment (docs/notes/hl7v2.md), not product code: the agent speaks FHIR,
and an interface engine would do this translation in production.

    python -m experiments.hl7v2.hl7                 # parse both samples, build the SIU from the fixture
    python -m experiments.hl7v2.hl7 --appointment 1350   # ...from the live HAPI booking instead

hl7apy rather than python-hl7: it carries the official message structures for
each v2 version, so "valid" means valid against HL7 2.5.1 (required fields,
segment order, groups, datatypes), not just "split cleanly on |".

ER7, the v2 wire format, in one breath: segments end in \r; fields split on
|, components on ^, repetitions on ~, subcomponents on &, escapes start
with \\; MSH-1 *is* the field separator and MSH-2 lists the other four.
Fields are positional: PID-5 is the name, PID-7 the date of birth, whatever
else is or isn't there.
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message
from hl7apy.parser import parse_message

SAMPLES = Path(__file__).parent / "samples"
VERSION = "2.5.1"
TIMEZONE_EXT = "http://hl7.org/fhir/StructureDefinition/timezone"
CALL_ID_SYSTEM = "http://fde-voice-agent.example/sid/call"


def to_er7_wire(text: str) -> str:
    """Sample files keep one segment per line for humans; the wire wants \\r."""
    return "\r".join(line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line)


def load(name: str) -> str:
    return to_er7_wire((SAMPLES / name).read_text())


def _segments(node, name: str) -> list:
    """Every segment called `name` anywhere in the message, in order,
    whichever group the message structure nests it in."""
    found = []
    for child in node.children:
        if child.name == name:
            found.append(child)
        elif getattr(child, "classname", "") == "Group":
            found += _segments(child, name)
    return found


def _patient(pid) -> dict:
    dob = pid.pid_7.ts_1.to_er7()
    return {
        "id": pid.pid_3.cx_1.to_er7(),
        "family": pid.pid_5.xpn_1.fn_1.to_er7(),
        "given": pid.pid_5.xpn_2.to_er7(),
        "dob": date(int(dob[:4]), int(dob[4:6]), int(dob[6:8])) if dob else None,
        "sex": pid.pid_8.to_er7() or None,
    }


def _parse(text: str):
    """STRICT parsing checks structure and datatypes but not cardinality: a
    message missing PID-5 parses fine. validate() is what checks required
    fields, so a receiver has to do both."""
    msg = parse_message(text, validation_level=VALIDATION_LEVEL.STRICT, find_groups=True)
    msg.validate()
    return msg


def parse_adt(text: str) -> dict:
    """ADT^A04 (register a patient): who they are, from PID; the visit, from PV1."""
    msg = _parse(text)
    [pid] = _segments(msg, "PID")
    [pv1] = _segments(msg, "PV1")
    return {
        "message_type": msg.msh.msh_9.to_er7(),
        "control_id": msg.msh.msh_10.to_er7(),
        "patient": _patient(pid),
        "patient_class": pv1.pv1_2.to_er7(),       # O = outpatient
        "attending": pv1.pv1_7.to_er7(),
    }


def parse_oru(text: str) -> dict:
    """ORU^R01 (results): who, from PID; what was ordered, from OBR; the values, from OBX."""
    msg = _parse(text)
    [pid] = _segments(msg, "PID")
    [obr] = _segments(msg, "OBR")
    observations = [{
        "code": obx.obx_3.ce_1.to_er7(),            # LOINC
        "name": obx.obx_3.ce_2.to_er7(),
        "value": obx.obx_5.to_er7(),
        "units": obx.obx_6.ce_1.to_er7(),
        "reference_range": obx.obx_7.to_er7(),
        "abnormal_flag": obx.obx_8.to_er7(),        # N normal, H high, L low
        "status": obx.obx_11.to_er7(),              # F final
    } for obx in _segments(msg, "OBX")]
    return {
        "message_type": msg.msh.msh_9.to_er7(),
        "patient": _patient(pid),
        "order": obr.obr_4.to_er7(),
        "observations": observations,
    }


# ── FHIR booking -> SIU^S12 ──────────────────────────────────────────────────
def _ts(value: str, tz: ZoneInfo) -> str:
    """FHIR instant -> HL7 TS in the practice's local time with its offset:
    2026-10-06T14:00:00Z -> 20261006100000-0400."""
    return datetime.fromisoformat(value).astimezone(tz).strftime("%Y%m%d%H%M%S%z")


def _xtn(e164: str) -> str:
    """+18135550142 -> ^PRN^CP^^1^813^5550142 (use, equipment, country, area, local)."""
    digits = e164.lstrip("+")
    if len(digits) == 11 and digits.startswith("1"):
        return f"^PRN^CP^^1^{digits[1:4]}^{digits[4:]}"
    return f"^PRN^CP^{e164}"


def _hd(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.upper())


def build_siu_s12(resources: dict, *, control_id: str, now: datetime) -> Message:
    """The SIU^S12 ("new appointment booked") a v2 scheduling system would
    receive for a booking made through FHIR: the FHIR Appointment and the
    resources it references, mapped field by field (docs/notes/hl7v2.md)."""
    appt, patient = resources["Appointment"], resources["Patient"]
    practitioner, location, org = resources["Practitioner"], resources["Location"], resources["Organization"]
    tz = ZoneInfo(next((e["valueCode"] for e in location.get("extension", []) if e["url"] == TIMEZONE_EXT), "UTC"))
    facility = _hd(org["name"])
    start, end = _ts(appt["start"], tz), _ts(appt["end"], tz)
    minutes = str(int((datetime.fromisoformat(appt["end"])
                       - datetime.fromisoformat(appt["start"])).total_seconds() // 60))
    service = appt["serviceType"][0]
    service_code = next((c["code"] for c in service.get("coding", [])), "")
    call_id = next((i["value"] for i in appt.get("identifier", []) if i["system"] == CALL_ID_SYSTEM), "")
    name = patient["name"][0]
    phone = next((t["value"] for t in patient.get("telecom", []) if t["system"] == "phone"), "")
    p_name = practitioner["name"][0]
    degree = next((q["code"]["text"] for q in practitioner.get("qualification", []) if q.get("code", {}).get("text")), "")

    msg = Message("SIU_S12", version=VERSION, validation_level=VALIDATION_LEVEL.STRICT)
    msg.msh.msh_3 = "FDE_VOICE_AGENT"
    msg.msh.msh_4 = facility
    msg.msh.msh_5 = "PMS"
    msg.msh.msh_6 = facility
    msg.msh.msh_7 = now.astimezone(tz).strftime("%Y%m%d%H%M%S%z")
    msg.msh.msh_9 = "SIU^S12^SIU_S12"
    msg.msh.msh_10 = control_id
    msg.msh.msh_11 = "T"  # T = test traffic; P in production
    msg.msh.msh_12 = VERSION

    sch = msg.sch
    sch.sch_1 = f"{call_id}^FDE_VOICE_AGENT"          # placer: who asked (our call)
    sch.sch_2 = f"{appt['id']}^FHIR"                  # filler: who holds it (the FHIR Appointment)
    sch.sch_6 = "NEW^New appointment^LOCAL"            # the one required SCH field
    sch.sch_7 = f"{service_code}^{service.get('text', service_code)}^LOCAL"
    sch.sch_8 = "Normal^Routine schedule request^HL70277"
    sch.sch_9 = minutes
    sch.sch_10 = "min^minutes^UCUM"
    sch.sch_11 = f"^^^{start}^{end}"                  # TQ: start, end (deprecated since 2.5, still widely read)
    sch.sch_16 = "FRONTDESK^Front Desk"               # required: who to call about it (the practice)
    org_phone = next((t["value"] for t in org.get("telecom", []) if t["system"] == "phone"), "")
    if org_phone:
        sch.sch_18 = _xtn(org_phone).replace("^PRN^CP", "^WPN^PH")
    sch.sch_20 = "FDE_VOICE_AGENT^Voice Agent"        # required: who entered it (us)
    sch.sch_25 = "Booked^Booked^HL70278"

    tq1 = msg.add_segment("TQ1")                      # the 2.5+ home of timing
    tq1.tq1_1 = "1"
    tq1.tq1_7 = start
    tq1.tq1_8 = end

    pid = msg.add_group("SIU_S12_PATIENT").pid
    pid.pid_1 = "1"
    pid.pid_3 = f"{patient['id']}^^^FHIR^PI"
    pid.pid_5 = f"{name.get('family', '')}^{' '.join(name.get('given', []))}"
    if patient.get("birthDate"):
        pid.pid_7 = patient["birthDate"].replace("-", "")
    if phone:
        pid.pid_13 = _xtn(phone)

    resources_group = msg.add_group("SIU_S12_RESOURCES")
    resources_group.rgs.rgs_1 = "1"
    resources_group.rgs.rgs_2 = "A"                   # A = add

    ais = resources_group.add_group("SIU_S12_SERVICE").ais
    ais.ais_1, ais.ais_2 = "1", "A"
    ais.ais_3 = f"{service_code}^{service.get('text', service_code)}^LOCAL"
    ais.ais_4, ais.ais_7, ais.ais_8, ais.ais_10 = start, minutes, "min^minutes^UCUM", "Booked"

    ail = resources_group.add_group("SIU_S12_LOCATION_RESOURCE").ail
    ail.ail_1, ail.ail_2 = "1", "A"
    ail.ail_3 = f"{location['id']}^^^{facility}^^^^^{location['name']}"   # PL: point of care .. facility .. description
    ail.ail_4 = "O^Provider's office^HL70305"
    ail.ail_6, ail.ail_9, ail.ail_10, ail.ail_12 = start, minutes, "min^minutes^UCUM", "Booked"

    aip = resources_group.add_group("SIU_S12_PERSONNEL_RESOURCE").aip
    aip.aip_1, aip.aip_2 = "1", "A"
    aip.aip_3 = (f"{practitioner['id']}^{p_name.get('family', '')}^{' '.join(p_name.get('given', []))}"
                 f"^^^{' '.join(p_name.get('prefix', []))}^{degree}")
    aip.aip_4 = f"{'DENT^Dentist' if degree == 'DDS' else 'HYG^Dental hygienist'}^LOCAL"
    aip.aip_6, aip.aip_9, aip.aip_10, aip.aip_12 = start, minutes, "min^minutes^UCUM", "Booked"

    msg.validate()  # against the HL7 2.5.1 SIU_S12 structure: raises if anything's off
    return msg


def wire(msg: Message) -> str:
    return msg.to_er7()  # segments joined by \r


def reparse_siu(text: str) -> dict:
    """Validate by re-parsing: what a receiving system would read back."""
    msg = _parse(text)
    [sch] = _segments(msg, "SCH")
    [pid] = _segments(msg, "PID")
    [ail] = _segments(msg, "AIL")
    [aip] = _segments(msg, "AIP")
    return {
        "message_type": msg.msh.msh_9.to_er7(),
        "placer_id": sch.sch_1.ei_1.to_er7(),
        "filler_id": sch.sch_2.ei_1.to_er7(),
        "start": sch.sch_11.tq_4.to_er7(),
        "status": sch.sch_25.ce_1.to_er7(),
        "patient": _patient(pid),
        "location": ail.ail_3.to_er7(),
        "practitioner": aip.aip_3.to_er7(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m experiments.hl7v2.hl7")
    parser.add_argument("--appointment", help="build the SIU from this FHIR Appointment on the live HAPI")
    args = parser.parse_args()

    print("ADT^A04 ->", json.dumps(parse_adt(load("adt_a04.hl7")), default=str, indent=1))
    oru = parse_oru(load("oru_r01.hl7"))
    print("ORU^R01 ->", json.dumps({**oru, "observations": oru["observations"][:1]}, default=str, indent=1))

    if args.appointment:
        from app.fhir.client import FhirClient
        with FhirClient() as fhir:
            appt = fhir.read("Appointment", args.appointment)
            resources = {"Appointment": appt}
            for p in appt["participant"]:
                kind, rid = p["actor"]["reference"].split("/")
                resources[kind] = fhir.read(kind, rid)
            resources["Organization"] = fhir.read("Organization", resources["Location"]["managingOrganization"]["reference"].split("/")[1])
    else:
        resources = json.loads((SAMPLES / "fhir_booking_1350.json").read_text())
    siu = wire(build_siu_s12(resources, control_id="SIU0001", now=datetime.now().astimezone()))
    print("\nSIU^S12 (one segment per line):\n" + siu.replace("\r", "\n"))
    print("\nre-parsed ->", json.dumps(reparse_siu(siu), default=str, indent=1))


if __name__ == "__main__":
    main()
