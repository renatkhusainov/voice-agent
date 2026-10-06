"""scripts/seed_fhir.py: the transaction Bundle it builds. Pure, no server:
the seed itself was run twice against a fresh HAPI (docs/fhir-mapping.md)."""

from datetime import date

from scripts.seed_fhir import SID, PracticeSeed, build_seed_bundle

PRACTICE = PracticeSeed(id=4, name="Sunshine Dental", phone="+15550009999", timezone="America/New_York",
                        business_hours={"mon": ["09:00", "17:00"], "tue": ["09:00", "17:00"], "wed": ["09:00", "17:00"],
                                        "thu": ["09:00", "17:00"], "fri": ["09:00", "17:00"], "sat": None, "sun": None})


def resources(bundle, kind):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == kind]


def test_a_week_is_16_half_hour_slots_per_weekday_per_practitioner():
    bundle = build_seed_bundle(PRACTICE, date(2026, 10, 5))  # a Monday

    assert len(resources(bundle, "Slot")) == 16 * 5 * 2
    assert {len(resources(bundle, k)) for k in ("Organization", "Location")} == {1}
    assert len(resources(bundle, "Practitioner")) == len(resources(bundle, "Schedule")) == 2


def test_slot_times_are_local_business_hours_in_utc_across_dst():
    october = resources(build_seed_bundle(PRACTICE, date(2026, 10, 5), days=1), "Slot")
    november = resources(build_seed_bundle(PRACTICE, date(2026, 11, 2), days=1), "Slot")  # after DST ends

    assert october[0]["start"] == "2026-10-05T13:00:00Z"   # 9:00 EDT
    assert november[0]["start"] == "2026-11-02T14:00:00Z"  # 9:00 EST
    assert october[15]["end"] == "2026-10-05T21:00:00Z"    # last slot ends 17:00 EDT
    assert all(s["status"] == "free" for s in october)


def test_closed_days_get_no_slots_and_custom_hours_are_honoured():
    short_week = PracticeSeed(4, "Sunshine Dental", "+15550009999", "America/New_York",
                              {"mon": ["10:00", "12:00"], "sat": ["09:00", "10:00"]})

    slots = resources(build_seed_bundle(short_week, date(2026, 10, 5)), "Slot")

    assert len(slots) == (4 + 2) * 2  # Mon 10-12 (4 slots) + Sat 9-10 (2), per practitioner


def test_every_entry_is_a_conditional_create_on_its_own_identifier():
    bundle = build_seed_bundle(PRACTICE, date(2026, 10, 5))

    for entry in bundle["entry"]:
        [ident] = entry["resource"]["identifier"]
        assert ident["system"].startswith(SID)
        assert entry["request"]["ifNoneExist"] == f"identifier={ident['system']}|{ident['value']}"
    identifiers = [(e["resource"]["identifier"][0]["system"], e["resource"]["identifier"][0]["value"])
                   for e in bundle["entry"]]
    assert len(identifiers) == len(set(identifiers))


def test_two_builds_are_identical_so_a_rerun_matches_the_first():
    assert build_seed_bundle(PRACTICE, date(2026, 10, 5)) == build_seed_bundle(PRACTICE, date(2026, 10, 5))


def test_every_reference_points_inside_the_bundle():
    bundle = build_seed_bundle(PRACTICE, date(2026, 10, 5))
    full_urls = {e["fullUrl"] for e in bundle["entry"]}

    def refs(value):
        if isinstance(value, dict):
            if "reference" in value:
                yield value["reference"]
            for v in value.values():
                yield from refs(v)
        elif isinstance(value, list):
            for v in value:
                yield from refs(v)

    all_refs = [r for e in bundle["entry"] for r in refs(e["resource"])]
    assert all_refs and set(all_refs) <= full_urls


def test_location_carries_hours_and_timezone_from_the_practice():
    [location] = resources(build_seed_bundle(PRACTICE, date(2026, 10, 5)), "Location")

    assert location["hoursOfOperation"] == [{"daysOfWeek": ["mon", "tue", "wed", "thu", "fri"],
                                             "openingTime": "09:00:00", "closingTime": "17:00:00"}]
    assert location["extension"][0]["valueCode"] == "America/New_York"
