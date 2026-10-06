"""POST /rag/search: ranked knowledge-base chunks, with scores, for one practice
(app/rag/search.py, docs/notes/rag.md)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Practice
from app.rag.embed import EmbeddingError
from app.rag.search import search

router = APIRouter(prefix="/rag", tags=["rag"])


class SearchRequest(BaseModel):
    practice_id: int
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"


class SearchHitOut(BaseModel):
    rank: int
    score: float = Field(description="hybrid: RRF score; vector: cosine similarity; keyword: ts_rank_cd")
    chunk_id: int
    document_id: int
    title: str
    source: str
    ordinal: int
    text: str
    vector_rank: int | None
    vector_similarity: float | None
    keyword_rank: int | None
    keyword_score: float | None


class SearchResponse(BaseModel):
    practice_id: int
    query: str
    mode: str
    degraded: str | None = None
    results: list[SearchHitOut]


@router.post("/search", response_model=SearchResponse)
def rag_search(payload: SearchRequest, db: Session = Depends(get_db)):
    if db.get(Practice, payload.practice_id) is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    try:
        result = search(db, payload.practice_id, payload.query, k=payload.k, mode=payload.mode)
    except EmbeddingError as e:  # only mode="vector" raises; hybrid degrades instead
        raise HTTPException(status_code=503, detail=f"Embedding unavailable: {e}") from e
    return SearchResponse(
        practice_id=payload.practice_id, query=payload.query, mode=result.mode, degraded=result.degraded,
        results=[SearchHitOut(rank=i, **{f: getattr(h, f) for f in SearchHitOut.model_fields if f != "rank"})
                 for i, h in enumerate(result.hits, 1)],
    )
