"""Mask PHI-shaped substrings in free text.

This is the boundary every transcript turn crosses before it is written to the
database, and the same function backs the log scrubber (app/phi/log_scrub.py).
It is deliberately regex-only: fast, deterministic, no model in the loop.

Masked today:
  * phone numbers   -> [PHONE]   (10/11-digit, formatted or bare, and 7-digit
                                  local numbers when hyphen/dot separated)
  * SSN shapes      -> [SSN]     (3-2-4 with dashes or spaces, or 9 bare digits)
  * dates with year -> [DOB]     (3/14/1987, 1987-03-14, March 14th, 1987, ...)
  * spoken digits   -> [NUMBER]  (7+ digit words/digits in a row: "eight one
                                  three five five five zero one four two")

Decisions worth knowing (also written up in docs/phi-and-secrets.md):
  * Spoken digits: we DO care. The assistant's replies and un-normalised STT
    output routinely spell numbers out, so a digits-only matcher would leak
    exactly the numbers people read aloud. A run of 7+ is the threshold: shorter
    runs ("nine one one", "one two three") are ordinary speech, and a 7-digit
    local number is the shortest thing worth calling a phone number.
  * Dates: "DOB-shaped" means a full date carrying a year. A bare "September
    24th" or "9/24" is an appointment slot the eval suite needs, so it is left
    alone. The cost: an appointment given WITH a year ("September 24, 2026") is
    masked too. That over-masking is intentional; a DOB and a booking date have
    the same shape, and leaking a DOB is the worse failure.
  * Not covered (yet): insurance IDs, card numbers (Deepgram's `pci` redaction
    handles those at the STT layer), names, addresses, and dates spelled out in
    words ("March fourteenth, nineteen eighty-seven").

Every mask is digit-free, so redact() is idempotent, and text with nothing to
mask comes back byte-identical (there is a test for exactly that; it is the one
that catches an over-greedy pattern).
"""

import re

__all__ = ["redact"]

PHONE = "[PHONE]"
SSN = "[SSN]"
DOB = "[DOB]"
NUMBER = "[NUMBER]"

# ── SSN ────────────────────────────────────────────────────────────────────────
# Applied before phone so a 9-digit run is never half-eaten by a phone pattern.
_SSN = re.compile(r"(?<!\d)(?:\d{3}[- ]\d{2}[- ]\d{4}|\d{9})(?!\d)")

# ── Phone ──────────────────────────────────────────────────────────────────────
# (?<!\d) / (?!\d) keep us from carving a "phone number" out of the middle of a
# longer digit run (a 16-digit card number, an order id): either the whole run is
# phone-shaped or we leave it alone.
_SEP = r"[\s.\-]?"
_PHONE = re.compile(
    r"(?<!\d)"
    r"(?:\+?1" + _SEP + r")?"
    r"(?:\(\d{3}\)" + _SEP + r"|\d{3}" + _SEP + r")"
    r"\d{3}" + _SEP + r"\d{4}"
    r"(?!\d)"
)
# 555-0142 / 555.0142. The separator is required: seven bare digits are too
# ambiguous (order numbers, zip+4 halves) to mask on shape alone.
_PHONE_LOCAL = re.compile(r"(?<![\d.\-])\d{3}[.\-]\d{4}(?![\d.\-])")

# ── DOB-shaped dates ───────────────────────────────────────────────────────────
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_YEAR = r"(?:19|20)\d{2}"
_DATES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        # 3/14/1987, 3-14-1987, 3.14.1987 (the same separator twice)
        r"(?<![\d/.\-])\d{1,2}([/.\-])\d{1,2}\1" + _YEAR + r"(?!\d)",
        # 3/14/87. A two-digit year only with slashes: with dots or dashes it
        # collides with version numbers and ranges.
        r"(?<![\d/.\-])\d{1,2}/\d{1,2}/\d{2}(?![\d/])",
        # 1987-03-14, 1987/03/14
        r"(?<![\d/.\-])" + _YEAR + r"([/\-])\d{1,2}\1\d{1,2}(?![\d/\-])",
        # March 14, 1987 / Mar. 14th 1987
        r"\b" + _MONTH + r"\.?\s+" + _DAY + r",?\s+" + _YEAR + r"\b",
        # 14 March 1987 / 14th of March, 1987
        r"\b" + _DAY + r"\s+(?:of\s+)?" + _MONTH + r",?\s+" + _YEAR + r"\b",
    )
]

# ── Spoken / spelled-out digit runs ────────────────────────────────────────────
_DIGIT = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|\d)"
_SPOKEN_RUN = re.compile(
    r"(?<!\w)" + _DIGIT + r"(?:[\s,.\-]+" + _DIGIT + r"){6,}(?!\w)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Return `text` with phone numbers, SSN shapes and DOB-shaped dates masked."""
    text = _SSN.sub(SSN, text)
    text = _PHONE.sub(PHONE, text)
    text = _PHONE_LOCAL.sub(PHONE, text)
    for pattern in _DATES:
        text = pattern.sub(DOB, text)
    return _SPOKEN_RUN.sub(NUMBER, text)
