"""app/agent/prompts.py: the versioned system prompt."""

from datetime import date

from app.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT_TEMPLATE, build_system_prompt
from app.agent.tools import TOOLS
from app.models.models import Practice


def test_prompt_version_is_a_non_empty_string():
    assert isinstance(PROMPT_VERSION, str) and PROMPT_VERSION


def test_build_system_prompt_fills_in_practice_and_date():
    practice = Practice(id=7, name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")

    prompt = build_system_prompt(practice, today=date(2026, 10, 1))

    assert "Sunshine Dental" in prompt
    assert "practice_id=7" in prompt
    assert "2026-10-01" in prompt


def test_build_system_prompt_defaults_today_to_the_real_date():
    practice = Practice(id=1, name="Test Dental", timezone="UTC", phone="+18135551234")

    prompt = build_system_prompt(practice)

    assert date.today().isoformat() in prompt


def test_prompt_covers_role_tone_what_not_to_do_and_tools():
    # Not a content-quality check (that needs an eval, not a unit test) — just
    # confirms the four required sections the task named are actually present,
    # so a future edit can't quietly drop one.
    for heading in ("ROLE", "TONE", "WHAT NOT TO DO", "TOOLS"):
        assert heading in SYSTEM_PROMPT_TEMPLATE


def test_prompt_mentions_every_registered_tool_by_name():
    # Ties the prompt to the actual tool registry: if a tool is ever added,
    # renamed, or removed in app/agent/tools.py without touching the prompt,
    # this fails instead of the model silently never hearing about it.
    for schema in TOOLS.standard_tools:
        assert schema.name in SYSTEM_PROMPT_TEMPLATE
