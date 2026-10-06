"""Knowledge-base search for one practice: vector, keyword, or both fused.

- **vector:** cosine distance (`<=>`) between the query's embedding and each
  chunk's, through the HNSW index. Finds "can you put me to sleep?" →
  sedation without sharing a word.
- **keyword:** Postgres full-text search on the generated `tsv` column.
  Finds exact tokens the embedding blurs: "D4341", "Delta", "Albany".
- **hybrid:** reciprocal rank fusion of the two lists,
  score = Σ 1 / (60 + rank). It uses ranks only, so it never has to compare
  a cosine similarity with a ts_rank (different scales). A chunk near the
  top of both lists wins.

Every query filters on documents.practice_id: tenant isolation, as in
scheduling. One practice's prices never answer another practice's caller.

If the query can't be embedded (no key, Voyage down or slow), hybrid
degrades to keyword-only and says so, rather than failing a live call.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.embed import Embedder, EmbeddingError, embedder

logger = logging.getLogger(__name__)

Mode = Literal["hybrid", "vector", "keyword"]
RRF_K = 60          # the constant from the original RRF paper; dampens rank 1 vs rank 2
CANDIDATES = 20     # per list, before fusion


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    title: str
    source: str
    ordinal: int
    text: str
    score: float = 0.0                      # what the results are ordered by (mode-specific)
    vector_rank: int | None = None
    vector_similarity: float | None = None  # 1 - cosine distance
    keyword_rank: int | None = None
    keyword_score: float | None = None      # ts_rank_cd


@dataclass
class SearchResult:
    mode: Mode
    hits: list[Hit] = field(default_factory=list)
    degraded: str | None = None  # why hybrid fell back to keyword-only


_COLUMNS = "c.id AS chunk_id, c.document_id, d.title, d.source, c.ordinal, c.text"

_VECTOR_SQL = text(f"""
    SELECT {_COLUMNS}, 1 - (c.embedding <=> CAST(:q AS vector)) AS similarity
    FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE d.practice_id = :practice_id
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :n
""")

# OR, not AND: plainto_tsquery("do you take delta dental") requires every
# word ('take' & 'delta' & 'dental'); a caller's question rarely has all its
# words in one chunk. Swapping & for | keeps the stemming and stop-word
# removal and lets ts_rank_cd reward chunks that match more of them.
_KEYWORD_SQL = text(f"""
    SELECT {_COLUMNS}, ts_rank_cd(c.tsv, query) AS rank
    FROM chunks c JOIN documents d ON d.id = c.document_id,
         CAST(replace(plainto_tsquery('english', :q)::text, '&', '|') AS tsquery) AS query
    WHERE d.practice_id = :practice_id AND c.tsv @@ query
    ORDER BY rank DESC, c.id
    LIMIT :n
""")


def _vector_hits(db: Session, practice_id: int, query_vector: list[float], n: int) -> list[Hit]:
    # With a WHERE filter, HNSW returns its ef_search nearest rows first and
    # filters after, so a tenant can come back short. pgvector >= 0.8 keeps
    # scanning until the filter is satisfied when asked to.
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = db.execute(_VECTOR_SQL, {"q": str(list(query_vector)), "practice_id": practice_id, "n": n})
    hits = []
    for rank, r in enumerate(rows.mappings(), 1):
        hits.append(Hit(r["chunk_id"], r["document_id"], r["title"], r["source"], r["ordinal"], r["text"],
                        score=float(r["similarity"]), vector_rank=rank,
                        vector_similarity=float(r["similarity"])))
    return hits


def _keyword_hits(db: Session, practice_id: int, query: str, n: int) -> list[Hit]:
    rows = db.execute(_KEYWORD_SQL, {"q": query, "practice_id": practice_id, "n": n})
    return [Hit(r["chunk_id"], r["document_id"], r["title"], r["source"], r["ordinal"], r["text"],
                score=float(r["rank"]), keyword_rank=rank, keyword_score=float(r["rank"]))
            for rank, r in enumerate(rows.mappings(), 1)]


def rrf(*ranked_lists: list[Hit], k: int = RRF_K) -> list[Hit]:
    """Reciprocal rank fusion. Merges hits by chunk, keeps every list's
    rank and score on the merged hit, orders by Σ 1/(k + rank)."""
    merged: dict[int, Hit] = {}
    for hits in ranked_lists:
        for rank, hit in enumerate(hits, 1):
            m = merged.setdefault(hit.chunk_id, Hit(hit.chunk_id, hit.document_id, hit.title, hit.source,
                                                    hit.ordinal, hit.text))
            m.score += 1 / (k + rank)
            for attr in ("vector_rank", "vector_similarity", "keyword_rank", "keyword_score"):
                if getattr(hit, attr) is not None:
                    setattr(m, attr, getattr(hit, attr))
    return sorted(merged.values(), key=lambda h: (-h.score, h.chunk_id))


def search(db: Session, practice_id: int, query: str, *, k: int = 5, mode: Mode = "hybrid",
           emb: Embedder | None = None) -> SearchResult:
    query = query.strip()
    if not query:
        return SearchResult(mode)
    result = SearchResult(mode)
    n = max(k, CANDIDATES)
    vector: list[Hit] = []
    if mode in ("hybrid", "vector"):
        try:
            query_vector = (emb or embedder()).embed_query(query)
        except EmbeddingError as e:
            if mode == "vector":
                raise
            logger.warning("query embedding failed, keyword-only: %s", e)
            result.degraded = f"embedding unavailable ({e}); keyword-only"
        else:
            vector = _vector_hits(db, practice_id, query_vector, n)
    keyword = _keyword_hits(db, practice_id, query, n) if mode in ("hybrid", "keyword") else []

    if mode == "vector":
        result.hits = vector[:k]
    elif mode == "keyword" or result.degraded:
        result.hits = keyword[:k]
    else:
        result.hits = rrf(vector, keyword)[:k]
    return result
