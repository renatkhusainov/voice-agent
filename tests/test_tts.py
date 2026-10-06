"""app/services/tts.py: speak each sentence as soon as it's written.

No network: the Deepgram websocket is a fake that records what's sent and
replays what Deepgram would answer.
"""

import asyncio
import json

import pytest
from pipecat.services.tts_service import TextAggregationMode

from app.services.tts import (
    EARLY_FLUSH_BUDGET,
    EagerSentenceAggregator,
    FlushingDeepgramTTSService,
    _EndOfReplyFlushedOnly,
)


def sentences(*chunks: str) -> tuple[list[str], str]:
    """Feed LLM text chunks; return (sentences released, text still held)."""
    agg = EagerSentenceAggregator(aggregation_type=TextAggregationMode.SENTENCE)

    async def go():
        out = []
        for chunk in chunks:
            async for a in agg.aggregate(chunk):
                out.append(a.text)
        return out
    return asyncio.run(go()), agg.text.text


# ── EagerSentenceAggregator ──────────────────────────────────────────────────
def test_a_preamble_is_released_without_waiting_for_more_text():
    # The live problem: "Let me check that." is followed by a tool call, not
    # by more text, so Pipecat's lookahead held it until the reply ended.
    assert sentences("Let me", " check that.") == (["Let me check that."], "")


@pytest.mark.parametrize("ending", ["!", "?"])
def test_exclamation_and_question_marks_release_immediately(ending):
    assert sentences(f"Sure{ending}") == ([f"Sure{ending}"], "")


def test_each_sentence_of_a_streamed_reply_is_released_as_it_ends():
    released, held = sentences("We have 9 AM", " open. Would that", " work?", " Great")
    assert released == ["We have 9 AM open.", "Would that work?"]
    assert held.strip() == "Great"


@pytest.mark.parametrize("text", ["That's $29.", "Dr.", "at 9 a.m.", "in the U.S."])
def test_a_period_that_may_not_end_the_sentence_still_waits(text):
    released, held = sentences(text)
    assert released == []
    assert held == text


def test_a_held_period_is_released_by_the_lookahead_as_before():
    # Falls back to Pipecat's own lookahead once more text arrives.
    released, _ = sentences("That's $29.", " Anything else?")
    assert released[0] == "That's $29."


# ── Flushes: one early per reply, budgeted, answers matched in order ─────────
class FakeSocket:
    def __init__(self, replies=()):
        self.sent: list[dict] = []
        self._replies = list(replies)
        self.state = "OPEN"

    async def send(self, message: str):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for reply in self._replies:
            yield reply


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def service(ws=None, clock=None) -> FlushingDeepgramTTSService:
    tts = FlushingDeepgramTTSService(api_key="test", sample_rate=8000, encoding="mulaw")
    tts._websocket = ws or FakeSocket()
    if clock:
        tts._clock = clock
    return tts


def speak(tts, context_id, *sentences_):
    async def go():
        for sentence in sentences_:
            async for _ in tts.run_tts(sentence, context_id):
                pass
    asyncio.run(go())


def test_only_a_replys_first_sentence_gets_an_early_flush():
    tts = service()

    speak(tts, "reply-1", "Let me check that.", "One moment.", "Still here.")
    asyncio.run(tts.flush_audio("reply-1"))
    speak(tts, "reply-2", "We have nine open.", "Does that work?")

    kinds = [m["type"] for m in tts._websocket.sent]
    assert kinds == ["Speak", "Flush", "Speak", "Speak", "Flush", "Speak", "Flush", "Speak"]
    assert list(tts._unanswered) == [False, True, False]  # early, end-of-reply, early


def test_early_flushes_stop_at_the_budget_and_resume_after_the_window():
    # Deepgram allows 20 flushes per ~60 s; a burst of replies must not spend
    # the headroom end-of-reply flushes need.
    clock = FakeClock()
    tts = service(clock=clock)

    for i in range(EARLY_FLUSH_BUDGET + 3):
        speak(tts, f"reply-{i}", "Sure.")
    assert (tts.early_flushes, tts.early_flushes_skipped) == (EARLY_FLUSH_BUDGET, 3)

    clock.now += 61
    speak(tts, "later", "Sure.")
    assert tts.early_flushes == EARLY_FLUSH_BUDGET + 1


def answers(tts, replies):
    async def go():
        return [m async for m in _EndOfReplyFlushedOnly(FakeSocket(replies), tts._on_flush_answer)]
    return asyncio.run(go())


FLUSHED = '{"type":"Flushed","sequence_id":%d}'
REFUSED = '{"type":"Warning","warn_code":"EXCESSIVE_FLUSH","warn_msg":"Rate limit exceeded for flushes."}'


def test_only_the_answer_to_an_end_of_reply_flush_reaches_pipecat():
    tts = service()
    tts._unanswered.extend([False, True])  # early flush, then end-of-reply flush
    audio = b"\x00" * 10

    out = answers(tts, [audio, FLUSHED % 0, audio, FLUSHED % 1])

    assert out == [audio, audio, FLUSHED % 1]


def test_a_refused_early_flush_does_not_shift_later_answers():
    # The live failure: a refused flush gets no sequence_id, so predicting
    # ids drifted and closed a reply early. Matching in order doesn't drift.
    tts = service()
    tts._unanswered.extend([False, True, False, True])  # reply 1: early, end; reply 2: early, end

    out = answers(tts, [FLUSHED % 0, FLUSHED % 1, REFUSED, FLUSHED % 2])

    assert out == [FLUSHED % 1, FLUSHED % 2]  # both end-of-reply answers, nothing else
    assert not tts._unanswered


def test_answer_order_with_a_refusal_in_the_middle():
    tts = service()
    tts._unanswered.extend([False, False, True])

    out = answers(tts, [FLUSHED % 0, REFUSED, FLUSHED % 1])

    assert out == [FLUSHED % 1]
    assert not tts._unanswered


def test_other_messages_pass_through_untouched():
    tts = service()
    replies = ['{"type":"Metadata"}', '{"type":"Cleared","sequence_id":0}', b"\x01"]
    assert answers(tts, replies) == replies


def test_state_resets_on_a_new_connection():
    tts = service()
    speak(tts, "reply-1", "Sure.")
    tts._websocket = FakeSocket()  # reconnect

    asyncio.run(tts.flush_audio("reply-1"))

    assert list(tts._unanswered) == [True]


def test_token_aggregation_is_refused():
    with pytest.raises(ValueError):
        FlushingDeepgramTTSService(api_key="test", text_aggregation_mode=TextAggregationMode.TOKEN)
