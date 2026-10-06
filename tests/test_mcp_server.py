"""app/mcp_server.py over the real MCP protocol (the SDK's own Client), not
by calling the Python functions directly.

In-process for tools, resource and prompt (stdio-style: the practice comes
from MCP_PRACTICE_ID). Over streamable HTTP, against app/main.py with its
real lifespan, for the API key -> practice mapping and tenant isolation.
No network: HTTP goes through an in-memory ASGI transport.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult

from app import mcp_server
from app.agent.tools import BookAppointmentInput
from app.config import settings
from app.main import app
from app.models.models import Appointment, AppointmentStatus, Call, CallStatus, Lead, Practice
from app.services.calls import start_call
from tests.conftest import TestingSessionLocal

NY = ZoneInfo("America/New_York")


def next_weekday_at(hour: int, n: int = 1) -> datetime:
    """The n-th weekday from tomorrow at `hour`:00 New York time, as UTC."""
    day, found = date.today(), 0
    while found < n:
        day += timedelta(days=1)
        found += day.weekday() < 5
    return datetime(day.year, day.month, day.day, hour, tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture
def practices(db_session, monkeypatch):
    monkeypatch.setattr(mcp_server, "SessionLocal", TestingSessionLocal)
    a = Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234")
    b = Practice(name="Other Dental", timezone="America/New_York", phone="+18135559999")
    db_session.add_all([a, b])
    db_session.commit()
    monkeypatch.setattr(settings, "mcp_practice_id", a.id)  # stdio scope
    return a, b


def accept(confirm=True):
    async def callback(context, params):
        callback.asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": confirm})
    callback.asked = []
    return callback


def run(coro):
    return asyncio.run(coro)


async def call(tool, arguments, **client_kwargs):
    async with Client(mcp_server.mcp, **client_kwargs) as client:
        return await client.call_tool(tool, arguments)


def booking(slot: datetime | str):
    return {"caller_name": "Dana Lee", "callback_number": "+18135550142", "service": "cleaning",
            "requested_slot": slot if isinstance(slot, str) else slot.isoformat()}


# ── What's exposed ───────────────────────────────────────────────────────────
def test_the_five_tools_are_exposed_and_none_takes_a_practice_id(practices):
    async def go():
        async with Client(mcp_server.mcp) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in run(go())}

    assert set(tools) == {"check_availability", "book_appointment", "reschedule_appointment",
                          "escalate_to_human", "answer_faq"}
    # The model can't choose the practice: it comes from the credential.
    assert "practice_id" not in json.dumps([t.input_schema for t in tools.values()])
    # Argument descriptions are tools.py's own fields, not a second copy.
    props = tools["book_appointment"].input_schema["properties"]
    assert props["caller_name"]["description"] == BookAppointmentInput.model_fields["caller_name"].description
    assert tools["check_availability"].annotations.read_only_hint is True


# ── Tools ────────────────────────────────────────────────────────────────────
def test_check_availability_is_scoped_to_the_configured_practice(practices):
    a, _ = practices
    day = next_weekday_at(10).astimezone(NY).date().isoformat()

    result = run(call("check_availability", {"date_range": {"start_date": day, "end_date": day}}))

    assert not result.is_error
    assert result.structured_content["practice_id"] == a.id
    assert result.structured_content["slots"][0]["spoken"].startswith("9 AM")


def test_booking_asks_the_user_and_writes_an_appointment_on_an_mcp_call(practices, db_session):
    a, _ = practices
    elicit = accept()

    result = run(call("book_appointment", booking(next_weekday_at(10)), elicitation_callback=elicit))

    assert not result.is_error, result.content
    # The server read it back to the human, like the voice gate does.
    assert elicit.asked and "Dana Lee" in elicit.asked[0] and "ending in 0 1 4 2" in elicit.asked[0]
    appointment = db_session.get(Appointment, result.structured_content["appointment_id"])
    assert appointment.status == AppointmentStatus.confirmed
    assert appointment.fhir_appointment_id  # the booking lives in the scheduling system
    call_row = db_session.get(Call, db_session.get(Lead, appointment.lead_id).call_id)
    assert (call_row.practice_id, call_row.caller_number, call_row.status) == (a.id, "mcp:stdio", CallStatus.completed)


def test_a_local_time_without_offset_is_the_practices_local_time(practices, db_session):
    slot = next_weekday_at(10)
    local = slot.astimezone(NY).replace(tzinfo=None).isoformat()  # "2026-10-02T10:00:00"

    result = run(call("book_appointment", booking(local), elicitation_callback=accept()))

    assert result.structured_content["spoken_time"].startswith("10 AM")
    stored = db_session.get(Appointment, result.structured_content["appointment_id"]).requested_slot
    assert stored.replace(tzinfo=timezone.utc) == slot


def test_nothing_is_booked_when_the_user_declines(practices, db_session):
    result = run(call("book_appointment", booking(next_weekday_at(10)), elicitation_callback=accept(confirm=False)))

    assert result.structured_content["status"] == "not_booked"
    assert db_session.query(Appointment).count() == 0


def test_without_elicitation_support_the_clients_own_tool_approval_is_the_confirmation(practices, db_session):
    result = run(call("book_appointment", booking(next_weekday_at(10))))

    assert not result.is_error
    assert db_session.query(Appointment).count() == 1


def test_an_unbookable_time_is_refused_before_the_user_is_asked(practices, db_session):
    elicit = accept()
    sunday = datetime(2030, 1, 13, 15, tzinfo=NY)

    result = run(call("book_appointment", booking(sunday), elicitation_callback=elicit))

    assert result.is_error
    assert "isn't an appointment time" in result.content[0].text
    assert elicit.asked == []
    assert db_session.query(Appointment).count() == 0
    assert db_session.query(Call).count() == 0  # refused at the proposal: nothing opened


def test_another_practices_appointment_cant_be_rescheduled(practices, db_session):
    _, b = practices
    other_call = start_call(db_session, b.id, "+18135550000")
    lead = Lead(call_id=other_call.id)
    db_session.add(lead)
    db_session.flush()
    theirs = Appointment(lead_id=lead.id, requested_slot=next_weekday_at(10), status=AppointmentStatus.pending)
    db_session.add(theirs)
    db_session.commit()

    result = run(call("reschedule_appointment", {"appointment_id": theirs.id,
                                                 "new_slot": next_weekday_at(11).isoformat()}))

    assert result.is_error
    assert f"No appointment with id {theirs.id}" in result.content[0].text  # "not found", not "not yours"
    db_session.refresh(theirs)
    assert theirs.status == AppointmentStatus.pending


def test_escalation_records_a_callback_lead(practices, db_session):
    result = run(call("escalate_to_human", {"reason": "caller reports severe tooth pain"}))

    assert "call you back" in result.structured_content["message_for_caller"]
    assert db_session.query(Lead).one().notes == "caller reports severe tooth pain"


def test_no_configured_practice_means_no_tools(practices, monkeypatch):
    monkeypatch.setattr(settings, "mcp_practice_id", None)

    result = run(call("answer_faq", {"question": "Do you take Delta?"}))

    assert result.is_error
    assert "MCP_PRACTICE_ID" in result.content[0].text


# ── Resource and prompt ──────────────────────────────────────────────────────
def test_hours_resource_for_the_scoped_practice(practices):
    a, b = practices

    async def go():
        async with Client(mcp_server.mcp) as client:
            own = await client.read_resource(f"practice://{a.id}/hours")
            with pytest.raises(Exception):
                await client.read_resource(f"practice://{b.id}/hours")
            return own
    own = run(go())

    body = json.loads(own.contents[0].text)
    assert body["name"] == "Sunshine Dental"
    assert body["hours"]["mon"] == "09:00-17:00" and body["hours"]["sun"] == "closed"


def test_front_desk_persona_prompt_is_the_phone_agents_system_prompt(practices):
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.get_prompt("front-desk-persona")
    prompt = run(go())

    text = prompt.messages[0].content.text
    assert "Sunshine Dental" in text and "Never diagnose" in text


# ── Streamable HTTP: API key -> practice ─────────────────────────────────────
KEY_A, KEY_B = "fdk_test_key_for_practice_a", "fdk_test_key_for_practice_b"


@pytest.fixture
def http(practices, monkeypatch):
    a, b = practices
    monkeypatch.setattr(settings, "mcp_practice_id", None)  # HTTP never falls back to the stdio scope
    verifier = mcp_server.ApiKeyVerifier({mcp_server.hash_api_key(KEY_A): a.id, mcp_server.hash_api_key(KEY_B): b.id})
    monkeypatch.setattr(mcp_server.mcp, "_token_verifier", verifier)
    return a, b


async def over_http(key: str | None, action):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://localhost",
                                      headers=headers) as http_client:
            if action is None:
                return await http_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                              headers={"Accept": "application/json, text/event-stream"})
            transport = streamable_http_client("http://localhost/mcp", http_client=http_client)
            async with Client(transport) as client:
                return await action(client)


@pytest.mark.parametrize("key", [None, "fdk_wrong"])
def test_http_without_a_valid_key_is_rejected(http, key):
    response = run(over_http(key, None))
    assert response.status_code == 401


def test_http_each_key_acts_only_for_its_own_practice(http, db_session):
    a, b = http

    async def book(client):
        return await client.call_tool("book_appointment", booking(next_weekday_at(10)))

    async def peek_at_a(client):
        with pytest.raises(Exception):
            await client.read_resource(f"practice://{a.id}/hours")
        return await client.read_resource(f"practice://{b.id}/hours")

    booked = run(over_http(KEY_A, book))
    b_hours = run(over_http(KEY_B, peek_at_a))

    appointment = db_session.get(Appointment, booked.structured_content["appointment_id"])
    call_row = db_session.get(Call, db_session.get(Lead, appointment.lead_id).call_id)
    assert (call_row.practice_id, call_row.caller_number) == (a.id, f"mcp:practice-{a.id}")
    assert json.loads(b_hours.contents[0].text)["name"] == "Other Dental"


def test_a_practice_id_smuggled_into_the_arguments_is_ignored(practices):
    a, b = practices
    day = next_weekday_at(10).astimezone(NY).date().isoformat()

    result = run(call("check_availability", {"practice_id": b.id, "date_range": {"start_date": day, "end_date": day}}))

    assert result.is_error or result.structured_content["practice_id"] == a.id


def test_http_rejects_a_foreign_host_header(http):
    # DNS-rebinding protection: a page on evil.example resolving to 127.0.0.1
    # can't drive a local MCP server from the user's browser.
    async def go():
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://evil.example",
                                          headers={"Authorization": f"Bearer {KEY_A}"}) as c:
                return await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    headers={"Accept": "application/json, text/event-stream"})
    assert run(go()).status_code == 421
