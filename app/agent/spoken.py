"""Formatting for things the agent says out loud — times and digits — so TTS
reads them the way a receptionist would, not the way a database stores them.

This lives in tool *output*, not in the system prompt, on purpose: a prompt
rule like "say times naturally, in the practice's timezone" asks the model to
convert UTC and do calendar math every time, which this project's own live
testing showed it gets wrong (the "next Tuesday" findings in
docs/notes/conversation-state-machine.md). Handing it a ready-made string
means there's nothing left to convert.

    spoken_time(2026-09-15T13:30Z, America/New_York) -> "9:30 AM Tuesday the 15th"
    spoken_digits("0142")                            -> "0 1 4 2"
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__all__ = ["ordinal", "spoken_digits", "spoken_time"]


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 22nd, 23rd ... 31st."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def spoken_time(value: datetime, tz: ZoneInfo) -> str:
    """An appointment time as a caller should hear it: in the practice's own
    timezone, 12-hour clock, weekday and day-of-month — "9:30 AM Tuesday the
    15th". Whole hours drop the ":00" ("1 PM"), which is how people say it
    and avoids TTS reading "one oh-oh".

    A naive `value` is treated as UTC, the only thing this app ever stores
    (see tools._as_utc) — never as local time.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(tz)
    hour = local.hour % 12 or 12
    clock = f"{hour}" if local.minute == 0 else f"{hour}:{local.minute:02d}"
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{clock} {meridiem} {local.strftime('%A')} the {ordinal(local.day)}"


def spoken_digits(digits: str) -> str:
    """Digits spaced out so TTS reads them one by one — "0142" as
    "zero one four two", not "one hundred forty-two"."""
    return " ".join(re.sub(r"\D", "", digits))
