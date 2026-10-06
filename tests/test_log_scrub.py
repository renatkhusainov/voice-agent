import io
import logging
import re
import sys

import pytest
from loguru import logger
from pipecat.frames.frames import LLMTextFrame, TranscriptionFrame

from app.models.models import Practice
from app.phi.log_scrub import LogScrubber, _StdlibScrubFilter, setup_logging

# Built from pieces so this file never contains a key-shaped literal (test_secrets
# scans every tracked file for exactly those).
FAKE_ANTHROPIC_KEY = "sk-" "ant-api03-" + "X" * 30
FAKE_TWILIO_SID = "AC" + "a1" * 16
REGISTERED = "registered-secret-value-42"


@pytest.fixture
def log_out():
    buf = io.StringIO()
    setup_logging(secrets=[REGISTERED], stream=buf)
    yield buf
    setup_logging()  # back to stderr for the rest of the session


# ── PHI in log lines ───────────────────────────────────────────────────────────
def test_phone_number_in_a_log_message_is_masked(log_out):
    logger.info("the caller said to reach them at (813) 555-0142")

    out = log_out.getvalue()
    assert "555-0142" not in out
    assert "[PHONE]" in out


def test_log_timestamp_is_not_mistaken_for_a_date_of_birth(log_out):
    logger.info("hello")

    line = log_out.getvalue()
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| INFO\s+\|", line)
    assert "[DOB]" not in line


# ── secrets ────────────────────────────────────────────────────────────────────
def test_registered_secret_is_masked_in_messages_and_tracebacks(log_out):
    logger.error(f"auth failed with {REGISTERED}")
    try:
        raise RuntimeError(f"upstream rejected {REGISTERED}")
    except RuntimeError:
        logger.exception("call crashed")

    out = log_out.getvalue()
    assert REGISTERED not in out
    assert "[REDACTED-SECRET]" in out


def test_key_shaped_strings_are_masked_even_when_not_registered(log_out):
    logger.warning(f"key={FAKE_ANTHROPIC_KEY} sid={FAKE_TWILIO_SID} Authorization: Bearer abcdefghijklmnop1234")

    out = log_out.getvalue()
    assert FAKE_ANTHROPIC_KEY not in out
    assert FAKE_TWILIO_SID not in out
    assert "abcdefghijklmnop1234" not in out


def test_short_secret_values_are_ignored_rather_than_mangling_every_line():
    assert LogScrubber(["test"])("a test message") == "a test message"


def test_tracebacks_never_show_local_variable_values(log_out):
    def handler():
        api_secret_local = "LOCALVALUE-that-must-never-print-9876"  # noqa: F841
        raise ValueError("boom")

    try:
        handler()
    except ValueError:
        logger.exception("crashed")

    out = log_out.getvalue()
    assert "LOCALVALUE" not in out
    assert "boom" in out  # still debuggable


# ── Pipecat ────────────────────────────────────────────────────────────────────
def test_pipecat_debug_is_dropped_but_info_and_our_own_debug_survive(log_out):
    pipecat = logger.patch(lambda r: r.update(name="pipecat.services.tts_service"))
    pipecat.debug("TTS request: ctx-1 - I think my son has a cavity")
    pipecat.info("Pipeline started")
    logger.debug("our own debug line")

    out = log_out.getvalue()
    assert "cavity" not in out
    assert "Pipeline started" in out
    assert "our own debug line" in out


def test_frame_payload_is_masked_when_pipecat_logs_a_failing_frame(log_out):
    # Pipecat's ai_service logs "exception processing {frame}" at ERROR, and a
    # frame's repr carries its text. Built from the real classes so a Pipecat
    # change to the repr breaks this test instead of quietly leaking.
    llm_frame = LLMTextFrame(text="my son has a cavity on his back tooth")
    stt_frame = TranscriptionFrame(
        text="my daughter is allergic to penicillin", user_id="u", timestamp="2026-09-21T00:00:00Z"
    )
    logger.error(f"DeepgramTTSService: exception processing {llm_frame}: boom")
    logger.error(f"AnthropicLLMService: exception processing {stt_frame}: boom")

    out = log_out.getvalue()
    assert "cavity" not in out
    assert "penicillin" not in out
    assert "LLMTextFrame" in out and "TranscriptionFrame" in out


# ── database errors ────────────────────────────────────────────────────────────
def test_sql_error_parameters_are_hidden():
    error = (
        '(psycopg.errors.UndefinedTable) relation "transcript_turns" does not exist\n'
        "[SQL: INSERT INTO transcript_turns (call_id, role, text) VALUES (%(call_id)s, %(role)s, %(text)s)]\n"
        "[parameters: {'call_id': 2, 'role': 'assistant', 'text': \"Hello! I'm the front desk\"}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/f405)"
    )

    out = LogScrubber()(error)

    assert "front desk" not in out
    assert "does not exist" in out and "[SQL: INSERT" in out


def test_postgres_row_detail_is_hidden():
    error = (
        'null value in column "text" violates not-null constraint\n'
        "DETAIL:  Failing row contains (5, 2, assistant, my son has a toothache, 2026-09-21)."
    )

    out = LogScrubber()(error)

    assert "toothache" not in out
    assert "violates not-null constraint" in out


def test_db_errors_do_not_echo_bound_values():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db import SessionLocal

    with SessionLocal() as db, pytest.raises(DBAPIError) as raised:
        db.execute(text("INSERT INTO no_such_table (body) VALUES (:body)"), {"body": "WHAT-THE-CALLER-SAID"})

    assert "WHAT-THE-CALLER-SAID" not in str(raised.value)


# ── uvicorn and stdlib ─────────────────────────────────────────────────────────
def test_uvicorn_access_log_scrubs_the_query_string(log_out):
    # A POST body never reaches uvicorn's access log (checked against a live
    # server), but a webhook configured as GET would put From=... in the path.
    access = logging.getLogger("uvicorn.access")
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setFormatter(logging.Formatter("%(message)s"))
    saved = (access.level, access.propagate)
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    access.propagate = False
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:51234", "POST", "/twilio/inbound?From=%2B18135550142&To=8135551234", "1.1", 405,
        )
    finally:
        access.removeHandler(handler)
        access.setLevel(saved[0])
        access.propagate = saved[1]

    out = captured.getvalue()
    assert "8135550142" not in out and "8135551234" not in out
    assert 'POST /twilio/inbound' in out and "405" in out


def test_stdlib_records_are_scrubbed_in_message_and_exception():
    scrub_filter = _StdlibScrubFilter(LogScrubber([REGISTERED]))
    try:
        raise ValueError(f"lookup failed for 813-555-0142 using {REGISTERED}")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "sqlalchemy.engine", logging.ERROR, __file__, 1, "call from %s", ("(813) 555-0142",), exc_info
    )

    assert scrub_filter.filter(record) is True

    assert "555-0142" not in record.getMessage()
    assert "555-0142" not in record.exc_text
    assert REGISTERED not in record.exc_text


# ── our own log lines never carry PHI in the first place ───────────────────────
def test_inbound_webhook_logs_the_sid_but_no_phone_numbers(client, db_session, log_messages):
    db_session.add(Practice(name="Sunshine Dental", timezone="America/New_York", phone="+18135551234"))
    db_session.commit()

    client.post("/twilio/inbound", data={"From": "+18135550142", "To": "+18135551234", "CallSid": "CA111"})
    client.post("/twilio/inbound", data={"From": "+18135550142", "To": "+19999999999", "CallSid": "CA222"})

    logged = " ".join(log_messages)
    assert "CA111" in logged and "CA222" in logged
    assert not re.search(r"813555|999999", logged)
