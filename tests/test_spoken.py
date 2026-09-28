"""app/agent/spoken.py: how times and digits are written for TTS."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.agent.spoken import ordinal, spoken_digits, spoken_time

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize("n, expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (10, "10th"),
    (11, "11th"), (12, "12th"), (13, "13th"),  # teens are always "th"
    (21, "21st"), (22, "22nd"), (23, "23rd"), (30, "30th"), (31, "31st"),
])
def test_ordinal(n, expected):
    assert ordinal(n) == expected


def test_the_module_test_phrase():
    # "9:30 AM Tuesday the 15th": 2026-12-15 is a Tuesday, EST is UTC-5.
    assert spoken_time(datetime(2026, 12, 15, 14, 30, tzinfo=timezone.utc), NY) == "9:30 AM Tuesday the 15th"


def test_converts_to_the_practice_timezone_not_utc():
    # Same instant, summer (EDT, UTC-4): 13:30 UTC is 9:30 AM local.
    assert spoken_time(datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc), NY) == "9:30 AM Tuesday the 15th"


def test_whole_hours_drop_the_minutes():
    assert spoken_time(datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc), NY) == "1 PM Thursday the 1st"


@pytest.mark.parametrize("hour_utc, expected_clock", [
    (16, "12 PM"),   # noon local (EDT), not "0 PM"
    (4, "12 AM"),    # midnight local, not "0 AM"
])
def test_noon_and_midnight(hour_utc, expected_clock):
    assert spoken_time(datetime(2026, 10, 2, hour_utc, 0, tzinfo=timezone.utc), NY).startswith(expected_clock + " ")


def test_naive_datetime_is_treated_as_utc():
    assert spoken_time(datetime(2026, 12, 15, 14, 30), NY) == "9:30 AM Tuesday the 15th"


def test_spoken_digits_are_spaced_and_stripped_of_punctuation():
    assert spoken_digits("0142") == "0 1 4 2"
    assert spoken_digits("555-0142") == "5 5 5 0 1 4 2"
