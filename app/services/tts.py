"""Deepgram TTS that starts speaking a reply's first sentence as soon as it's
written, instead of after the whole reply.

Measured (docs/notes/latency-budget.md), and why this exists: Deepgram's
streaming TTS produces no audio for buffered text until it gets a `Flush`.
With one, the first audio arrives ~130-145 ms later. Pipecat 1.10 sends one
Flush per reply, when the LLM finishes. So on a tool turn a "Let me check
that." preamble waited for the tool call's JSON to be generated, and on any
turn the first sentence waited for the rest of the reply.

Three pieces:

  * EagerSentenceAggregator releases a sentence the moment it ends. Pipecat's
    aggregator waits for the *next* non-space character to rule out "$29.50"
    and "Dr. Lee". A preamble followed by a tool call never gets one. Here
    `!` and `?` release immediately, and so does a `.` after an ordinary
    word; a `.` after a digit, an initial or a known abbreviation still waits
    for Pipecat's lookahead.

  * One early Flush per reply: after its first sentence only. That's the one
    that sets time-to-first-audio; the rest go out with Pipecat's own
    end-of-reply Flush. Deepgram allows 20 flushes per ~60 s per connection
    (measured: a 21st is refused with EXCESSIVE_FLUSH, and the allowance was
    back ~64 s later). Flushing every sentence hit that limit in a 4-turn
    call, so early flushes also stop once the last minute holds
    EARLY_FLUSH_BUDGET of them, leaving headroom for the end-of-reply ones,
    which a reply can't finish without.

  * Pipecat reads any `Flushed` reply as "this reply's audio is done" and
    closes its audio context, which would cut a reply after its first
    sentence. So each `Flushed` (or refusal) is matched, in order, to the
    Flush it answers, and only answers to end-of-reply flushes reach Pipecat.
"""

import json
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

__all__ = ["EagerSentenceAggregator", "FlushingDeepgramTTSService"]

FLUSH_LIMIT_WINDOW_SECS = 60.0
# Deepgram's limit is 20 per window. Early flushes may use 12 of them; the
# other 8 are headroom for end-of-reply flushes (one per reply).
EARLY_FLUSH_BUDGET = 12

# Words a period doesn't end a sentence after. Lowercased, without the period.
ABBREVIATIONS = frozenset({
    "dr", "mr", "mrs", "ms", "st", "jr", "sr", "vs", "etc", "no", "approx", "appt",
})
_LAST_WORD = re.compile(r"(\S+)\.$")


class EagerSentenceAggregator(SimpleTextAggregator):
    def _period_ends_sentence(self) -> bool:
        match = _LAST_WORD.search(self._text)
        if match is None:
            return False
        word = match.group(1).lower()
        if word[-1].isdigit():                    # "$29." could be "$29.50"
            return False
        if "." in word or word in ABBREVIATIONS:  # "a.m.", "U.S.", "Dr."
            return False
        if len(word) == 1 and word.isalpha():     # an initial, or the "a." of "a.m."
            return False
        return True

    async def _check_sentence_with_lookahead(self, char: str) -> Aggregation | None:
        if not self._needs_lookahead and (char in "!?" or (char == "." and self._period_ends_sentence())):
            sentence, self._text = self._text, ""
            return Aggregation(text=sentence.strip(" "), type=AggregationType.SENTENCE)
        return await super()._check_sentence_with_lookahead(char)


class _EndOfReplyFlushedOnly:
    """The Deepgram websocket as Pipecat's receive loop sees it. A `Flushed`
    or an EXCESSIVE_FLUSH refusal is handed to `on_flush_answer`, which says
    whether Pipecat should see it. Everything else (audio, other messages,
    send, state, close) passes straight through."""

    def __init__(self, ws: Any, on_flush_answer: Callable[[bool], bool]):
        self._ws = ws
        self._on_flush_answer = on_flush_answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        async for message in self._ws:
            if isinstance(message, str):
                if '"Flushed"' in message:
                    if not self._on_flush_answer(True):
                        continue
                elif '"Warning"' in message:
                    # Pipecat prints only msg["description"], which Deepgram's
                    # TTS warnings don't carry. Log the whole thing: service
                    # metadata (codes, limits), never the text being spoken.
                    logger.warning(f"Deepgram TTS warning: {message}")
                    if "EXCESSIVE_FLUSH" in message:
                        self._on_flush_answer(False)
                        continue
            yield message


class FlushingDeepgramTTSService(DeepgramTTSService):
    def __init__(self, **kwargs):
        kwargs.setdefault("text_aggregation_mode", TextAggregationMode.SENTENCE)
        if kwargs["text_aggregation_mode"] != TextAggregationMode.SENTENCE:
            raise ValueError("FlushingDeepgramTTSService flushes by sentence; it needs SENTENCE aggregation")
        super().__init__(**kwargs)
        self._text_aggregator = EagerSentenceAggregator(aggregation_type=TextAggregationMode.SENTENCE)
        self._clock: Callable[[], float] = time.monotonic
        self._socket_seen: Any = None
        self._unanswered: deque[bool] = deque()      # sent flushes, in order: True = end-of-reply
        self._early_sent_at: deque[float] = deque()  # early flushes in the last window
        self._early_flushed_context: str | None = None
        self.early_flushes = 0
        self.early_flushes_skipped = 0

    def _track_socket(self) -> None:
        # Unanswered flushes and the rate window belong to one connection.
        if self._websocket is not self._socket_seen:
            self._socket_seen = self._websocket
            self._unanswered.clear()
            self._early_sent_at.clear()

    def _on_flush_answer(self, accepted: bool) -> bool:
        """Deepgram answered the oldest unanswered Flush. Returns whether
        Pipecat should see the answer: only for an accepted end-of-reply one."""
        end_of_reply = self._unanswered.popleft() if self._unanswered else True
        if not accepted and end_of_reply:
            # Stock Pipecat has the same exposure; with the early-flush budget
            # this needs >20 replies in a minute.
            logger.error(f"{self}: Deepgram refused an end-of-reply Flush (rate limit)")
        return accepted and end_of_reply

    def _early_flush_allowed(self) -> bool:
        now = self._clock()
        while self._early_sent_at and now - self._early_sent_at[0] > FLUSH_LIMIT_WINDOW_SECS:
            self._early_sent_at.popleft()
        return len(self._early_sent_at) < EARLY_FLUSH_BUDGET

    async def _send_flush(self, *, end_of_reply: bool) -> None:
        self._track_socket()
        self._unanswered.append(end_of_reply)
        await self._websocket.send(json.dumps({"type": "Flush"}))

    def _get_websocket(self):
        self._track_socket()
        return _EndOfReplyFlushedOnly(super()._get_websocket(), self._on_flush_answer)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        async for frame in super().run_tts(text, context_id):
            yield frame
        if not self._websocket or context_id == self._early_flushed_context:
            return  # not a reply's first sentence
        self._early_flushed_context = context_id
        self._track_socket()
        if self._early_flush_allowed():
            self._early_sent_at.append(self._clock())
            self.early_flushes += 1
            await self._send_flush(end_of_reply=False)
        else:
            self.early_flushes_skipped += 1

    async def flush_audio(self, context_id: str | None = None):
        """Pipecat's end-of-reply flush: the one whose Flushed closes the context."""
        if self._websocket:
            try:
                await self._send_flush(end_of_reply=True)
            except Exception as exc:
                logger.error(f"{self} error sending Flush message: {type(exc).__name__}")
