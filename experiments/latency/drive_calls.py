"""Synthetic phone calls for latency measurement: a fake *Twilio* on the
other end of our own `/twilio/ws`.

It speaks the Twilio Media Streams protocol (see
experiments/raw_media_streams/) to the real app: `connected`, `start` with
the same customParameters /twilio/inbound would put there, then 20 ms μ-law
frames in real time — silence, or a caller utterance synthesized once by
Deepgram TTS and cached. Everything on the server side is production code:
serializer, VAD, Smart Turn, Deepgram STT, Claude with tools, Deepgram TTS,
output transport, and app/services/latency.py writing call_metrics. Only the
PSTN leg (carrier + Twilio edge) is missing.

It also measures the one number the server can't see on its own: from the
last *voiced* 20 ms frame the caller sent to the first audio frame back.
That's what the caller perceives, minus the phone network.

    # server, in another terminal (LATENCY_LABEL tags call_metrics rows):
    LATENCY_LABEL=baseline uvicorn app.main:app --port 8765
    # driver:
    python -m experiments.latency.drive_calls --label baseline --port 8765

Results: call_metrics rows (server) + app/temp/latency_runs/<label>.jsonl
(client). Report: python -m experiments.latency.report baseline [other...]
"""

import argparse
import asyncio
import base64
import hashlib
import json
import time
import urllib.request
import uuid
from pathlib import Path

import websockets

from app.config import settings
from app.db import SessionLocal
from app.models.models import Practice

FRAME_MS = 20
FRAME_BYTES = 160  # 8 kHz μ-law, 1 byte per sample
SILENCE = b"\xff" * FRAME_BYTES
CALLER_VOICE = "aura-orion-en"  # not the bot's voice, so transcripts are unambiguous
AUDIO_CACHE = Path("app/temp/latency_audio")
RESULTS_DIR = Path("app/temp/latency_runs")
PRACTICE_PHONE = "+15550009999"  # a test practice, not a real number

# 5 calls x 4 caller turns = 20 turns. Nothing clinical: an escalation ends
# the call through Twilio's REST API, which a fake callSid can't satisfy.
SCRIPTS = [
    ["Hi, I'd like to book a cleaning.",
     "Do you have anything next Wednesday morning?",
     "The earliest one works. My name is Jordan Lee and my number is 813 555 0123.",
     "Yes, that's all correct."],
    ["Hi, what are your office hours?",
     "Do you take Delta Dental insurance?",
     "Okay. Do you have any openings next Friday afternoon?",
     "Great, thanks. I'll call back to book. Bye."],
    ["Hello, I need to schedule a checkup for my daughter.",
     "How about next Thursday at ten in the morning?",
     "Her name is Mia Chen, and the best number is 813 555 0177.",
     "Yes, that's right, thank you."],
    ["Hi there, is the office open on Saturdays?",
     "What about early mornings, before work?",
     "Can you check next Tuesday at nine?",
     "Okay, never mind for now. Thanks."],
    ["Hi, I want to come in for a filling.",
     "Any time on Monday next week works.",
     "Let's do eleven. I'm Sam Park, 813 555 0145.",
     "Yes, please book it."],
]


# ── Caller audio ─────────────────────────────────────────────────────────────
def _ulaw_to_linear(byte: int) -> int:
    """G.711 μ-law byte -> 16-bit linear sample."""
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


def _trim_silence(audio: bytes, threshold: int = 400) -> bytes:
    """Strip leading/trailing near-silence, so "speech end" below means the
    end of the last word, not the end of the TTS buffer."""
    voiced = [i for i, b in enumerate(audio) if abs(_ulaw_to_linear(b)) > threshold]
    return audio[voiced[0]: voiced[-1] + 1] if voiced else b""


def caller_audio(text: str) -> bytes:
    AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
    path = AUDIO_CACHE / f"{hashlib.sha1((CALLER_VOICE + text).encode()).hexdigest()[:16]}.ulaw"
    if not path.exists():
        request = urllib.request.Request(
            f"https://api.deepgram.com/v1/speak?model={CALLER_VOICE}&encoding=mulaw&sample_rate=8000&container=none",
            data=json.dumps({"text": text}).encode(),
            headers={"Authorization": f"Token {settings.deepgram_api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            path.write_bytes(_trim_silence(response.read()))
    return path.read_bytes()


# ── One call ─────────────────────────────────────────────────────────────────
class FakeTwilioCall:
    def __init__(self, ws, stream_sid: str):
        self.ws = ws
        self.stream_sid = stream_sid
        self._pending: list[bytes] = []
        self._utterance_done = asyncio.Event()
        self.last_voice_sent = 0.0
        self.media_times: list[float] = []
        self.media_bytes: list[int] = []
        self.closed = False

    async def send_json(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def sender(self) -> None:
        """Real-time 20 ms frames on an absolute schedule (no drift): the
        queued utterance if there is one, silence otherwise, like a phone."""
        seq, start = 2, time.monotonic()
        while not self.closed:
            if self._pending:
                payload = self._pending.pop(0)
                if not self._pending:
                    self.last_voice_sent = time.monotonic()
                    self._utterance_done.set()
            else:
                payload = SILENCE
            seq += 1
            await self.send_json({
                "event": "media", "sequenceNumber": str(seq), "streamSid": self.stream_sid,
                "media": {"track": "inbound", "chunk": str(seq - 2),
                          "timestamp": str((seq - 2) * FRAME_MS),
                          "payload": base64.b64encode(payload).decode()},
            })
            await asyncio.sleep(max(0.0, start + (seq - 2) * FRAME_MS / 1000 - time.monotonic()))

    async def receiver(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if message.get("event") == "media":
                    self.media_times.append(time.monotonic())
                    self.media_bytes.append(len(base64.b64decode(message["media"]["payload"])))
        except websockets.ConnectionClosed:
            pass
        self.closed = True

    async def say(self, audio: bytes) -> float:
        self._utterance_done.clear()
        self._pending = [audio[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\xff")
                         for i in range(0, len(audio), FRAME_BYTES)]
        await self._utterance_done.wait()
        return self.last_voice_sent

    async def first_audio_after(self, t: float, timeout: float) -> float | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.closed:
            later = [m for m in self.media_times if m > t]
            if later:
                return later[0]
            await asyncio.sleep(0.005)
        return None

    async def wait_until_quiet(self, quiet: float = 1.2, timeout: float = 30.0) -> None:
        """The bot has finished talking once no audio has arrived for `quiet` s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.closed:
            last = self.media_times[-1] if self.media_times else 0.0
            if self.media_times and time.monotonic() - last > quiet:
                return
            await asyncio.sleep(0.05)


def _practice_id() -> int:
    with SessionLocal() as db:
        practice = db.query(Practice).filter_by(phone=PRACTICE_PHONE).first()
        if practice is None:
            practice = Practice(name="Sunshine Dental", timezone="America/New_York", phone=PRACTICE_PHONE)
            db.add(practice)
            db.commit()
        return practice.id


async def run_call(url: str, label: str, index: int, script: list[str], practice_id: int) -> list[dict]:
    run = uuid.uuid4().hex[:8]
    stream_sid, call_sid = f"MZlatency{run}", f"CAlatency{run}"
    results = []
    async with websockets.connect(url, max_size=None) as ws:
        call = FakeTwilioCall(ws, stream_sid)
        await call.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        await call.send_json({
            "event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
            "start": {
                "accountSid": "AC_FAKE_LATENCY_DRIVER", "streamSid": stream_sid, "callSid": call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                # caller_number on the Call row, so a run's calls can be found later.
                "customParameters": {"practice_id": str(practice_id),
                                     "from_number": f"latency-{label}-{run}", "to_number": PRACTICE_PHONE},
            },
        })
        tasks = [asyncio.create_task(call.sender()), asyncio.create_task(call.receiver())]
        await call.first_audio_after(0.0, timeout=20)  # greeting
        await call.wait_until_quiet()

        for turn, text in enumerate(script, start=1):
            if call.closed:
                break
            speech_end = await call.say(caller_audio(text))
            first_audio = await call.first_audio_after(speech_end, timeout=15)
            await call.wait_until_quiet()
            e2e = round((first_audio - speech_end) * 1000) if first_audio else None
            # How much bot audio came back this turn (8 bytes per ms of μ-law):
            # a guard that a TTS change didn't cut replies short.
            bot_audio_ms = sum(b for t, b in zip(call.media_times, call.media_bytes) if t > speech_end) // 8
            results.append({"label": label, "call": index, "run": run, "turn": turn,
                            "client_e2e_ms": e2e, "bot_audio_ms": bot_audio_ms, "chars": len(text)})
            print(f"  call {index} turn {turn}: {e2e} ms", flush=True)
            await asyncio.sleep(0.3)

        call.closed = True
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--calls", type=int, default=len(SCRIPTS))
    args = parser.parse_args()

    for script in SCRIPTS:  # synthesize (or load) all caller audio before timing anything
        for text in script:
            caller_audio(text)

    practice_id = _practice_id()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{args.label}.jsonl"
    for index, script in enumerate(SCRIPTS[: args.calls], start=1):
        print(f"call {index}/{args.calls}", flush=True)
        rows = await run_call(f"ws://localhost:{args.port}/twilio/ws", args.label, index, script, practice_id)
        with out.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        await asyncio.sleep(1.0)
    print(f"client results appended to {out}")


if __name__ == "__main__":
    asyncio.run(main())
