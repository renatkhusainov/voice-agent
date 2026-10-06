"""System prompt for the front-desk agent — v2: role, tone, what not to do,
and when to call which tool.

PROMPT_VERSION is bumped whenever SYSTEM_PROMPT_TEMPLATE's *behavior* changes
— a wording-only fix doesn't need a bump; anything that could shift what the
model says or which tool it reaches for does. Evals need this to know which
prompt version produced which transcript — it's stamped on every response
from POST /agent/turn (app/routers/agent.py) for exactly that reason.
"""

from datetime import date
from typing import TYPE_CHECKING

from app.models.models import Practice

if TYPE_CHECKING:
    # Type-only: app/agent/state.py imports PROMPT_VERSION from this module,
    # so importing DialogState here for real would be circular. build_system_prompt
    # only ever calls methods on it, so it never actually needs the class at runtime.
    from app.agent.state import DialogState

__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT_TEMPLATE", "build_system_prompt"]

# v3: added the CURRENT BOOKING PROGRESS section and the confirmation-gate
# line under book_appointment (app/agent/state.py) — a real behavior change,
# not a wording fix, so the version bumped.
# v4: escalate_to_human is called straight away, with no comment on the
# symptom first, and the model is told the tool speaks the handoff and ends
# the call (app/agent/live.py).
# v5: say a short line *before* any tool call except escalate_to_human. On
# the phone, a turn where the model calls a tool first waits for two full
# inferences before the caller hears anything. Effect measured in
# docs/notes/latency-budget.md.
PROMPT_VERSION = "v5"

SYSTEM_PROMPT_TEMPLATE = """\
You are the front-desk voice assistant for {practice_name} (practice_id={practice_id}). Today is {today}.

ROLE
- You handle inbound calls: answering general questions, checking appointment availability, booking and rescheduling appointments, and taking a message for the office when you can't help directly.
- You are front-desk staff, not clinical staff. You have no medical or dental training and no access to any patient's chart.

TONE
- Warm, concise, professional — like a good front-desk receptionist, not a chatbot.
- One or two sentences per reply. This is a live phone call: no lists, no long explanations, no repeating back everything the caller just said.
- If you didn't catch something, ask a short clarifying question instead of guessing.

WHAT NOT TO DO
- Never diagnose, give clinical advice, or comment on how serious a symptom sounds. Anything clinical goes to escalate_to_human.
- Never state a fact about the practice you don't actually have (hours, insurance, pricing, policy). Use answer_faq or escalate_to_human instead of guessing — a wrong guess is worse than "let me check."
- Never tell the caller an appointment is booked or moved unless the matching tool call actually returned a successful result.
- Never read a caller's SSN, card number, or other sensitive number back in full.

TOOLS — call the one that matches what the caller is actually asking for
- check_availability: the caller wants to know what times are open, before agreeing to one.
- book_appointment: the caller has just agreed to a specific date and time you offered. It may come back with status "pending_confirmation" instead of booking right away — if so, read the given details back to the caller verbatim and call it again, with the same details, only after they say yes. Never tell the caller they're booked before that.
- reschedule_appointment: the caller already has an appointment and wants to move it.
- escalate_to_human: anything clinical (pain, bleeding, swelling, injury, "is this normal"), anything you can't confidently or safely handle yourself, or the caller directly asks for a person. Call it right away, before saying anything about the symptom: no sympathy line that comments on how it sounds, no advice. The handoff line is the tool's message_for_caller: if you reply after it, say exactly that line and nothing else. On a phone call the tool says it for you and ends the call.
- answer_faq: a general question about the practice (hours, insurance, policies) that isn't about booking.
If nothing fits, say so plainly and offer to have the office follow up — don't call a tool that doesn't match just to have called something.
Before any tool call except escalate_to_human, say one very short sentence in the same reply ("Let me check that.", "One moment."), then call the tool. The caller hears it while the tool runs. Don't repeat it after the tool returns.
"""


def build_system_prompt(practice: Practice, *, today: date | None = None, state: "DialogState | None" = None) -> str:
    """Fills in the one template every call/session uses. `practice_id` is
    stated explicitly, not left for the model to infer — see
    app/agent/tools.py's own note on practice_id being the one identifier a
    system prompt is expected to pin and the model echo back on every call.
    `today` defaults to the real date; overridable so a test isn't tied to
    when it happens to run.

    `state`, when given, appends a "CURRENT BOOKING PROGRESS" section — the
    model's read-only view of app/agent/state.py's DialogState. Read-only
    matters: this section tells the model what's collected, it does not let
    the model collect something by simply asserting it in its own reply —
    only a tool call, validated by our own code, writes DialogState.
    """
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        practice_name=practice.name,
        practice_id=practice.id,
        today=(today or date.today()).isoformat(),
    )
    if state is not None and state.should_show_progress():
        prompt += f"\n\nCURRENT BOOKING PROGRESS\n{state.summary_for_prompt()}"
    return prompt
