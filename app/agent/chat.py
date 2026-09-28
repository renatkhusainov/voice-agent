"""python -m app.agent.chat — an interactive terminal REPL against the real
agent loop, for manual testing without a phone call, Twilio, or audio.

Needs a real ANTHROPIC_API_KEY (loaded the same way app/services/bot.py loads
it — via .env / the environment) and a real database, since it runs through
the same run_agent_loop and TOOLS the real call path does — this is the same
harness POST /agent/turn (app/routers/agent.py) exposes over HTTP, driven
from a terminal instead. See app/agent/session.py for what get_or_create_session
and take_turn actually do; this file is just the REPL around them.

    python -m app.agent.chat                  # demo practice, auto-created if needed
    python -m app.agent.chat --practice-id 3   # a specific existing practice
"""

import argparse
import sys

from anthropic import Anthropic
from sqlalchemy.orm import Session

from app.agent.prompts import PROMPT_VERSION
from app.agent.session import get_or_create_session, take_turn
from app.config import settings
from app.db import SessionLocal
from app.models.models import Practice
from app.services.calls import start_call

DEMO_PRACTICE_PHONE = "+10000000000"
DEMO_BUSINESS_HOURS = {day: ["09:00", "17:00"] for day in ("mon", "tue", "wed", "thu", "fri")}


def get_or_create_demo_practice(db: Session) -> Practice:
    """Used when --practice-id isn't given: a stable, reusable practice for
    quick manual testing, keyed by its fixed phone number so repeat runs
    reuse the same one instead of accumulating duplicates."""
    practice = db.query(Practice).filter_by(phone=DEMO_PRACTICE_PHONE).first()
    if practice is None:
        practice = Practice(
            name="Demo Dental", timezone="America/New_York", phone=DEMO_PRACTICE_PHONE,
            business_hours=DEMO_BUSINESS_HOURS,
        )
        db.add(practice)
        db.commit()
    return practice


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--practice-id", type=int, default=None, help="An existing practice id; omit for a demo practice")
    args = parser.parse_args(argv)

    db = SessionLocal()
    client = Anthropic(api_key=settings.anthropic_api_key)

    if args.practice_id is not None:
        practice = db.get(Practice, args.practice_id)
        if practice is None:
            print(f"No practice with id {args.practice_id}", file=sys.stderr)
            raise SystemExit(1)
    else:
        practice = get_or_create_demo_practice(db)

    # Deterministic per practice, not arbitrary: app/agent/state.py's
    # DialogState is now Redis-backed and keyed by this same session_id, so a
    # second `--practice-id N` run picks the same key back up — collected
    # slots survive a restart of this CLI itself, the same property
    # tests/test_store.py proves for the harness in general.
    session = get_or_create_session(
        f"cli-{practice.id}", create_call_id=lambda: start_call(db, practice.id, "cli-harness").id,
    )
    print(f"Chatting with {practice.name} (prompt {PROMPT_VERSION}, call_id={session.call_id}). Ctrl-D to quit.")

    while True:
        try:
            message = input("you> ").strip()
        except EOFError:
            print()
            break
        if not message:
            continue
        reply = take_turn(client, db, session, practice=practice, message=message)
        print(f"bot> {reply}")


if __name__ == "__main__":
    main()
