"""Ingest a practice's knowledge base: markdown → chunks → embeddings → rows.
See docs/notes/rag.md.

    python -m scripts.ingest --practice-id 4
    python -m scripts.ingest --practice-id 4 --dir kb/sunshine-dental --prune

**Idempotent by content hash.** Each document's hash covers its text, the
chunker version and the embedding model. Unchanged → skipped, no API call.
Changed → its chunks are rebuilt in one transaction, and any chunk whose
text is unchanged keeps its stored embedding (looked up by chunk hash), so
editing one paragraph re-embeds one chunk, not the doc. A second run over
the same folder embeds nothing.

Three phases: chunk every changed doc, embed all missing chunks in one
batched call, then write each doc. Embedding happens before any write, so a
failed API call leaves the database exactly as it was.

Files whose name starts with `_` (the fact sheet) are not ingested.
`--prune` deletes documents whose file is gone.
"""

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.models import Chunk, Document, Practice
from app.rag import chunk as chunker
from app.rag.embed import Embedder, embedder

DEFAULT_DIR = Path("kb/sunshine-dental")


@dataclass
class IngestReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    chunks_embedded: int = 0
    chunks_reused: int = 0


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def doc_hash(text: str, model: str) -> str:
    return _sha(text, f"chunker-v{chunker.VERSION}", model)


def chunk_hash(text: str, model: str) -> str:
    return _sha(model, text)


def title_of(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def kb_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.md") if not p.name.startswith("_"))


def ingest(db: Session, practice_id: int, directory: Path, *, emb: Embedder | None = None,
           prune: bool = False) -> IngestReport:
    emb = emb or embedder()
    report = IngestReport()
    existing = {d.source: d for d in db.scalars(select(Document).where(Document.practice_id == practice_id))}
    seen = set()

    # 1. Plan: chunk every new or changed doc, nothing written yet.
    changed = []  # (source, path, text, digest, pieces, hashes)
    for path in kb_files(directory):
        source = path.relative_to(directory).as_posix()
        seen.add(source)
        text = path.read_text()
        digest = doc_hash(text, emb.model)
        doc = existing.get(source)
        if doc is not None and doc.content_hash == digest:
            report.unchanged.append(source)
            continue
        pieces = chunker.chunk_markdown(text)
        changed.append((source, path, text, digest, pieces, [chunk_hash(p.text, emb.model) for p in pieces]))

    # 2. Embed: every chunk text the practice doesn't already have, across all
    #    changed docs, in one batched call (one request per doc would be 31
    #    requests, and a free Voyage account allows 3 a minute).
    all_hashes = {h for *_, hashes in changed for h in hashes}
    stored = dict(db.execute(
        select(Chunk.content_hash, Chunk.embedding).join(Document)
        .where(Document.practice_id == practice_id, Chunk.content_hash.in_(all_hashes))
    ).all()) if all_hashes else {}
    todo = {h: p.text for *_, pieces, hashes in changed for p, h in zip(pieces, hashes) if h not in stored}
    if todo:
        stored.update(zip(todo, emb.embed_documents(list(todo.values()))))
    report.chunks_embedded = len(todo)
    report.chunks_reused = sum(len(hashes) for *_, hashes in changed) - len(todo)

    # 3. Write: one transaction per doc.
    for source, path, text, digest, pieces, hashes in changed:
        doc = existing.get(source)
        if doc is None:
            doc = Document(practice_id=practice_id, source=source)
            db.add(doc)
            report.created.append(source)
        else:
            doc.chunks.clear()
            db.flush()  # delete old chunks before the new ones reuse their ordinals
            report.updated.append(source)
        doc.title = title_of(text, path.stem)
        doc.content_hash = digest
        doc.chunks.extend(
            Chunk(ordinal=p.ordinal, text=p.text, token_count=p.tokens, content_hash=h, embedding=stored[h])
            for p, h in zip(pieces, hashes)
        )
        db.commit()

    if prune:
        for source, doc in existing.items():
            if source not in seen:
                db.delete(doc)
                report.deleted.append(source)
        db.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ingest")
    parser.add_argument("--practice-id", type=int, required=True)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--prune", action="store_true", help="delete documents whose file is gone")
    args = parser.parse_args()

    with SessionLocal() as db:
        if db.get(Practice, args.practice_id) is None:
            print(f"No practice {args.practice_id}", file=sys.stderr)
            return 1
        r = ingest(db, args.practice_id, args.dir, prune=args.prune)
    print(f"practice {args.practice_id}, {args.dir}: {len(r.created)} created, {len(r.updated)} updated, "
          f"{len(r.unchanged)} unchanged, {len(r.deleted)} deleted; "
          f"chunks embedded {r.chunks_embedded}, reused {r.chunks_reused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
