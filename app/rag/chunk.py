"""Markdown → chunks, by hand. Headings first, then size.

1. Split the doc into sections at headings (#, ##, ###). A heading is the
   strongest topic boundary a doc gives you, so a chunk never straddles one
   if it can avoid it.
2. Pack whole sections into a chunk while it stays under `max_tokens`. Our
   docs' sections are short (60–150 words), so a chunk is usually 2–4
   neighboring sections of one topic.
3. A section too big on its own is split by paragraphs, then sentences, and
   consecutive pieces of it overlap by ~`overlap` tokens, so a fact that
   spans the cut is whole in at least one chunk. No overlap across a heading
   boundary: there the cut is clean by construction.

Every chunk starts with the doc title, and each section keeps its heading.
"Late fee: $50" means nothing to a search on its own, but "Cancellation and
no-show policy > Late cancellations: $50" does, for both the embedding and
the keyword index.
"""

import re
from dataclasses import dataclass

from app.rag.embed import estimate_tokens

VERSION = 1  # bump when the output changes: the ingest hash includes it
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    tokens: int


def _sections(markdown: str) -> tuple[str, list[str]]:
    """(title, [section text with its heading]). Text before the first ##
    belongs to the title's section."""
    title, sections, current = "", [], []
    for line in markdown.splitlines():
        m = _HEADING.match(line)
        if m and len(m.group(1)) == 1 and not title:
            title = m.group(2).strip()
            continue
        if m and current and any(s.strip() for s in current if not _HEADING.match(s)):
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if any(line.strip() for line in current):
        sections.append("\n".join(current).strip())
    return title, sections


def _split_long(section: str, max_tokens: int, overlap: int) -> list[str]:
    """Paragraphs, then sentences; pieces under max_tokens with overlap."""
    units = []
    for para in re.split(r"\n\s*\n", section):
        units += [para] if estimate_tokens(para) <= max_tokens else _SENTENCE.split(para)
    pieces, current = [], []
    for unit in units:
        if current and estimate_tokens("\n\n".join(current + [unit])) > max_tokens:
            pieces.append("\n\n".join(current))
            tail, size = [], 0  # carry whole units from the end, up to `overlap` tokens
            for prev in reversed(current):
                size += estimate_tokens(prev)
                if size > overlap:
                    break
                tail.insert(0, prev)
            current = tail
        current.append(unit)
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def chunk_markdown(markdown: str, *, max_tokens: int = 500, overlap: int = 60) -> list[Chunk]:
    title, sections = _sections(markdown)
    header = f"{title}\n\n" if title else ""
    budget = max_tokens - estimate_tokens(header)
    pieces: list[str] = []
    for section in sections:
        if estimate_tokens(section) > budget:
            pieces += _split_long(section, budget, overlap)
        else:
            pieces.append(section)
    # Balanced, not greedy: greedy packing turns 540 tokens into 480 + 60, and
    # a 60-token tail has too little context to compete in search. Decide
    # the chunk count first, then aim every chunk at an equal share.
    total = sum(estimate_tokens(p) for p in pieces)
    target = total / max(1, -(-total // budget))
    chunks, current = [], ""
    for piece in pieces:
        if current and (estimate_tokens(f"{current}\n\n{piece}") > budget
                        or estimate_tokens(current) >= target):
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return [Chunk(i, header + text, estimate_tokens(header + text)) for i, text in enumerate(chunks)]
