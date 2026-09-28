"""Keep PHI and secrets out of logs.

Two log systems are in play and both are covered:
  * loguru  - our own code and Pipecat.
  * stdlib  - uvicorn, SQLAlchemy, httpx and everything else.

Approach, in order of strength:
  1. Don't log content in the first place (our own log lines carry call_id,
     roles, counts and character totals, never text or phone numbers).
  2. Clamp Pipecat. It logs whole conversations, TTS text and caller numbers at
     DEBUG (e.g. "Generating chat from context [...]", "TTS request: ... - text",
     "Parsed - Type: twilio, Data: {from_number...}"), so its DEBUG output is
     dropped outright. INFO and above stay.
  3. Scrub every line that does get written (scrub() below): PHI shapes via
     redact(), registered secret values, key-shaped strings, the payload of
     Pipecat frame reprs, and database error details.
  4. No loguru `diagnose`: it prints the *values* of local variables inside
     tracebacks, which is how an API key or a transcript ends up in a log.

Only the message and the exception text are scrubbed, never the timestamp: a
log timestamp is date-shaped and redact() would happily turn it into [DOB].
"""

import logging
import os
import re
import sys
import traceback
from collections.abc import Iterable
from typing import TextIO

from loguru import logger

from app.phi.redact import redact

__all__ = ["LogScrubber", "secrets_from_environ", "setup_logging"]

SECRET_MASK = "[REDACTED-SECRET]"
MIN_SECRET_LEN = 8  # shorter values would mask ordinary words in every log line

_KEY_SHAPES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic
    re.compile(r"\b(?:AC|SK)[0-9a-f]{32}\b"),  # Twilio account / API key SIDs
    re.compile(r"(?i)\b(?:bearer|token|basic)\s+[A-Za-z0-9._~+/=\-]{16,}"),  # auth headers
]

# Pipecat frame reprs embed the payload: "LLMTextFrame#0(pts: None, text: [..])",
# "TranscriptionFrame#1(user: u, text: [..], language: None, ...)". ai_service.py
# logs `exception processing {frame}` at ERROR, so a processor crash prints the
# caller's words even with DEBUG clamped.
_FRAME_TEXT = re.compile(r"text: \[.*?\](?=,\s*\w+:|\)|\s*$)", re.DOTALL)

# SQLAlchemy appends "[parameters: {...}]" to DB errors: the INSERT's values, i.e.
# the transcript text. hide_parameters=True on the engine stops that at the source;
# this is the backstop.
_SQL_PARAMS = re.compile(r"\[parameters: .*?\](?=\s*(?:\(Background on this error|\Z))", re.DOTALL)
# Postgres error DETAIL lines carry row values ("Failing row contains (...)").
_PG_DETAIL = re.compile(r"(?m)^(\s*DETAIL:).*$")

# No leading underscore on purpose: NGROK_AUTHTOKEN has none before TOKEN.
_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD)$")


def secrets_from_environ() -> list[str]:
    """Values of environment variables that look like credentials (…_KEY, …_TOKEN…)."""
    return [v for k, v in os.environ.items() if _SECRET_ENV_NAME.search(k) and v]


class LogScrubber:
    """Callable that masks secrets and PHI in a piece of log text."""

    def __init__(self, secrets: Iterable[str] = ()):
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True
        )

    def __call__(self, text: str) -> str:
        text = _SQL_PARAMS.sub("[parameters: <hidden>]", text)
        text = _PG_DETAIL.sub(r"\1 <hidden>", text)
        text = _FRAME_TEXT.sub("text: [<redacted>]", text)
        for secret in self._secrets:
            text = text.replace(secret, SECRET_MASK)
        for shape in _KEY_SHAPES:
            text = shape.sub(SECRET_MASK, text)
        return redact(text)


# ── loguru ─────────────────────────────────────────────────────────────────────
def _drop_pipecat_debug(record) -> bool:
    return not (record["name"] or "").startswith("pipecat") or record["level"].no >= 20


def _render(record, scrub: LogScrubber) -> str:
    stamp = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = (
        f"{stamp} | {record['level'].name:<8} | "
        f"{record['name']}:{record['function']}:{record['line']} - {scrub(record['message'])}"
    )
    exc = record["exception"]
    if exc is not None:
        trace = "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
        line += "\n" + scrub(trace)
    return line + "\n"


# ── stdlib logging ─────────────────────────────────────────────────────────────
class _StdlibScrubFilter(logging.Filter):
    def __init__(self, scrub: LogScrubber):
        super().__init__()
        self._scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            # (client_addr, method, path, http_version, status). uvicorn's own
            # formatter unpacks these, so scrub the path in place: a webhook
            # configured as GET would put From=... in the query string.
            args = list(record.args)
            args[2] = self._scrub(str(args[2]))
            record.args = tuple(args)
        else:
            record.msg = self._scrub(record.getMessage())
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._scrub(logging.Formatter().formatException(record.exc_info))
        return True


def _add_filter_once(target, scrub: LogScrubber) -> None:
    if not any(isinstance(f, _StdlibScrubFilter) for f in target.filters):
        target.addFilter(_StdlibScrubFilter(scrub))


def setup_logging(secrets: Iterable[str] = (), stream: TextIO | None = None) -> LogScrubber:
    """Install the scrubbing log pipeline. Safe to call more than once.

    `stream` defaults to sys.stderr, resolved at write time so pytest's capture
    (and anything else that swaps sys.stderr) keeps working.
    """
    scrub = LogScrubber(secrets)

    def sink(message) -> None:
        out = stream or sys.stderr
        out.write(_render(message.record, scrub))
        out.flush()

    logger.remove()
    logger.add(
        sink,
        level="DEBUG",
        filter=_drop_pipecat_debug,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )

    root = logging.getLogger()
    if not root.handlers:
        # Without a handler, Python's last-resort handler prints WARNING+ raw.
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        root.addHandler(handler)
    for handler in root.handlers:
        _add_filter_once(handler, scrub)
    for name in ("uvicorn.error", "uvicorn.access"):
        _add_filter_once(logging.getLogger(name), scrub)
    return scrub
