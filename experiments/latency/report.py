"""p50/p95 per stage from call_metrics (server) and the driver's JSONL
(client), one column pair per LATENCY_LABEL, as a Markdown table.

    python -m experiments.latency.report baseline prompt-cache ...

Stage durations are differences between the per-turn offsets in
call_metrics (see app/services/latency.py). Interrupted turns (the caller
talked again before any audio came back) are counted, but left out of every
duration: they have no end.
"""

import json
import sys
from pathlib import Path

import numpy as np

from app.db import SessionLocal
from app.models.models import CallMetric

RESULTS_DIR = Path("app/temp/latency_runs")


def _diff(a, b):
    return None if a is None or b is None else a - b


STAGES = [
    ("Endpointing: speech end → turn released", lambda m: m.turn_end_ms),
    ("  of which VAD silence window", lambda m: m.vad_ms),
    ("  STT final transcript (offset)", lambda m: m.stt_final_ms),
    ("Turn released → LLM request", lambda m: _diff(m.llm_start_ms, m.turn_end_ms)),
    ("LLM request → first token", lambda m: _diff(m.llm_first_token_ms, m.llm_start_ms)),
    ("First token → first TTS audio", lambda m: _diff(m.tts_first_byte_ms, m.llm_first_token_ms)),
    ("First TTS audio → audio out", lambda m: _diff(m.first_audio_out_ms, m.tts_first_byte_ms)),
    ("**End to end (server)**", lambda m: m.first_audio_out_ms),
]


def _pct(values: list[float]) -> tuple[int | None, int | None, int]:
    if not values:
        return None, None, 0
    return round(np.percentile(values, 50)), round(np.percentile(values, 95)), len(values)


def stage_stats(label: str) -> dict[str, tuple]:
    with SessionLocal() as db:
        rows = db.query(CallMetric).filter_by(label=label).all()
    done = [m for m in rows if not m.interrupted]
    stats = {name: _pct([v for m in done if (v := fn(m)) is not None]) for name, fn in STAGES}
    tool_ms = [t["end_ms"] - t["start_ms"] for m in done for t in (m.tools or []) if t.get("end_ms") is not None]
    stats["Tool execution (per call)"] = _pct(tool_ms)
    tool_turns = [m.first_audio_out_ms for m in done if m.tools]
    plain_turns = [m.first_audio_out_ms for m in done if not m.tools]
    stats["  E2E, turns without tools"] = _pct(plain_turns)
    stats["  E2E, turns with tools"] = _pct(tool_turns)

    client = []
    path = RESULTS_DIR / f"{label}.jsonl"
    if path.exists():
        client = [r["client_e2e_ms"] for line in path.read_text().splitlines()
                  if (r := json.loads(line))["client_e2e_ms"] is not None]
    stats["**End to end (client: last voiced frame → first audio frame)**"] = _pct(client)
    stats["_turns / interrupted / calls_"] = (len(rows), sum(m.interrupted for m in rows), len({m.call_id for m in rows}))
    return stats


def main(labels: list[str]) -> None:
    all_stats = {label: stage_stats(label) for label in labels}
    names = list(next(iter(all_stats.values())).keys())
    header = "| Stage | " + " | ".join(f"{l} p50 | {l} p95" for l in labels) + " |"
    print(header)
    print("|---|" + "---:|---:|" * len(labels))
    for name in names:
        cells = []
        for label in labels:
            a, b, n = all_stats[label][name]
            if name.startswith("_turns"):
                cells += [f"{a} turns, {b} interrupted", f"{n} calls"]
            else:
                cells += ["–" if a is None else f"{a}", "–" if b is None else f"{b}"]
        print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(sys.argv[1:] or ["baseline"])
