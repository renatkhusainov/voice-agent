"""app/agent/chat.py: only get_or_create_demo_practice is meaningfully
unit-testable — main() is an interactive REPL around app/agent/session.py's
take_turn, which is already covered by tests/test_agent_session.py.
"""

from app.agent.chat import DEMO_PRACTICE_PHONE, get_or_create_demo_practice
from app.models.models import Practice


def test_creates_a_demo_practice_on_first_use(db_session):
    practice = get_or_create_demo_practice(db_session)

    assert practice.id is not None
    assert practice.phone == DEMO_PRACTICE_PHONE
    assert practice.business_hours is not None


def test_reuses_the_same_demo_practice_on_later_calls(db_session):
    first = get_or_create_demo_practice(db_session)
    second = get_or_create_demo_practice(db_session)

    assert first.id == second.id
    assert db_session.query(Practice).filter_by(phone=DEMO_PRACTICE_PHONE).count() == 1
