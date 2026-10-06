"""The front desk's tools over the Model Context Protocol, so any MCP client
(Claude Desktop, Claude Code, another agent) can check availability and book
into a practice. See docs/mcp.md.

No second implementation of anything. Each tool is a thin adapter over the
same app/agent/tools.py functions the voice agent calls, run through the
same loop.execute_tool (validation, ToolError handling, redacted logging).
Tool argument descriptions are the tools.py Pydantic fields themselves. What
this layer adds is only what MCP is different about:

  * Which practice. On the voice path the dialed number decides it. Here the
    caller's credential does: an API key maps to exactly one practice on HTTP,
    and MCP_PRACTICE_ID sets it for a local stdio process. The model is never
    asked for a practice_id and can't pass one. That's the same isolation
    rule as /calls: another practice's appointments, hours and data are "not
    found".
  * A Call row per booking/escalation, caller_number "mcp:<client>": every
    Lead and Appointment in this app hangs off a Call, and the channel
    stays visible in the data.
  * Confirmation. The voice gate (app/agent/state.py) exists because nobody
    can check the model before it books. An MCP client shows the user the
    tool call before running it, and on top of that, when the client
    supports elicitation, the server itself reads the booking back and asks
    the human to confirm. That's the MCP-native form of the voice read-back.

Run it:
    python -m app.mcp_server                      # stdio (MCP_PRACTICE_ID=4)
    python -m app.mcp_server new-key --practice-id 4
    # streamable HTTP: mounted by app/main.py at /mcp (Authorization: Bearer <key>)
"""

import argparse
import asyncio
import hashlib
import json
import secrets
from functools import partial
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from app.agent import tools as agent_tools
from app.agent.loop import execute_tool
from app.agent.prompts import build_system_prompt
from app.agent.state import BookingSlots, DialogState
from app.agent.tools import (
    AnswerFaqInput,
    BookAppointmentInput,
    CheckAvailabilityInput,
    DateRange,
    EscalateToHumanInput,
    RescheduleAppointmentInput,
    ToolHandler,
)
from app.config import settings
from app.db import SessionLocal
from app.models.models import CallStatus, Practice
from app.services.calls import end_call, start_call

__all__ = ["ApiKeyVerifier", "build_http_app", "hash_api_key", "mcp"]

INSTRUCTIONS = (
    "Front-desk tools for one dental practice: check open appointment times, book, "
    "reschedule, answer general questions, or hand off to staff. The practice is fixed "
    "by your credentials; you never pass a practice id. Times go in as the practice's "
    "local time with no UTC offset (e.g. 2026-12-15T09:30:00) or as the exact `start` "
    "from check_availability; responses include a `spoken` / `spoken_time` form to show "
    "the user. Never give clinical advice: use escalate_to_human for anything clinical."
)


# ── Auth: API key -> practice ────────────────────────────────────────────────
def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeyVerifier:
    """The MCP SDK's TokenVerifier hook, fed a static API key instead of an
    OAuth token: `Authorization: Bearer <key>`. Swapping this class for a
    JWT verifier (plus AuthSettings pointing at a real authorization server)
    is the whole change to OAuth; see docs/mcp.md."""

    def __init__(self, key_hashes: dict[str, int]):
        self._key_hashes = key_hashes

    async def verify_token(self, token: str) -> AccessToken | None:
        practice_id = self._key_hashes.get(hash_api_key(token))
        if practice_id is None:
            return None
        return AccessToken(
            token=token, client_id=f"practice-{practice_id}", scopes=["front-desk"],
            claims={"practice_id": practice_id},
        )


def _scoped_practice_id() -> int:
    """The one practice this request may act for: from the verified API key
    on HTTP, from MCP_PRACTICE_ID on stdio. Never from tool arguments."""
    token = get_access_token()
    if token is not None:
        return int(token.claims["practice_id"])
    if settings.mcp_practice_id is not None:
        return settings.mcp_practice_id
    raise ToolError("This MCP server isn't configured for a practice (set MCP_PRACTICE_ID).")


def _channel() -> str:
    token = get_access_token()
    return f"mcp:{token.client_id}" if token is not None else "mcp:stdio"


mcp = MCPServer(
    name="front-desk",
    title="Dental front desk",
    instructions=INSTRUCTIONS,
    token_verifier=ApiKeyVerifier(settings.mcp_api_keys),
    # No OAuth authorization server here: issuer_url is required by the SDK's
    # AuthSettings but only used to advertise one, which this server doesn't.
    auth=AuthSettings(issuer_url="http://localhost:8000", resource_server_url=None),
)


# ── Running a tools.py tool ──────────────────────────────────────────────────
def _dispatch(practice_id: int, call_id: int | None) -> dict[str, ToolHandler]:
    """tools.build_dispatch, with reschedule scoped to the key's practice
    rather than to a call's."""
    dispatch = agent_tools.build_dispatch(call_id or 0)
    dispatch["reschedule_appointment"] = ToolHandler(
        RescheduleAppointmentInput, partial(agent_tools.reschedule_appointment, practice_id=practice_id),
    )
    return dispatch


def _run_tool_blocking(name: str, arguments: dict[str, Any], practice_id: int, call_id: int | None) -> dict[str, Any]:
    """In a worker thread (sync DB work, as on the voice path)."""
    with SessionLocal() as db:
        outcome = execute_tool(db, name, arguments, _dispatch(practice_id, call_id), call_id)
    if outcome.is_error:
        raise ToolError(outcome.content)
    return json.loads(outcome.content)


def _open_call(practice_id: int, channel: str) -> int:
    with SessionLocal() as db:
        return start_call(db, practice_id, channel).id


def _close_call(call_id: int, failed: bool) -> None:
    with SessionLocal() as db:
        end_call(db, call_id, CallStatus.failed if failed else CallStatus.completed)


async def _run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_run_tool_blocking, name, arguments, _scoped_practice_id(), None)


async def _run_tool_on_new_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Bookings and escalations hang off a Call, like everything the voice
    agent records: one per MCP tool call, caller_number "mcp:<client>"."""
    practice_id = _scoped_practice_id()
    call_id = await asyncio.to_thread(_open_call, practice_id, _channel())
    failed = True
    try:
        result = await asyncio.to_thread(_run_tool_blocking, name, arguments, practice_id, call_id)
        failed = False
        return result
    finally:
        await asyncio.to_thread(_close_call, call_id, failed)


def _field(model: type[BaseModel], name: str):
    """A tools.py field, reused as an MCP tool argument: one description, one
    set of constraints, whichever protocol the model reaches it through."""
    return model.model_fields[name]


# Times travel as plain strings: tools.py accepts practice-local time with no
# offset, which a JSON-schema "date-time" (RFC 3339, offset required) would
# make a strict client reject before it ever reached us.
_SLOT = Field(description=BookAppointmentInput.model_fields["requested_slot"].description)


# ── Tools ────────────────────────────────────────────────────────────────────
@mcp.tool(
    description="Look up open appointment slots over a range of dates. Each slot has "
                "`start` (pass it back to book_appointment) and `spoken` (show it to the user).",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    structured_output=True,
)
async def check_availability(date_range: Annotated[DateRange, _field(CheckAvailabilityInput, "date_range")]) -> dict[str, Any]:
    return await _run_tool("check_availability", {
        "practice_id": _scoped_practice_id(), "date_range": date_range.model_dump(mode="json"),
    })


class BookingConfirmation(BaseModel):
    confirm: bool = Field(description="Book this appointment?")


async def _confirm_booking(
    caller_name: str, callback_number: str, service: str, requested_slot: str, ctx: Context,
) -> Elicit[BookingConfirmation] | BookingConfirmation:
    """Resolver for book_appointment's `confirmation`: check the booking, then
    read it back to the human and ask. The SDK carries the question the way
    the client's protocol version supports (a request mid-call, or an
    input-required result the client retries), and may run this more than
    once, so it only reads.

    A client without elicitation support gets no question from us: its own
    tool-approval prompt, which shows the user these exact arguments, is the
    confirmation."""
    arguments = {"practice_id": _scoped_practice_id(), "caller_name": caller_name,
                 "callback_number": callback_number, "service": service, "requested_slot": requested_slot}
    read_back = await asyncio.to_thread(_read_back, arguments)
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        return BookingConfirmation(confirm=True)  # the SDK wraps a plain value as accepted
    return Elicit(f"Book this appointment? {read_back}", BookingConfirmation)


@mcp.tool(
    description="Book a new appointment once the user has agreed to a specific date and time. "
                "The user is asked to confirm the details before anything is booked.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                openWorldHint=False),
    structured_output=True,
)
async def book_appointment(
    caller_name: Annotated[str, _field(BookAppointmentInput, "caller_name")],
    callback_number: Annotated[str, _field(BookAppointmentInput, "callback_number")],
    service: Annotated[str, _field(BookAppointmentInput, "service")],
    requested_slot: Annotated[str, _SLOT],
    confirmation: Annotated[ElicitationResult[BookingConfirmation], Resolve(_confirm_booking)],
    notes: Annotated[str | None, _field(BookAppointmentInput, "notes")] = None,
) -> dict[str, Any]:
    arguments = {
        "practice_id": _scoped_practice_id(), "caller_name": caller_name, "callback_number": callback_number,
        "service": service, "requested_slot": requested_slot, "notes": notes,
    }
    if not isinstance(confirmation, AcceptedElicitation) or not confirmation.data.confirm:
        return {"status": "not_booked", "reason": "The user did not confirm."}
    return await _run_tool_on_new_call("book_appointment", arguments)


def _read_back(arguments: dict[str, Any]) -> str:
    """Everything booking will check (slot open, in hours, on the grid, in
    the future) is checked before the user is asked, as the voice gate does,
    and phrased exactly like the voice read-back."""
    try:
        payload = BookAppointmentInput.model_validate(arguments)
    except ValueError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        raise ToolError(f"Invalid input for this tool: {fields}") from None
    with SessionLocal() as db:
        try:
            check = agent_tools.check_booking(db, payload, call_id=None)
        except agent_tools.ToolError as exc:
            raise ToolError(str(exc)) from None
    slots = BookingSlots(
        practice_id=payload.practice_id, caller_name=payload.caller_name,
        callback_number=payload.callback_number, service=payload.service, requested_slot=check.slot,
    )
    return DialogState(slots=slots).read_back_text(check.tz)


@mcp.tool(
    description="Move an existing appointment of this practice to a new date and time.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    structured_output=True,
)
async def reschedule_appointment(
    appointment_id: Annotated[int, _field(RescheduleAppointmentInput, "appointment_id")],
    new_slot: Annotated[str, _SLOT],
    reason: Annotated[str | None, _field(RescheduleAppointmentInput, "reason")] = None,
) -> dict[str, Any]:
    return await _run_tool("reschedule_appointment", {
        "appointment_id": appointment_id, "new_slot": new_slot, "reason": reason,
    })


@mcp.tool(
    description="Hand off to office staff: anything clinical, or anything you can't safely handle. "
                "Records a callback request; returns the line to give the user.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    structured_output=True,
)
async def escalate_to_human(reason: Annotated[str, _field(EscalateToHumanInput, "reason")]) -> dict[str, Any]:
    return await _run_tool_on_new_call("escalate_to_human", {"reason": reason})


@mcp.tool(
    description="Answer a general question about the practice (hours, insurance, policies).",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    structured_output=True,
)
async def answer_faq(question: Annotated[str, _field(AnswerFaqInput, "question")]) -> dict[str, Any]:
    return await _run_tool("answer_faq", {"question": question})


# ── Resource and prompt ──────────────────────────────────────────────────────
def _load_scoped_practice(practice_id: int) -> Practice:
    if practice_id != _scoped_practice_id():
        raise ResourceError(f"No practice with id {practice_id}")  # not "not yours": no existence leak
    with SessionLocal() as db:
        practice = db.get(Practice, practice_id)
    if practice is None:
        raise ResourceError(f"No practice with id {practice_id}")
    return practice


@mcp.resource(
    "practice://{practice_id}/hours",
    name="practice-hours",
    description="Opening hours and timezone of a practice (only the one your credentials are for).",
    mime_type="application/json",
)
async def practice_hours(practice_id: int) -> str:
    practice = await asyncio.to_thread(_load_scoped_practice, int(practice_id))
    return json.dumps({
        "practice_id": practice.id, "name": practice.name, "timezone": practice.timezone,
        "hours": agent_tools.business_hours(practice),
    })


@mcp.prompt(
    name="front-desk-persona",
    description="The front-desk assistant's persona and rules: the same system prompt the phone agent runs on.",
)
async def front_desk_persona() -> str:
    practice = await asyncio.to_thread(_load_scoped_practice, _scoped_practice_id())
    return build_system_prompt(practice)


# ── Transports ───────────────────────────────────────────────────────────────
def build_http_app() -> Starlette:
    """A fresh streamable-HTTP app (and session manager: the SDK allows each
    one to run once), for app/main.py to mount at /mcp and start in its
    lifespan. DNS-rebinding protection stays on: localhost plus
    MCP_ALLOWED_HOSTS only."""
    local = ["127.0.0.1", "localhost", "[::1]"]
    # Both "host" and "host:port": a Host header carries no port on 80/443.
    hosts = [*local, *(f"{h}:*" for h in local), *settings.mcp_allowed_hosts]
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
        ),
    )


def _new_key(practice_id: int) -> None:
    key = f"fdk_{secrets.token_urlsafe(32)}"
    print(f"API key for practice {practice_id} (shown once, give it to the MCP client):\n  {key}")
    print(f"Add to MCP_API_KEYS in .env (merge with existing entries):\n  MCP_API_KEYS='{{\"{hash_api_key(key)}\": {practice_id}}}'")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.mcp_server")
    sub = parser.add_subparsers(dest="command")
    new_key = sub.add_parser("new-key", help="generate an API key for the HTTP transport")
    new_key.add_argument("--practice-id", type=int, required=True)
    args = parser.parse_args()
    if args.command == "new-key":
        _new_key(args.practice_id)
    else:
        mcp.run("stdio")  # stdout is the protocol from here on: nothing else may print to it


if __name__ == "__main__":
    main()
