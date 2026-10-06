"""20-question relevance check for the knowledge base (docs/notes/rag.md).

    python -m experiments.rag_eval.run --practice-id 4
    python -m experiments.rag_eval.run --practice-id 4 --modes keyword   # no Voyage key needed

For each question and mode: where does the first correct chunk rank? A
chunk is correct when it comes from an expected doc and contains every
expected string (questions.json). Reports hit@3 (the DoD: >= 17/20 for
hybrid) and MRR@10, plus each question's ranks so misses can be read, not
just counted.
"""

import argparse
import json
import sys
from pathlib import Path

from app.db import SessionLocal
from app.rag.embed import embedder
from app.rag.search import search

QUESTIONS = Path(__file__).parent / "questions.json"
DEPTH = 10


def _norm(s: str) -> str:
    return s.replace("–", "-").replace("—", "-").lower()


def correct(hit, expect: list[dict]) -> bool:
    body = _norm(hit.text)
    return any(hit.source == e["doc"] and all(_norm(c) in body for c in e["contains"]) for e in expect)


def first_correct_rank(hits, expect) -> int | None:
    return next((i for i, h in enumerate(hits, 1) if correct(h, expect)), None)


def evaluate(db, practice_id: int, questions: list[dict], modes: list[str], k: int = 3) -> dict:
    if set(modes) & {"vector", "hybrid"}:
        # One batched request for all questions, instead of one per search:
        # fills the query cache that search() reads (and fits a 3 RPM tier).
        embedder().embed_queries([q["q"] for q in questions])
    results = {m: [] for m in modes}
    for q in questions:
        for mode in modes:
            result = search(db, practice_id, q["q"], k=DEPTH, mode=mode)
            if result.degraded:
                raise SystemExit(f"{mode} search degraded: {result.degraded}")
            rank = first_correct_rank(result.hits, q["expect"])
            results[mode].append({"id": q["id"], "rank": rank, "top": [h.source for h in result.hits[:k]]})
    summary = {}
    for mode, rows in results.items():
        hits = sum(1 for r in rows if r["rank"] and r["rank"] <= k)
        mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
        summary[mode] = {"hit_at_k": hits, "n": len(rows), "mrr": round(mrr, 3)}
    return {"k": k, "summary": summary, "rows": results}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments.rag_eval.run")
    parser.add_argument("--practice-id", type=int, required=True)
    parser.add_argument("--modes", default="vector,keyword,hybrid")
    parser.add_argument("-k", type=int, default=3)
    parser.add_argument("--json", type=Path, help="also write the full results here")
    args = parser.parse_args()
    questions = json.loads(QUESTIONS.read_text())["questions"]
    modes = args.modes.split(",")

    with SessionLocal() as db:
        out = evaluate(db, args.practice_id, questions, modes, k=args.k)

    width = max(len(q["q"]) for q in questions)
    print(f"{'#':>2}  {'question':<{width}}  " + "  ".join(f"{m:>7}" for m in modes))
    for i, q in enumerate(questions):
        ranks = []
        for m in modes:
            r = out["rows"][m][i]["rank"]
            ranks.append(f"{('—' if r is None else r):>6}{'' if r and r <= args.k else '✗'}")
        print(f"{q['id']:>2}  {q['q']:<{width}}  " + "  ".join(f"{x:>7}" for x in ranks))
    print()
    for m, s in out["summary"].items():
        print(f"{m:>8}: hit@{args.k} {s['hit_at_k']}/{s['n']}   MRR@{DEPTH} {s['mrr']}")
    if args.json:
        args.json.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
