import pytest

from app.phi import redact


# ── phone numbers ──────────────────────────────────────────────────────────────
def test_bare_ten_digit_number():
    assert redact("8135550142") == "[PHONE]"


def test_formatted_phone_number():
    assert redact("(813) 555-0142") == "[PHONE]"


@pytest.mark.parametrize("raw", [
    "813-555-0142", "813.555.0142", "813 555 0142", "(813)555-0142",
    "+1 813 555 0142", "+18135550142", "1-813-555-0142", "18135550142",
])
def test_other_phone_formats(raw):
    assert redact(raw) == "[PHONE]"


def test_seven_digit_local_number_needs_a_separator():
    assert redact("my number is 555-0142") == "my number is [PHONE]"
    # Seven bare digits are too ambiguous (order numbers etc.) to mask on shape.
    assert redact("order 5550142") == "order 5550142"


def test_two_numbers_are_masked_separately():
    assert redact("call 8135550142 or (813) 555-0199 please") == "call [PHONE] or [PHONE] please"


# ── spoken digits ──────────────────────────────────────────────────────────────
# Decision: we care. The assistant's replies and un-normalised STT spell digits
# out, so a digits-only matcher would leak exactly the numbers people read aloud.
@pytest.mark.parametrize("raw, expected", [
    ("eight one three five five five zero one four two", "[NUMBER]"),
    ("my number is eight one three five five five zero one four two.", "my number is [NUMBER]."),
    # Deepgram-punctuated, one digit at a time.
    ("eight, one, three, five, five, five, zero, one, four, two.", "[NUMBER]."),
    # Callers pause between groups, so commas land mid-number. Requiring a
    # consistent separator would leak this, the most realistic case.
    ("eight one three, five five five, zero one four two", "[NUMBER]"),
    ("Eight One Three Five Five Five Oh One Four Two", "[NUMBER]"),
    ("8 1 3 5 5 5 0 1 4 2", "[NUMBER]"),
])
def test_spoken_digits_are_masked(raw, expected):
    assert redact(raw) == expected


@pytest.mark.parametrize("clean", [
    "nine one one",
    "one two three",
    "I have one kid, two dogs and three cats",
    "it takes five or six days",
])
def test_short_spoken_digit_runs_are_just_speech(clean):
    assert redact(clean) == clean


# ── SSN ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["123-45-6789", "123 45 6789", "123456789"])
def test_ssn_shapes(raw):
    assert redact(f"my social is {raw}") == "my social is [SSN]"


# ── DOB-shaped dates ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "03/14/1987", "3-14-1987", "3.14.1987", "3/14/87", "1987-03-14", "1987/03/14",
    "March 14, 1987", "March 14th, 1987", "mar 14 1987", "Sept. 3rd 2015",
    "14 March 1987", "14th of March, 1987",
])
def test_dob_shaped_dates(raw):
    assert redact(f"born {raw}") == "born [DOB]"


def test_appointment_dates_without_a_year_are_left_alone():
    text = "Can we do September 24th at 3 pm, or 9/24 in the morning?"
    assert redact(text) == text


# ── the one people skip: clean text must come back byte-identical ──────────────
# A greedy regex shows up here first: times, prices, zip codes, ranges, versions,
# bare years, long ids and the tags Deepgram itself inserts must all survive.
CLEAN_TEXTS = [
    "",
    "Hi, this is the front desk at iHeartSmiles. How can I help you today?",
    "I have 3 kids and we're free at 10:30 on Tuesday or 4 pm Thursday.",
    "It costs $1,200.50 and takes 5-10 minutes; room 1234.",
    "Our office is at 1234 Main Street, Tampa, FL 33647-1234.",
    "He is 12, was born in 2014, and weighs 55 pounds.",
    "3-4 weeks, 10-15 minutes, 2 to 3 visits.",
    "version 3.11.2 released 1.5.20",
    "Call 911 if it's an emergency.",
    "order 12345678 and card 4111111111111111",  # 8 and 16 digits: not phone-shaped
    "Deepgram tags: [SSN_1] [PHONE_NUMBER_1] [DOB_1] [CREDIT_CARD_1]",
    "tabs\tand\nnewlines  and  double  spaces — emoji 🦷 and accents café",
    "   leading and trailing whitespace   ",
]


@pytest.mark.parametrize("text", CLEAN_TEXTS)
def test_clean_text_comes_back_byte_identical(text):
    assert redact(text).encode("utf-8") == text.encode("utf-8")


# ── in context, and idempotence ────────────────────────────────────────────────
def test_phi_inside_a_sentence_keeps_the_surrounding_text():
    text = "Hi, it's Dana. My son was born 03/14/2016, call me at (813) 555-0142 after 3 pm."
    assert redact(text) == "Hi, it's Dana. My son was born [DOB], call me at [PHONE] after 3 pm."


def test_long_digit_runs_are_not_partially_masked():
    # Carving a "phone number" out of the middle of a longer run would leave
    # half the digits behind and still look like redaction worked.
    for run in ("4111111111111111", "12345678901234"):
        assert redact(run) == run


@pytest.mark.parametrize("text", [
    "call (813) 555-0142", "ssn 123-45-6789", "born 03/14/1987",
    "eight one three five five five zero one four two", "nothing to see here",
])
def test_redact_is_idempotent(text):
    once = redact(text)
    assert redact(once) == once
