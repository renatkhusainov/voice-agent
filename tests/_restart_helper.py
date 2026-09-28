"""Invoked as a subprocess by tests/test_restart.py — writes or reads a
DialogState for a fixed session id, through the REAL store.py client (not
fakeredis). Not a test module itself: the leading underscore keeps pytest
from collecting it, and each invocation of this file is a genuinely separate
OS process from whichever one launched it.
"""

import sys
from datetime import datetime, timezone

from app.agent import store
from app.agent.state import BookingSlots, DialogIntent, DialogState

SESSION_ID = "pytest-restart-verification"


def _expected_state() -> DialogState:
    return DialogState(
        intent=DialogIntent.book_appointment,
        slots=BookingSlots(
            practice_id=1, caller_name="Dana Lee", callback_number="+18135550142",
            service="cleaning", requested_slot=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
        ),
        pending_confirmation=True, proposed_at_turn=1, turn_count=1,
    )


def main() -> None:
    mode = sys.argv[1]

    if mode == "write":
        store.set_state(SESSION_ID, _expected_state())
        print("wrote")
        # No clean shutdown hook, no atexit — the process just ends here,
        # the same way a `kill` would end it mid-conversation.

    elif mode == "read":
        loaded = store.get_state(SESSION_ID)
        if loaded is None:
            print("MISSING")
            sys.exit(1)
        if loaded == _expected_state():
            print("MATCH")
        else:
            print(f"MISMATCH: {loaded!r} != {_expected_state()!r}")
            sys.exit(1)

    elif mode == "cleanup":
        store.delete_state(SESSION_ID)
        print("cleaned")

    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
